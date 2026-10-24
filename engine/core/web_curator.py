"""网页整理流水线（2026-10-17）：规划 AI → 检索/抽取 → 审阅 AI → 重复率高就回头改查询。

三个角色（都走 OpenAI 兼容 ChatClient，配置沿用 config.json#article 的三张 profile）：
  planner   规划：按主题 + 上一轮的审阅反馈，产出本轮检索式（换角度、避开已用词）
  extractor 整理：把抓到的网页正文压成与主题相关的干净纯文本（无关就返回空）
  reviewer  审阅：判断文本相对主题的覆盖度与**内部重复率**，给出下一轮该怎么改查询

检索：用户给搜索引擎网址（内含 {q} 或 {query} 占位；没有占位就补 ?q=），只取其中的正常
       结果链接（过滤引擎自身域名 / 静态资源 / 重复 URL）。
抓取：urllib + 标准库 HTMLParser 抽正文段落，不引入第三方依赖。
去重：段落级精确去重 + 4-gram 字符 shingle 的 Jaccard 近似去重（>0.82 视为重复）。
输出：<工程>/data/corpora/<名字>.txt（纯文本，段间空行），可直接被「数据集」页当预训练语料勾选。

对外只暴露 WebCurator（run() 同步跑完整流程，emit(kind, payload) 推日志/进度）。
"""

from __future__ import annotations

import gzip
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Callable

from .dup import DupGuard, reask_hint, reask_level, units

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# 抽正文时整段跳过的标签
SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "iframe", "form", "select",
             "option", "button", "nav", "footer", "aside", "head"}
# 段落边界
BLOCK_TAGS = {"p", "div", "section", "article", "main", "li", "ul", "ol", "td", "th",
              "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "br", "tr"}
# 搜索结果里要丢掉的域名片段（搜索引擎自己 / 静态站 / 无正文）
BAD_HOSTS = ("bing.com", "microsoft.com", "msn.com", "live.com", "go.microsoft", "360kan.com",
             "so.com", "360.cn", "sogou.com", "baidu.com", "google.com", "w3.org",
             "schema.org", "mozilla.org", "creativecommons.org", "github.com/legal",
             "beian.miit.gov.cn", "doubleclick.net", "gstatic.com")
BAD_EXTS = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff",
            ".woff2", ".ttf", ".zip", ".rar", ".exe", ".mp4", ".mp3", ".pdf")


# ------------------------------------------------------------------ 网络 / 解码

def safe_url(url: str) -> str:
    """把非 ASCII 字符百分号编码（否则 urllib 会抛 UnicodeEncodeError）。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    def enc(v: str) -> str:
        return urllib.parse.quote(v, safe="/%:@&=?+#,;~!*'()[]")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, enc(parts.path), enc(parts.query), ""))


def http_get(url: str, timeout: float = 20.0, limit: int = 4_000_000) -> str:
    """取网页源码（文本）。失败抛异常，由调用方记日志跳过。"""
    url = safe_url(url)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(limit)
        ctype = resp.headers.get("Content-Type", "") or ""
        enc = ""
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            enc = m.group(1)
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return _decode(raw, enc)


def _decode(raw: bytes, enc: str = "") -> str:
    if enc:
        try:
            return raw.decode(enc, errors="ignore")
        except LookupError:
            pass
    for e in ("utf-8", "gb18030"):
        try:
            return raw.decode(e, errors="ignore")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def engine_url_for(engine: str, query: str) -> str:
    """把用户给的搜索引擎网址变成一次查询的 URL。

    支持 https://host/search?q={q}（也认 {query}）；没有占位就补 ?q=<词>。
    2026-10-18：首页地址（如 https://cn.bing.com）也能用 —— Bing / Google / 搜狗的
    首页是 JS 壳，服务端直接 ?q= 抓回来只有空壳（实测 bing 首页 15KB、0 条结果），
    所以在拼 ?q= 之前先把「首页」补成真正的检索路径。
    """
    base = str(engine or "").strip()
    q = urllib.parse.quote(str(query or "").strip())
    if "{q}" in base:
        return base.replace("{q}", q)
    if "{query}" in base:
        return base.replace("{query}", q)
    if not base:
        return ""
    pathed, param = _search_path(base)
    if pathed:
        return pathed + "?" + param + "=" + q
    joiner = "&" if "?" in base else "?"
    return base + joiner + "q=" + q


# 首页 → (检索路径, 检索词参数名)：只认明确知道的几家，其余原样补 ?q=
_SEARCH_PATHS = (
    ("bing.com", "/search", "q"),
    ("google.com", "/search", "q"),
    ("baidu.com", "/s", "wd"),
    ("sogou.com", "/web", "query"),
)


def _search_path(base: str) -> tuple[str, str]:
    """首页地址 → (补好的前缀, 参数名)；已经写死路径/参数或认不出来的返回 ("", "")。"""
    try:
        p = urllib.parse.urlparse(base)
    except Exception:                                    # noqa: BLE001
        return "", ""
    host = (p.hostname or "").lower()
    path = p.path or "/"
    if path not in ("", "/") or p.query:
        return "", ""                                    # 用户自己写了路径/参数，绝不多手
    for suffix, real, param in _SEARCH_PATHS:
        if host == suffix or host.endswith("." + suffix):
            return base.rstrip("/") + real, param
    return "", ""


# ------------------------------------------------------------------ HTML → 段落

class _ParaText(HTMLParser):
    """把 HTML 抽成段落列表（跳过脚本/样式/导航等）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.buf: list[str] = []
        self.paras: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        t = data.strip()
        if t:
            self.buf.append(t)

    def _flush(self) -> None:
        if not self.buf:
            return
        text = re.sub(r"\s+", " ", " ".join(self.buf)).strip()
        self.buf = []
        if text:
            self.paras.append(text)


def html_to_paras(html: str) -> list[str]:
    p = _ParaText()
    try:
        p.feed(html)
        p.close()
    except Exception:                                    # noqa: BLE001 坏 HTML 不致命
        pass
    p._flush()
    out = [x for x in p.paras if len(x) >= 30]
    # 去掉明显的样板行（导航/版权/面包屑）
    junk = re.compile(r"(版权所有|©\s*\d{4}|All rights reserved|登录|注册|Cookie|隐私政策|"
                      r"关于我们|联系我们|广告|下载APP|扫码|分享到)")
    return [x for x in out if not junk.search(x)]


RESULT_BLOCK = re.compile(r'<li[^>]*class="[^"]*\b(?:b_algo|res-list|result)\b[^"]*"[^>]*>(.*?)</li>', re.S)


def _clean_hrefs(chunk: str, seen: set, out: list, limit: int) -> None:
    for m in re.finditer(r'href="(https?://[^"]+)"', chunk):
        url = m.group(1).replace("&amp;", "&")
        try:
            parts = urllib.parse.urlparse(url)
        except ValueError:
            continue
        host = (parts.netloc or "").lower()
        is_engine_redirect = (host.endswith("so.com") and parts.path == "/link") or \
                             (host.endswith("bing.com") and parts.path.startswith("/ck"))
        if not host or (any(b in host for b in BAD_HOSTS) and not is_engine_redirect):
            continue
        if parts.path.lower().endswith(BAD_EXTS):
            continue
        key = host + parts.path + "?" + (parts.query or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= limit:
            return


def pick_links(page: str, limit: int = 8) -> list[str]:
    """从搜索结果页里挑出结果链接。

    2026-10-17：优先只解析结果块（Bing 的 li.b_algo / 360 的 res-list），
    否则侧栏、词典挂件、导航链接会排在真结果前面，把额度吃光（实测搜「大模型微调定义」
    拿到的全是大字典页）。
    """
    seen: set = set()
    out: list[str] = []
    for m in RESULT_BLOCK.finditer(page):
        _clean_hrefs(m.group(1), seen, out, limit)
        if len(out) >= limit:
            break
    if len(out) >= max(2, limit // 2):
        return out
    # 兜底：整页扫，但要跳过导航/页脚常见的相对锚（这里只认绝对 http(s)）
    for m in re.finditer(r'href="(https?://[^"]+)"', page):
        url = m.group(1).replace("&amp;", "&")
        try:
            parts = urllib.parse.urlparse(url)
        except ValueError:
            continue
        host = (parts.netloc or "").lower()
        is_engine_redirect = (host.endswith("so.com") and parts.path == "/link") or \
                             (host.endswith("bing.com") and parts.path.startswith("/ck"))
        if not host or (any(b in host for b in BAD_HOSTS) and not is_engine_redirect):
            continue
        if parts.path.lower().endswith(BAD_EXTS):
            continue
        key = host + parts.path + "?" + (parts.query or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ 主题相关性

def resolve_redirect(url: str, timeout: float = 15.0) -> str:
    """把搜索引擎的跳转链接（360 的 so.com/link?m=…、Bing 的 /ck/a）还原成真实网址。

    还原不出来就原样返回，交给后续抓取去失败。
    """
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return url
    host = (parts.netloc or "").lower()
    if not (host.endswith("so.com") or host.endswith("bing.com")):
        return url
    try:
        page = http_get(url, timeout=timeout, limit=400_000)
    except Exception:                                    # noqa: BLE001
        return url
    pat = r"""(?:URL=|url=|href=|location\.replace\()\s*['"]?(https?://[^'"<>\s]+)"""
    for m in re.finditer(pat, page):
        target = m.group(1).replace("&amp;", "&")
        try:
            tparts = urllib.parse.urlparse(target)
        except ValueError:
            continue
        thost = (tparts.netloc or "").lower()
        if not thost or "so.com" in thost or "bing.com" in thost or "360.cn" in thost:
            continue
        return target
    return url


def serp_titles(page: str, limit: int = 12) -> list[str]:
    """搜索结果页里的标题文字（用于判断这条检索式有没有被引擎拆成单字）。"""
    out: list[str] = []
    for m in re.finditer(r"<h[23][^>]*>(.*?)</h[23]>", page, re.S):
        t = re.sub(r"<[^>]+>", "", m.group(1))
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def topic_grams(topic: str) -> tuple:
    """主题的两个粗筛口径：中文 3-gram（去空格）+ 英文长词。"""
    t = re.sub(r"\s+", "", topic or "")
    grams = {t[i:i + 3] for i in range(max(0, len(t) - 2))} if len(t) >= 3 else {t}
    words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", topic or "")}
    return (grams, words)


def page_relevant(text: str, grams: tuple) -> bool:
    """粗筛：正文里出现主题的一个 3-gram（或英文长词）就算相关。宁可放过，不要错杀。"""
    if not text:
        return False
    cjk, words = grams
    low = text.lower()
    if any(g and g in text for g in cjk):
        return True
    return any(w in low for w in words)


# ------------------------------------------------------------------ 去重

def _shingles(text: str, n: int = 4) -> set:
    s = re.sub(r"\s+", "", text or "")
    if len(s) <= n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def split_paras(text: str) -> list[str]:
    out = []
    for chunk in re.split(r"\n\s*\n|\n", text or ""):
        t = chunk.strip()
        t = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", t)      # 列表符号（模型偶尔还是会给）
        if len(t) >= 20:
            out.append(t)
    return out


def dup_stats(paras: list[str]) -> tuple:
    """返回 (重复率, 重复片段示例列表)。重复 = 与已有段落 4-gram Jaccard > 0.82。"""
    kept: list = []          # [(para, shingles)]
    dup_examples: list[str] = []
    dup = 0
    for p in paras:
        sh = _shingles(p)
        is_dup = False
        for _, other in kept:
            if _jaccard(sh, other) > 0.82:
                is_dup = True
                break
        if is_dup:
            dup += 1
            if len(dup_examples) < 3:
                dup_examples.append(p[:40])
        else:
            kept.append((p, sh))
    total = max(1, len(paras))
    return round(dup / float(total), 3), dup_examples


def dedupe_paras(candidates: list[str]) -> tuple:
    """段落级去重：完全相同的段落只留一次，近似重复的丢掉后面的。"""
    kept: list = []
    kept_sh: list = []
    dropped = 0
    for p in candidates:
        sh = _shingles(p)
        if any(_jaccard(sh, o) > 0.82 for o in kept_sh):
            dropped += 1
            continue
        kept.append(p)
        kept_sh.append(sh)
    return kept, dropped


# ------------------------------------------------------------------ LLM 角色

def _json_reply(text: str) -> dict:
    """从模型回复里抠出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        obj = json.loads(t[i:j + 1])
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _default_sys(lang: str = "zh") -> dict[str, str]:
    """网页整理的出厂提示词（用户 2026-10-20）：和 config.json#webcurate 是同一份。

    网页整理也是蒸馏的一种，提示词同样定稿、带语言要求；这里按界面语言取中文版 / 英文版，
    页面「模型配置」里改了系统提示词则以页面上的为准（见 WebCurator 的 *_sys 参数）。
    """
    try:
        from .config import DEFAULT_CONFIG          # type: ignore
        sec = DEFAULT_CONFIG.get("webcurate") or {}
    except Exception:                               # noqa: BLE001
        sec = {}
    want_en = str(lang or "").strip().lower().startswith("en")
    out = {}
    for role, fallback in (("planner", PLANNER_SYS_FALLBACK),
                           ("extractor", EXTRACTOR_SYS_FALLBACK),
                           ("reviewer", REVIEWER_SYS_FALLBACK)):
        prof = sec.get(role) if isinstance(sec.get(role), dict) else {}
        zh = str(prof.get("system_prompt") or "").strip()
        en = str(prof.get("system_prompt_en") or "").strip()
        out[role] = (en or zh or fallback) if want_en else (zh or en or fallback)
    return out


# 兜底文案（config 读不到时用；正常情况走 _default_sys 里 config 的定稿）
PLANNER_SYS_FALLBACK = (
    "你是检索策略规划助手，为语言模型蒸馏语料规划网页检索。根据主题给出互不重叠的检索式，"
    "覆盖不同角度（定义、原理、做法、数据、案例、对比、常见问题），每条 6~16 个字。"
    '只输出 JSON：{"queries":[{"q":"检索式","angle":"这条想拿到什么"}]}，不要解释。'
    "所有输出一律使用简体中文。"
)
EXTRACTOR_SYS_FALLBACK = (
    "你是网页正文整理助手。把给定网页文本整理成干净纯文本：只保留与主题相关的事实内容，"
    "去掉导航、广告、版权、评论区与 markdown 符号，分 2~6 段、全文不超过 600 字，不要解释。"
    "所有输出一律使用简体中文。"
)
REVIEWER_SYS_FALLBACK = (
    "你是语料审阅助手，判断文本能否直接用于训练：看主题覆盖度与内部重复率。"
    '只输出 JSON：{"dup_rate":0~1,"coverage":0~1,"verdict":"ok|redo","advice":"建议","dups":[]}。'
    "所有输出一律使用简体中文。"
)
PLANNER_SYS = PLANNER_SYS_FALLBACK
EXTRACTOR_SYS = EXTRACTOR_SYS_FALLBACK
REVIEWER_SYS = REVIEWER_SYS_FALLBACK


def _valid_rate(v) -> bool:
    """模型给的 dup_rate 是否可用（0~1 的有限数）。"""
    try:
        f = float(v)                            # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return 0.0 <= f <= 1.0


class WebCurator:
    """一条完整流水线。run() 同步执行；emit(kind, payload) 推事件给宿主。"""

    def __init__(
        self,
        planner,                       # ChatClient
        extractor,                     # ChatClient
        reviewer,                      # ChatClient
        *,
        topic: str,
        engine_url: str,
        out_path: str,
        pieces: int = 20,
        concurrency: int = 3,
        max_chars: int = 500,
        queries_per_round: int = 4,
        pages_per_query: int = 3,
        max_rounds: int = 3,
        min_page_chars: int = 200,
        dup_warn: float = 0.0,
        dup_abort: float = 0.0,
        lang: str = "zh",              # 界面语言（用户 2026-10-20）：决定默认提示词与语料语言
        planner_sys: str = "",
        extractor_sys: str = "",
        reviewer_sys: str = "",
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.planner = planner
        self.extractor = extractor
        self.reviewer = reviewer
        # 提示词：页面「模型配置」里的优先，空着就用本语言的出厂定稿（用户 2026-10-20）
        _d = _default_sys(lang)
        self.planner_sys = str(planner_sys or "").strip() or _d["planner"]
        self.extractor_sys = str(extractor_sys or "").strip() or _d["extractor"]
        self.reviewer_sys = str(reviewer_sys or "").strip() or _d["reviewer"]
        self.topic = (topic or "").strip()
        self.engine_url = (engine_url or "").strip()
        self.out_path = out_path
        self.queries_per_round = max(1, int(queries_per_round))
        self.pages_per_query = max(1, int(pages_per_query))
        self.max_rounds = max(1, int(max_rounds))
        self.pieces_target = max(1, int(pieces))            # 采集篇数（用户 2026-10-21：按篇采集）
        self.concurrency = max(1, int(concurrency))         # 并发抓取/整理的网页数
        self.max_chars = max(100, int(max_chars))           # 每篇最大字数
        self.min_page_chars = max(50, int(min_page_chars))
        self.on_event = on_event
        self._stop = threading.Event()

        self.used_queries: list[str] = []
        self.paras: list[str] = []                          # 已收的「篇」（每篇一段整理后正文）
        self.sources: list[dict] = []
        self.rounds: list[dict] = []
        self.dup_rate = 0.0
        self.guard = DupGuard(dup_warn, dup_abort)         # 重复率守卫（阈值可设，用户 2026-10-21：高级设置「重复率上限」）
        self._dup_warn, self._dup_abort = dup_warn, dup_abort
        self._extract_fails = 0

    # ---------- 控制 / 输出 ----------
    def stop(self) -> None:
        self._stop.set()

    def _emit(self, kind: str, payload: dict | None = None) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload or {})
            except Exception:                            # noqa: BLE001 事件回调不该弄挂流水线
                pass

    def _log(self, text: str) -> None:
        self._emit("log", {"text": text})

    def _progress(self, **kw: Any) -> None:
        base = {"chars": self.chars(), "paras": len(self.paras),
                "pieces": len(self.paras), "pieces_target": self.pieces_target,
                "sources": len(self.sources), "dup_rate": self.dup_rate,
                "round": len(self.rounds), "topic": self.topic}
        base.update(kw)
        self._emit("progress", base)

    def chars(self) -> int:
        return sum(len(p) for p in self.paras)

    # ---------- 三个角色的调用 ----------
    def _plan(self, feedback: str, avoid: list[str]) -> list[dict]:
        user = ["主题：%s" % self.topic,
                "需要 %d 条检索式。" % self.queries_per_round]
        if avoid:
            user.append("已用检索式（不要重复）：" + "；".join(avoid[-12:]))
        if feedback:
            user.append("上一轮审阅反馈：" + feedback)
        h = reask_hint(reask_level(self.guard.rate()), "查法")   # 重复率过线追加「换一种查法」（用户 2026-10-21：对用户不可见）
        if h:
            user.append(h)
        reply = self.planner.chat(
            [{"role": "system", "content": self.planner_sys},
             {"role": "user", "content": "\n".join(user)}],
            temperature=0.9, max_tokens=1200)
        obj = _json_reply(reply)
        out: list[dict] = []
        for it in (obj.get("queries") or [])[:self.queries_per_round]:
            if isinstance(it, str):
                q = it.strip()
                angle = ""
            elif isinstance(it, dict):
                q = str(it.get("q") or it.get("query") or "").strip()
                angle = str(it.get("angle") or "").strip()
            else:
                continue
            if q and q not in self.used_queries and q not in [o["q"] for o in out]:
                out.append({"q": q, "angle": angle})
        return out

    def _extract(self, url: str, raw_paras: list[str]) -> str:
        full = "\n\n".join(raw_paras)
        if len(full) < self.min_page_chars:
            return ""
        text = ""
        last_err: Exception | None = None
        # 第一次失败就把正文砍短重试一次（2026-10-17：推理型模型容易把正文挤进 max_tokens 之外）
        for limit in (5000, 2500, 1200):
            try:
                reply = self.extractor.chat(
                    [{"role": "system", "content": self.extractor_sys},
                     {"role": "user", "content": "主题：%s\n网页：%s\n\n正文：\n%s"
                      % (self.topic, url, full[:limit])}],
                    temperature=0.3, max_tokens=2400)
                text = (reply or "").strip()
                last_err = None
                break
            except Exception as exc:                     # noqa: BLE001
                last_err = exc
        if last_err is not None:
            raise last_err
        if text in ("", "无", "空", "（空）"):
            return ""
        return text

    def _review(self) -> dict:
        text = "\n\n".join(self.paras)
        sample = text[:4000]
        if len(text) > 6000:
            sample += "\n\n……\n\n" + text[-2000:]
        reply = self.reviewer.chat(
            [{"role": "system", "content": self.reviewer_sys},
             {"role": "user", "content":
              "主题：%s\n段落数：%d · 字符数：%d · 来源网页：%d\n\n文本：\n%s"
              % (self.topic, len(self.paras), self.chars(), len(self.sources), sample)}],
            temperature=0.2, max_tokens=1500)
        obj = _json_reply(reply)
        if not obj:
            self._log("[审阅] 模型没按 JSON 回（%s），本次按本地重复率判断"
                      % ((reply or "空回复").strip()[:60]))
        local_dup, examples = dup_stats(self.paras)
        try:
            llm_dup = float(obj.get("dup_rate"))
        except (TypeError, ValueError):
            llm_dup = None
        try:
            coverage = float(obj.get("coverage"))
        except (TypeError, ValueError):
            coverage = None
        dups = [str(x)[:40] for x in (obj.get("dups") or []) if str(x).strip()][:3]
        return {
            "dup_local": local_dup,
            "dup_llm": llm_dup,
            "dup_rate": max(local_dup, llm_dup if _valid_rate(llm_dup) else 0.0),
            "coverage": coverage,
            "verdict": str(obj.get("verdict") or "").strip(),
            "advice": str(obj.get("advice") or "").strip(),
            "dups": dups or examples,
        }

    # ---------- 按篇采集（用户 2026-10-21：采集篇数 / 并发数 / 最大字数） ----------
    def _trim(self, text: str) -> str:
        """一篇最多 max_chars 字：按段落边界截断，整段超长就硬截。短段不丢（截断只砍不删）。"""
        if not self.max_chars or len(text) <= self.max_chars:
            return text
        out: list[str] = []
        n = 0
        for p in (x.strip() for x in re.split(r"\n\s*\n|\n", text)):
            if not p:
                continue
            if out and n + len(p) > self.max_chars:
                break
            out.append(p)
            n += len(p) + 2
        t = "\n\n".join(out)
        return t[:self.max_chars] if t else text[:self.max_chars]

    def _piece_dup(self, text: str) -> bool:
        """整篇与已收篇近似重复（4-gram Jaccard > 0.82）→ 丢这一篇。"""
        sh = _shingles(text)
        return any(_jaccard(sh, _shingles(p)) > 0.82 for p in self.paras)

    def _dup_feed(self, text: str) -> None:
        """重复率守卫：≥警告线 弹窗一次，≥中止线 中止采集（阈值可设，用户 2026-10-21）。"""
        rate = self.guard.add_many(units(text))
        self.dup_rate = rate
        level = self.guard.level()
        w = round(self.guard.warn_at * 100)
        a = round(self.guard.abort_at * 100)
        if level == "warn":
            self._log("[重复率] 已达 %d%%（≥%d%%），内容重复偏高" % (round(rate * 100), w))
            self._emit("dup_warn", {"rate": rate, "warn": w, "abort": a})
        elif level == "abort":
            self._log("[重复率] 已达 %d%%（≥%d%%），中止采集（已收集内容保留）" % (round(rate * 100), a))
            self._emit("dup_abort", {"rate": rate, "warn": w, "abort": a})
            self._stop.set()

    def _fetch_batch(self, q: str, links: list[str], grams: tuple) -> int:
        """并发抓取/整理一条检索式的候选页；每条检索式最多收 pages_per_query 篇。返回新增篇数。"""
        lock = threading.Lock()
        seen_urls: set = set()
        got = 0

        def work(link: str) -> None:
            nonlocal got
            if self._stop.is_set() or self.guard.aborted:
                return
            with lock:
                if got >= self.pages_per_query or len(self.paras) >= self.pieces_target:
                    return
            try:
                link = resolve_redirect(link)        # 360/Bing 跳转链接 → 真实网址
                with lock:
                    if link in seen_urls:            # 一条结果常给两个跳转，还原后去重
                        return
                    seen_urls.add(link)
                html = http_get(link, timeout=20)
                raw = html_to_paras(html)
            except Exception as exc:                 # noqa: BLE001
                self._log("[抓取] 跳过 %s（%s）" % (link[:70], type(exc).__name__))
                return
            raw_chars = sum(len(x) for x in raw)
            if raw_chars < self.min_page_chars:
                self._log("[抓取] 跳过 %s（正文太短 %d 字）" % (link[:70], raw_chars))
                return
            # 相关性粗筛：明显跟主题无关的页不进模型（省时间省 token）
            if not page_relevant("\n".join(raw), grams):
                self._log("[整理] %s 与主题无关（正文没有主题关键词），丢弃" % link[:70])
                return
            try:
                clean = self._extract(link, raw)
            except Exception as exc:                 # noqa: BLE001
                # 兜底：整理 AI 没吐正文时用原文段落顶上（标记出来，别让整篇空手）
                self._extract_fails += 1
                clean = "\n\n".join([x for x in raw if len(x) >= 40][:6])[:900]
                self._log("[整理] 模型没回正文（%s: %s），改用原文段落兜底 %d 字"
                          % (type(exc).__name__, str(exc)[:50], len(clean)))
            if not clean:
                self._log("[整理] %s 与主题无关，丢弃" % link[:70])
                return
            clean = self._trim(clean)                # 篇内截断到最大字数
            self._dup_feed(clean)                    # 重复率：抓到什么算什么（含随后被判重的）
            if self._stop.is_set() or self.guard.aborted:
                return
            with lock:
                if self._piece_dup(clean):
                    self._log("[去重] %s 与已收篇重复，丢弃" % link[:70])
                    return
                if got >= self.pages_per_query or len(self.paras) >= self.pieces_target:
                    return
                self.paras.append(clean)
                self.sources.append({"url": link, "chars": len(clean)})
                got += 1
                self._log("[整理] %s → 第 %d 篇 / %d 字" % (link[:70], len(self.paras), len(clean)))
            self._progress()

        workers = min(max(1, self.concurrency), max(1, len(links)))
        if links:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(work, links))
        return got

    # ---------- 主流程 ----------
    def run(self) -> dict:
        if not self.topic:
            raise ValueError("主题不能为空")
        if not self.engine_url:
            raise ValueError("搜索引擎网址不能为空")

        self.guard = DupGuard(self._dup_warn, self._dup_abort)
        self.dup_rate = 0.0
        feedback = ""
        for rnd in range(1, self.max_rounds + 1):
            if self._stop.is_set() or self.guard.aborted:
                self._log("[停止] 收到停止请求，本轮结束后退出")
                break
            self._log("———— 第 %d 轮：%s ————" % (rnd, "让规划 AI 换一批查法" if rnd > 1 else "让规划 AI 出一批查法"))
            plan = self._plan(feedback, self.used_queries)
            if not plan:
                self._log("[规划] 没拿到可用检索式（模型没按 JSON 回？），本轮跳过")
                break
            for item in plan:
                self._log("[规划] 检索式「%s」· 想拿到：%s" % (item["q"], item["angle"] or "—"))

            got = 0
            grams = topic_grams(self.topic)
            for item in plan:
                if self._stop.is_set() or self.guard.aborted:
                    break
                if len(self.paras) >= self.pieces_target:
                    break
                q = item["q"]
                self.used_queries.append(q)
                try:
                    url = engine_url_for(self.engine_url, q)
                    page = http_get(url, timeout=20)
                except Exception as exc:                 # noqa: BLE001
                    self._log("[检索] 「%s」失败：%s: %s" % (q, type(exc).__name__, str(exc)[:80]))
                    continue
                titles = serp_titles(page)
                if titles and not any(page_relevant(t, grams) for t in titles):
                    self._log("[检索] 「%s」的结果跟主题无关（引擎把词拆开搜了），跳过这条检索式"
                              % q)
                    self._log("[检索] 首条结果标题：「%s」" % titles[0][:34])
                    continue
                links = pick_links(page, limit=self.pages_per_query * 4)
                self._log("[检索] 「%s」拿到 %d 个候选链接" % (q, len(links)))
                got += self._fetch_batch(q, links, grams)    # 并发抓取整理（并发数）
                self._progress()
                if self.guard.aborted:
                    break
                if len(self.paras) >= self.pieces_target:
                    self._log("[进度] 已收满 %d 篇，停止采集" % self.pieces_target)
                    break

            self.dup_rate = self.guard.rate()
            self._progress()
            self._log("[统计] 本轮新增 %d 篇 · 累计 %d 篇 / %d 字 · 重复率 %.2f"
                      % (got, len(self.paras), self.chars(), self.dup_rate))

            if self.guard.aborted:
                break
            if not self.paras:
                self._log("[审阅] 还没有任何内容，跳过审阅")
                continue

            self._log("———— 让审阅 AI 看覆盖度 ————")
            try:
                rev = self._review()
            except Exception as exc:                     # noqa: BLE001
                self._log("[审阅] 模型调用失败（%s: %s）" % (type(exc).__name__, str(exc)[:60]))
                rev = {"dup_rate": self.dup_rate, "coverage": None, "verdict": "",
                       "advice": "", "dups": []}
            self.rounds.append({
                "round": rnd,
                "queries": [x["q"] for x in plan],
                "new_paras": got,
                "chars": self.chars(),
                "dup_rate": self.dup_rate,
                "coverage": rev.get("coverage"),
                "verdict": rev.get("verdict"),
                "advice": rev.get("advice"),
            })
            self._log("[审阅] 重复率 %.2f · 覆盖度 %s · 判定 %s"
                      % (self.dup_rate,
                         ("%.2f" % rev["coverage"]) if rev.get("coverage") is not None else "—",
                         rev.get("verdict") or "—"))
            feedback = rev.get("advice") or ""
            if feedback:
                self._log("[通知规划 AI] %s" % feedback)

        text = "\n\n".join(self.paras).strip()
        if not text:
            hint = ""
            if self._extract_fails:
                hint = ("；整理 AI 有 %d 次只回了推理、没吐出正文（推理型模型会把 max_tokens 吃光）——"
                        "换一个不推理的模型，或在「语料蒸馏」页把该角色的 max_tokens 调大" % self._extract_fails)
            raise RuntimeError("没有整理出任何内容（检索/抓取/整理全空）%s。可以换主题、换搜索引擎网址，"
                               "或调大每轮检索式/每检索篇数" % hint)
        os.makedirs(os.path.dirname(self.out_path) or ".", exist_ok=True)
        with open(self.out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")

        result = {
            "ok": True,
            "topic": self.topic,
            "engine_url": self.engine_url,
            "path": self.out_path,
            "chars": len(text),
            "paras": len(self.paras),
            "pieces": len(self.paras),
            "pieces_target": self.pieces_target,
            "sources": self.sources,
            "rounds": self.rounds,
            "dup_rate": self.dup_rate,
            "stopped": self._stop.is_set(),
        }
        self._log("[完成] %s · %d 字 / %d 篇 · 重复率 %.2f"
                  % (os.path.basename(self.out_path), len(text), len(self.paras), self.dup_rate))
        self._emit("result", result)
        return result

