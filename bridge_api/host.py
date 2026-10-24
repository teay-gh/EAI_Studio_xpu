"""bridge_api/host.py —— 「托管」页后端（用户 2026-10-17）。

用户描述想要的对话模型 → 托管 AI 出**方案 + 预估消耗**（时间 / 存储 / 内存显存 / token）→
用户审阅可改 → 确认后由托管 AI 按「数据蒸馏 → 网页整理 → 训练 → 验证 → 结论」跑完整条链。

护栏（用户原话逐条落地）：
  1. **只有上一件事做完、要切下一件事时才叫醒托管 AI** 分析「该不该继续」；
  2. 托管 AI 有中止权；每次切换给它 2 分钟（120s）做分析；
  3. 设置里勾了「强制托管训练完成」→ 托管 AI 只弹风险提示、不再中止；
  4. 某个任务 5 分钟没有任何进度 → 叫醒托管 AI 判断是不是真卡住，卡住就停
     （**不受**「强制完成」影响，勾了也照样能因卡住而停）；
  5. 托管全程只用设置里的**默认 API**（config.assistant），页面上不允许填别的 api。

实现要点：跑在后台线程里（_host.start_task，组 host），页面轮询 GET /api/host/state?since=N。
所有 AI 调用失败都不抛到页面：降级成「继续」并把原因写进事件流（宁可不拦，也别把托管卡死）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable, Optional

import _host

TASK_NAME = "host:orchestrator"
GROUP = "host"

STEP_LABEL = {
    "distill": "数据蒸馏",
    "webcurate": "网页整理",
    "train": "训练",
    "validate": "验证",
    "report": "结论",
}

AI_GATE_SECONDS = 120.0        # 切任务时给托管 AI 的分析时间（用户口径：2 分钟）
STALL_SECONDS = 300.0          # 单任务无进度多久算可疑（用户口径：5 分钟）
STALL_ASK_SECONDS = 60.0       # 卡死判定给 AI 的时间
POLL = 2.0                     # 进度采样间隔

_STOP = threading.Event()
_LOCK = threading.RLock()
_PLAN: dict[str, Any] = {}          # 当前方案（draft 或已确认）
_STEPS: list[dict[str, Any]] = []   # 运行期的步骤状态
_TRAIN_LOG = ""                      # 本次托管训练的 loss CSV 路径（日志卡读它）
_LOG_LINES: list[str] = []            # 托管自己的运行日志（页面「运行日志」卡；重启用 last_run.json 恢复）
_SUBSTEPS: dict[str, list[str]] = {}  # 每个大步骤的「小步」明细（用户 2026-10-18：点步骤卡看）
_STATE_ROOT = ""                      # 上面这些状态属于哪个工程（切换工程要清空，用户 2026-10-18）

# ---------------- 小步（微阶段）状态：托管弹窗按「上下排布的小卡片 + 状态球」展示（用户 2026-10-18）
# 每个大步骤拆成固定微阶段，状态由真实信号推进：idle=未运行 / run=运行中 / ok=完成 / err=出错
STAGE_DEFS: dict[str, list[str]] = {
    "distill": ["准备主题与参数", "生成主题（AI）", "批量生成问题（AI）",
                "批量生成回答（AI）", "质检与去重", "写入语料文件"],
    "webcurate": ["规划检索词（AI）", "抓取网页正文", "正文整理（AI）", "去重与质检", "写入语料文件"],
    "train": [],      # 训练：行由方案里的训练方式决定（预训练 / 继续预训练 / 指令微调 / LoRA 微调），只一条
    "validate": ["载入训练产物", "生成样例回答（AI）", "AI 判定质量", "写回结论"],
    "report": ["汇总指标", "写入报告"],
}
_STAGE: dict[str, list[dict[str, str]]] = {}
_DIST_CUR = 0        # 蒸馏事件游标（用于扫日志里的阶段标记）
_WEB_CUR = 0         # 网页整理事件游标


def _stage_rows(key: str) -> list[dict[str, str]]:
    rows = _STAGE.get(key)
    if rows is None:
        rows = [{"label": t, "state": "idle", "note": ""} for t in STAGE_DEFS.get(key, [])]
        _STAGE[key] = rows
    return rows


def _stage_init(key: str) -> None:
    _STAGE[key] = [{"label": t, "state": "idle", "note": ""} for t in STAGE_DEFS.get(key, [])]


def _stage_go(key: str, idx: int, state: str = "run", note: str = "") -> None:
    """推进到第 idx 个微阶段：之前的自动记完成，当前记 state。"""
    rows = _stage_rows(key)
    for i, row in enumerate(rows):
        if i < idx and row["state"] in ("idle", "run"):
            row["state"] = "ok"
        elif i == idx:
            row["state"] = state
            if note:
                row["note"] = note


def _stage_all(key: str, state: str = "ok") -> None:
    for row in _stage_rows(key):
        if row["state"] in ("idle", "run"):
            row["state"] = state


def _stage_fail(key: str, note: str = "") -> None:
    rows = _stage_rows(key)
    for row in rows:
        if row["state"] == "run":
            row["state"] = "err"
            if note:
                row["note"] = note
            return
    if rows:
        rows[-1]["state"] = "err"
        if note:
            rows[-1]["note"] = note


def _scan_distill_log(text: str) -> None:
    """把采集日志里的阶段话术映射成微阶段（引擎自己打的「阶段 1/2」「入库 #N」）。"""
    if not text:
        return
    if "阶段 1/2" in text:
        _stage_go("distill", 2, note="正在出题")
    elif "阶段 2/2" in text:
        _stage_go("distill", 3, note="正在答题")
    elif "审阅未过" in text:
        _stage_go("distill", 4, note="审阅未过，丢弃一条")
    elif "入库 #" in text or "完成 #" in text:
        _stage_go("distill", 5, note="已入库")


def _scan_web_log(text: str) -> None:
    """网页整理的微阶段：规划 → 检索/抓取 → 整理 → 去重质检 → 落盘。"""
    if not text:
        return
    if "[规划]" in text:
        _stage_go("webcurate", 0, note="在挑检索式")
    elif "[检索]" in text:
        _stage_go("webcurate", 1, note="在搜候选页")
    elif "[抓取]" in text:
        _stage_go("webcurate", 1, note="在抓正文")
    elif "[整理]" in text:
        _stage_go("webcurate", 2, note="AI 整理正文")
    elif "[去重]" in text:
        _stage_go("webcurate", 3, note="去重/质检")
    elif "[统计]" in text:
        _stage_go("webcurate", 4, note="在落盘")

_CUR_STEP = ""                        # 当前正在跑的大步骤 key（_emit 自动归到它名下）
_PAUSE = threading.Event()            # set = 已暂停（不再推进下一步 / 不做卡死判定）
_DECISIONS: list[dict[str, Any]] = []
_REPORT = ""
_ERROR = ""
_STARTED_AT = 0.0
_FINISHED_AT = 0.0
_LAST_FAIL = ""      # 最近一次子进程失败时的日志尾巴（写进报告，别只写「异常退出」）


def _set_fail(text: str) -> None:
    global _LAST_FAIL
    _LAST_FAIL = (text or "").strip()


# ---------------------------------------------------------------- 配置 / AI

def _cfg() -> dict[str, Any]:
    return _host.load_cfg()


def _default_api() -> dict[str, Any]:
    """托管只认设置里的默认 API（config.assistant），不读任何页面级配置。"""
    a = _cfg().get("assistant") or {}
    return {"base_url": str(a.get("base_url") or "").strip(),
            "api_key": str(a.get("api_key") or "").strip(),
            "model": str(a.get("model") or "").strip()}


def _ai(timeout: float = AI_GATE_SECONDS, max_tokens: int = 1600, temperature: float = 0.2):
    from core.client import ChatClient              # 引擎模块（_host 已把引擎目录放进 sys.path）
    api = _default_api()
    missing = [k for k in ("base_url", "api_key", "model") if not api[k]]
    if missing:
        raise RuntimeError("设置里的默认 API 缺字段：" + " / ".join(missing) + "（到「全局设置 → 默认 API」补齐）")
    return ChatClient(api["base_url"], api["api_key"], api["model"],
                      temperature=temperature, max_tokens=max_tokens, timeout=timeout)


def _json_from(text: str) -> Optional[dict]:
    """从模型回复里抠出第一个 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:                                # noqa: BLE001
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(t[i:j + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:                            # noqa: BLE001
            return None
    return None


# ---------------------------------------------------------------- 环境快照

REPO_MIN_LINES = 20          # 至少这么多条才算「能用」（用户 2026-10-18）


def _repo_audit() -> dict[str, Any]:
    """仓库体检（用户 2026-10-18）：把现有数据集列出来，标出哪些够量、能直接训练用。"""
    out: dict[str, Any] = {"datasets": [], "usable": [], "error": ""}
    seen: set = set()
    try:
        import repo as repo_mod
        items = repo_mod.summary({}) or {}
        for d in (items.get("datasets") or []):
            lines = int(d.get("lines") or 0)
            row = {
                "name": str(d.get("name") or ""),
                "kind": str(d.get("kind") or ""),
                "lines": lines,
                "chars": int(d.get("chars") or 0),
                "mb": round(int(d.get("size_bytes") or 0) / 1048576.0, 2),
                "tab": list(d.get("tab") or []),
                "use": str(d.get("use") or ""),
                "path": str(d.get("path") or ""),
                "usable": bool(d.get("ok", True)) and lines >= REPO_MIN_LINES,
            }
            key = (row["name"], row["lines"])      # 同名同条数（工程内副本 + 外部引用）只算一个
            if key in seen:
                continue
            seen.add(key)
            out["datasets"].append(row)
            if row["usable"]:
                out["usable"].append(row)
    except Exception as exc:                             # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    out["datasets"].sort(key=lambda r: -int(r.get("lines") or 0))
    out["usable"].sort(key=lambda r: -int(r.get("lines") or 0))
    return out


def _env_facts() -> dict[str, Any]:
    cfg = _cfg()
    proj = _host.project(cfg)
    train = cfg.get("train") or {}
    facts: dict[str, Any] = {
        "project": proj.root,
        "project_name": os.path.basename(proj.root.rstrip("\\/")),
        "device": str(train.get("device") or "auto"),
        "d_model": train.get("d_model"),
        "n_layer": train.get("n_layer"),
        "block_size": train.get("block_size"),
    }
    # 数据集 / 权重体量：给 AI 判断「够不够训」用
    try:
        import repo as repo_mod                             # 同目录桥模块：仓库清单
        items = repo_mod.summary({}) or {}
        ds = items.get("datasets") or []
        ws = items.get("weights") or []
        facts["datasets"] = len(ds)
        facts["dataset_bytes"] = sum(int(d.get("size_bytes") or d.get("bytes") or 0) for d in ds)
        # 仓库清单里权重的字节数键名是 size（数据集是 size_bytes）——别只认 size_bytes，
        # 否则每个权重都算成 0.0 MB，托管 AI 会误判「全是空占位符」直接中止（2026-10-18 实测踩到）
        facts["repo"] = _repo_audit()
        facts["weights"] = [{"name": w.get("name"),
                             "mb": round(int(w.get("size") or w.get("size_bytes") or w.get("bytes") or 0) / 1048576.0, 2)}
                            for w in ws[:12]]
    except Exception:                                        # noqa: BLE001
        pass
    try:
        from core import backend_marker
        mark = backend_marker.read() or {}
        facts["backend"] = mark.get("backend")
        facts["backend_name"] = mark.get("xpu_name") or mark.get("cuda_name") or ""
    except Exception:                                        # noqa: BLE001
        pass
    return facts


# ---------------------------------------------------------------- 预估（本地算，不靠 AI 编数）

def _dataset_bytes(facts: dict[str, Any]) -> int:
    return int(facts.get("dataset_bytes") or 0)


def _param_count(facts: dict[str, Any]) -> int:
    """粗算参数：按 config 的 d_model / n_layer（GPT 结构：每层 attn 4d² + ffn 8d²，词表另算）。"""
    d = int(facts.get("d_model") or 128)
    n = int(facts.get("n_layer") or 4)
    return max(1, n * (4 * d * d + 8 * d * d))


def _estimate(plan: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    dist = plan.get("dataset") or {}
    web = plan.get("web") or {}
    tr = plan.get("train") or {}
    count = int(dist.get("count") or 0)
    rounds = int(web.get("rounds") or 0) if web.get("enabled") else 0
    queries = int(web.get("queries_per_round") or 4)
    pages = int(web.get("pages_per_query") or 3)
    target_chars = int(web.get("target_chars") or 6000)
    steps = int(tr.get("steps") or 0)
    batch = int(tr.get("batch_size") or 32)

    # token：蒸馏每条 ≈ 3 次调用（出题/答题/审阅）≈ 1.6k token；网页整理按目标字数折算 ≈ chars/1.5
    tok_dist = count * 1600
    tok_web = int(target_chars / 1.5) * max(1, rounds)
    tok_gate = 4000                                   # 每次切换一次 AI 分析（约 8 次封顶）
    tok = tok_dist + tok_web + tok_gate * (3 + rounds)

    # 存储：数据集文本 + 网页纯文本 + 权重（4 字节/参数，LoRA 只存适配器约 2%）
    ds_mb = count * 1.5 / 1024.0
    web_mb = target_chars * max(1, rounds) / 1024.0 / 1024.0
    params = _param_count(facts)
    kind = str(tr.get("kind") or "sft").lower()
    w_mb = (params * 4 / 1048576.0) * (0.02 if kind == "lora" else 1.0)
    disk_mb = round(ds_mb + web_mb + w_mb + 8, 1)      # +8MB 日志/中间文件

    # 内存/显存：权重 × 4 字节 × 1.35（与对话验证页同一口径）；训练额外 activations 约 batch×block×d×层数
    mem_mb = params * 4 / 1048576.0 * 1.35
    block = int(facts.get("block_size") or 128)
    d = int(facts.get("d_model") or 128)
    n = int(facts.get("n_layer") or 4)
    act_mb = batch * block * d * n * 4 / 1048576.0
    mem_peak_mb = round(mem_mb + act_mb, 1)

    # 时间：蒸馏与网页整理按模型吞吐估，训练按步数（步/秒 ≈ 40，CPU 再打折）
    backend = str(facts.get("backend") or "").lower()
    sps = 40.0 if backend in ("cuda", "xpu") else 12.0
    min_distill = count * 1.4 / max(1.0, float(dist.get("concurrency") or 3)) / 60.0   # 单条约 1.4s，按并发折算成分钟
    min_web = rounds * queries * pages * 2.2 / 60.0 if rounds else 0
    min_train = steps / sps / 60.0 if steps else 0
    total = min_distill + min_web + min_train + 2.0
    return {
        "minutes": max(0.1, round(total, 1)),
        "minutes_distill": round(min_distill, 1),
        "minutes_web": round(min_web, 1),
        "minutes_train": round(min_train, 1),
        "disk_mb": disk_mb,
        "mem_peak_mb": mem_peak_mb,
        "tokens": int(tok),
        "tokens_distill": int(tok_dist),
        "tokens_web": int(tok_web),
        "note": "按当前 config（d_model=%s / n_layer=%s / block=%s，设备 %s）粗算，实跑可能有出入"
                % (facts.get("d_model"), facts.get("n_layer"), facts.get("block_size"), facts.get("device")),
    }


# ---------------------------------------------------------------- 方案（AI）

PLAN_SYS = (
    "你是 EProject/EAI Studio 的「模型托管」规划专家。用户会描述他想要的对话模型（领域、风格、体量），"
    "你要判断需要造多少数据、要不要先抓网页、用什么训练参数。\n"
    "硬性规则：\n"
    "1) 只能从 0 开始训练：train.kind 固定 \"pretrain\"，train.base 固定为空字符串；"
    "不许 continue / sft / lora，也不许基于任何已有权重继续训练。\n"
    "2) 先看用户消息里的「仓库体检」：usable=true 的是够量的现成数据集。"
    "若其中一个正好合适，dataset.reuse 填它名字、dataset.count 填它的条数，web.enabled=false、不再蒸馏；"
    "否则 dataset.reuse 留空字符串，按需蒸馏新数据。\n"
    "只输出一个 JSON 对象，不要任何解释文字，字段如下：\n"
    '{"summary":"一句话方案说明",'
    '"dataset":{"reuse":"","topic":"数据蒸馏的主题（可多行）","count":200,"concurrency":3,"name":"数据集名"},'
    '"web":{"enabled":true,"topic":"网页整理主题","rounds":3,"queries_per_round":4,'
    '"pages_per_query":3,"target_chars":6000,"out_name":"网页整理输出名"},'
    '"train":{"kind":"pretrain","steps":3000,"lr":0.0005,"batch_size":32,"base":""},'
    '"validate":{"samples":6},'
    '"risks":["风险提示1","风险提示2"]}\n'
    "约束：count 50~2000；steps 200~30000；数据量小（<500 条）就别给太大的 steps；"
    "网页整理只在领域知识需要外部资料时开。"
)

def _plan_summary(plan: dict[str, Any]) -> str:
    """按参数重写「说明」，保证说明里写的步骤和实际启用的一致（用户 2026-10-18）。"""
    ds = plan.get("dataset") or {}
    web = plan.get("web") or {}
    tr = plan.get("train") or {}
    va = plan.get("validate") or {}
    parts: list[str] = []
    if web.get("enabled"):
        parts.append("先抓取「%s」相关的网页资料（%s 轮 / 每轮 %s 个查询 / 每查询 %s 页）"
                     % (str(web.get("topic") or "")[:40], web.get("rounds"),
                        web.get("queries_per_round"), web.get("pages_per_query")))
    if str(ds.get("reuse") or "").strip():
        parts.append("直接复用仓库数据集「%s」（%s 条），不再蒸馏" % (ds.get("reuse"), ds.get("count")))
    else:
        parts.append("蒸馏约 %s 条问答对话数据" % ds.get("count"))
    parts.append("再从 0 开始预训练 %s 步（学习率 %s，batch %s，不加载任何已有权重）"
                 % (tr.get("steps"), tr.get("lr"), tr.get("batch_size")))
    parts.append("最后用 %s 条样本验证" % va.get("samples"))
    return "，".join(parts) + "。"


def _fix_summary(plan: dict[str, Any]) -> None:
    """AI 的说明是自由文本，可能和勾选的步骤打架：不一致就按参数重写（用户 2026-10-18）。"""
    text = str(plan.get("summary") or "").strip()
    web_on = bool((plan.get("web") or {}).get("enabled"))
    bad = False
    if not web_on and any(w in text for w in ("网页", "抓取", "爬取")):
        bad = True                                     # 没开网页整理却说抓网页
    if any(w in text for w in ("LoRA", "lora", "微调", "基座", "继续训练")):
        bad = True                                     # 托管只从 0 预训练
    if not text or bad:
        plan["summary"] = _plan_summary(plan)


def _sanitize_plan(plan: dict[str, Any], audit: dict[str, Any] | None = None) -> dict[str, Any]:
    """托管铁律 + 仓库复用归一化（方案生成、确认、恢复旧方案三处共用）。"""
    tr = plan.setdefault("train", {})
    tr["kind"] = "pretrain"
    tr["base"] = ""
    ds = plan.setdefault("dataset", {})
    ds.setdefault("reuse", "")
    audit = audit if audit is not None else _repo_audit()
    usable = {str(r.get("name")): r for r in (audit.get("usable") or [])}
    reuse = str(ds.get("reuse") or "").strip()
    if reuse not in usable:
        ds["reuse"] = ""
    else:
        ds["reuse"] = reuse
        ds["count"] = max(int(usable[reuse].get("lines") or 0), 1)
        plan.setdefault("web", {})["enabled"] = False
    plan["repo"] = {
        "usable": [{"name": r.get("name"), "lines": r.get("lines"), "kind": r.get("kind")}
                   for r in (audit.get("usable") or [])[:12]],
        "datasets": [{"name": r.get("name"), "lines": r.get("lines"), "usable": r.get("usable")}
                     for r in (audit.get("datasets") or [])[:20]],
        "error": audit.get("error") or "",
    }
    _fix_summary(plan)
    return plan

def _draft_plan(brief: str) -> dict[str, Any]:
    facts = _env_facts()
    repo = facts.get("repo") or {}
    usable = repo.get("usable") or []
    if usable:
        repo_txt = "；".join("%s（%d 条 / %s）" % (r.get("name"), int(r.get("lines") or 0), r.get("kind") or "?")
                            for r in usable[:8])
    elif (repo.get("datasets") or []):
        repo_txt = "有 %d 个数据集，但都不足 %d 条，不能直接用" % (len(repo["datasets"]), REPO_MIN_LINES)
    else:
        repo_txt = "仓库里还没有数据集"
    user = ("工程：%s\n设备：%s（%s）\n现有数据集 %s 个 / 共 %s KB\n现有权重（本次不会加载）：%s\n"
            "仓库体检（条数≥%d 才算能用）：%s\n\n用户想要的模型：\n%s"
            % (facts.get("project_name"), facts.get("device"), facts.get("backend") or "未知",
               facts.get("datasets", 0), int(_dataset_bytes(facts) / 1024),
               facts.get("weights") or "无", REPO_MIN_LINES, repo_txt, brief))
    reply = _ai(timeout=90.0, max_tokens=1200, temperature=0.3).chat(
        [{"role": "system", "content": PLAN_SYS}, {"role": "user", "content": user}])
    plan = _json_from(reply) or {}
    if not plan:
        raise RuntimeError("托管 AI 没返回可解析的方案（原始回复前 200 字：%s）" % (reply or "")[:200])
    plan.setdefault("summary", "")
    plan.setdefault("dataset", {})
    plan.setdefault("web", {})
    plan.setdefault("train", {})
    plan.setdefault("validate", {"samples": 6})
    plan.setdefault("risks", [])
    # 兜底：字段类型/范围收敛（AI 有时给字符串或离谱值）
    def _int(v, dflt, lo, hi):
        try:
            return max(lo, min(hi, int(float(str(v).strip()))))
        except Exception:                            # noqa: BLE001
            return dflt
    ds = plan["dataset"]
    ds["count"] = _int(ds.get("count"), 200, 20, 5000)
    ds["concurrency"] = _int(ds.get("concurrency"), 3, 1, 8)
    ds.setdefault("name", "")
    ds.setdefault("topic", "")
    web = plan["web"]
    web["enabled"] = bool(web.get("enabled"))
    web["rounds"] = _int(web.get("rounds"), 3, 1, 6)
    web["queries_per_round"] = _int(web.get("queries_per_round"), 4, 1, 12)
    web["pages_per_query"] = _int(web.get("pages_per_query"), 3, 1, 8)
    web["target_chars"] = _int(web.get("target_chars"), 6000, 400, 60000)
    tr = plan["train"]
    # 托管铁律（用户 2026-10-18）：不加载任何已有权重，从 0 开始预训练
    tr["kind"] = "pretrain"
    tr["base"] = ""

    tr["steps"] = _int(tr.get("steps"), 3000, 100, 60000)
    tr["batch_size"] = _int(tr.get("batch_size"), 32, 1, 128)
    try:
        tr["lr"] = float(tr.get("lr") or 0.0005)
    except Exception:                                # noqa: BLE001
        tr["lr"] = 0.0005
    tr["lora_r"] = _int(tr.get("lora_r"), 8, 1, 128)
    tr["lora_alpha"] = _int(tr.get("lora_alpha"), 16, 1, 256)
    # 复用仓库现成数据集 + 铁律归一化（用户 2026-10-18）
    _sanitize_plan(plan, repo)
    plan["estimate"] = _estimate(plan, facts)
    plan["facts"] = {"project": facts.get("project_name"), "device": facts.get("device"),
                     "backend": facts.get("backend"), "datasets": facts.get("datasets", 0)}
    return plan


# ---------------------------------------------------------------- 执行：各步的启动 / 等待

def _emit(task, kind: str, text: str, step: str = "", **extra) -> None:
    ts = time.strftime("%H:%M:%S")
    payload = {"text": text, "t": ts}
    payload.update(extra)
    _LOG_LINES.append("[%s] %s" % (ts, text))
    del _LOG_LINES[:-2000]                          # 只留最后 2000 行
    key = step or _CUR_STEP
    if key:
        bucket = _SUBSTEPS.setdefault(key, [])
        bucket.append("[%s] %s" % (ts, text))
        del bucket[:-400]
    task.emit(kind, payload)


def _step_distill(plan: dict[str, Any], task) -> dict[str, Any]:
    import response as resp
    ds = plan.get("dataset") or {}
    body = {
        "topics": str(ds.get("topic") or ""),
        "count": int(ds.get("count") or 200),
        "concurrency": int(ds.get("concurrency") or 3),
        "dataset_name": str(ds.get("name") or ""),
    }
    _stage_go("distill", 0, note="主题 %d 个字 · 目标 %s 条" % (len(body["topics"]), body["count"]))
    res = resp.collect_start(body) or {}
    if not res.get("ok"):
        _stage_fail("distill", res.get("error") or "启动失败")
        return {"ok": False, "error": res.get("error") or "启动失败"}
    if str(body.get("topics") or "").strip():
        _stage_go("distill", 2, note="主题由方案给定")     # 给了主题就不用 AI 现生成
    else:
        _stage_go("distill", 1, note="等待 AI 生成主题")
    return {"ok": True, "probe": lambda: _distill_progress()}


def _distill_progress() -> str:
    """进度指纹：采集统计里的条数（变化就说明没卡）；顺带把阶段话术喂给小步状态机。"""
    global _DIST_CUR
    try:
        import response as resp
        st = resp.collect_status({"since": _DIST_CUR}) or {}
        try:
            _DIST_CUR = int(st.get("next") or _DIST_CUR)
        except (TypeError, ValueError):
            pass
        for ev in (st.get("events") or []):
            if ev.get("kind") == "log":
                _scan_distill_log(str((ev.get("payload") or {}).get("text") or ""))
        # 真实进度：输出目录里的样本条数 + 事件总数（两个都在涨 = 没卡）
        return "samples=%s events=%s" % (st.get("samples"), st.get("total_events"))
    except Exception as exc:                         # noqa: BLE001
        return "read-error:%s" % exc


def _step_webcurate(plan: dict[str, Any], task) -> dict[str, Any]:
    import webcurate as wc
    web = plan.get("web") or {}
    body = {
        "topic": str(web.get("topic") or (plan.get("dataset") or {}).get("topic") or ""),
        "out_name": str(web.get("out_name") or ""),
        "queries_per_round": int(web.get("queries_per_round") or 4),
        "pages_per_query": int(web.get("pages_per_query") or 3),
        "max_rounds": int(web.get("rounds") or 3),
        "target_chars": int(web.get("target_chars") or 6000),
    }
    _stage_init("webcurate")
    _stage_go("webcurate", 0, note="主题 %d 个字" % len(body["topic"]))
    res = wc.api_start(body) or {}
    if not res.get("ok"):
        _stage_fail("webcurate", res.get("error") or "启动失败")
        return {"ok": False, "error": res.get("error") or "启动失败"}
    _stage_go("webcurate", 1, note="已开始检索")
    return {"ok": True, "probe": lambda: _web_progress()}


def _web_progress() -> str:
    global _WEB_CUR
    try:
        import webcurate as wc
        st = wc.api_status({"since": _WEB_CUR}) or {}
        try:
            _WEB_CUR = int(st.get("next") or _WEB_CUR)
        except (TypeError, ValueError):
            pass
        for ev in (st.get("events") or []):
            if ev.get("kind") == "log":
                _scan_web_log(str((ev.get("payload") or {}).get("text") or ""))
        return "events=%s running=%s" % (st.get("total_events"), st.get("running"))
    except Exception as exc:                         # noqa: BLE001
        return "read-error:%s" % exc


def _host_paths(plan: dict[str, Any]) -> dict[str, str]:
    """托管训练自己的产物路径（不改用户 config 里的 out/log 名，persist=False 只作用于本次）。"""
    tr = plan.get("train") or {}
    kind = str(tr.get("kind") or "sft").lower()
    proj = _host.project(_cfg())
    tag = "host_" + time.strftime("%Y%m%d_%H%M%S")
    key = "lora" if kind == "lora" else ("continue" if kind == "continue" else ("pretrain" if kind == "pretrain" else "sft"))
    return {
        "key": key,
        "out": os.path.join(proj.root, "checkpoints", "%s_%s.pt" % (tag, key)).replace(os.sep, "/"),
        "log": os.path.join(proj.logs_dir, "%s_%s.csv" % (tag, key)).replace(os.sep, "/"),
    }


def _step_train(plan: dict[str, Any], task) -> dict[str, Any]:
    tr = plan.get("train") or {}
    kind = str(tr.get("kind") or "sft").lower()
    stage, method = ("sft", "lora") if kind == "lora" else (kind, "full")
    global _TRAIN_LOG
    paths = _host_paths(plan)
    _TRAIN_LOG = paths["log"]
    tr["_out"], tr["_log"] = paths["out"], paths["log"]
    params: dict[str, Any] = {"batch_size": int(tr.get("batch_size") or 32)}
    # 训练页的键名是 <阶段>_steps / <阶段>_lr / <阶段>_out / <阶段>_log_csv
    suffix = {"pretrain": "pretrain", "continue": "continue", "sft": "sft", "lora": "lora"}[kind]
    params["%s_steps" % suffix] = int(tr.get("steps") or 3000)
    params["%s_lr" % suffix] = float(tr.get("lr") or 0.0005)
    params["%s_out" % suffix] = paths["out"]
    params["%s_log_csv" % suffix] = paths["log"]
    if kind == "lora":
        params["lora_r"] = int(tr.get("lora_r") or 8)
        params["lora_alpha"] = int(tr.get("lora_alpha") or 16)
    base = str(tr.get("base") or "").strip()
    if base:
        init_key = "continue_init_from" if kind == "continue" else ("pretrain_init_from" if kind == "pretrain" else "sft_init_from")
        if init_key != "pretrain_init_from":            # 预训练没有 init_from 这个表单键
            params[init_key] = base
    # 方案要复用仓库数据集时，预训练语料只合并那一个文件（用户 2026-10-18）
    reuse = str((plan.get("dataset") or {}).get("reuse") or "").strip()
    if reuse:
        try:
            import _corpus
            row = next((r for r in (_repo_audit().get("datasets") or [])
                        if str(r.get("name")) == reuse), None)
            if row and row.get("path"):
                _root = _host.project_root()
                _path = str(row["path"])
                _parent = os.path.dirname(_path)
                # 该数据集所在目录必须进扫描清单，否则合并拿不到它（用户 2026-10-18）
                _dirs = _corpus.scan_dirs(_root) or []
                if os.path.normcase(_parent) not in [os.path.normcase(d) for d in _dirs]:
                    _dirs = _dirs + [_parent]
                    with open(_corpus.dirs_file(_root), "w", encoding="utf-8") as _fh:
                        json.dump({"dirs": _dirs}, _fh, ensure_ascii=False, indent=2)
                _corpus.save_selection(_root, "pretrain", [_path])
                _emit(task, "sys", "[托管] 预训练语料只用数据集「%s」：%s" % (reuse, _path))
        except Exception as exc:                         # noqa: BLE001
            _emit(task, "warn", "[托管] 指定复用数据集失败（将按数据集页勾选合并）：%s" % exc)
    _stage_init("train")
    KIND_LABEL = {"pretrain": "预训练", "continue": "继续预训练",
                  "sft": "指令微调", "lora": "LoRA 微调"}
    _STAGE["train"] = [{"label": KIND_LABEL.get(kind, kind), "state": "run", "note": ""}]
    res = _host.start_training(stage, method, params, persist=False, owner="host",
                               runtime_tag="host") or {}
    if not res.get("ok"):
        _stage_fail("train", res.get("error") or "启动失败")
        return {"ok": False, "error": res.get("error") or "启动失败"}
    job = str(res.get("job") or "")
    plan["train"]["_job"] = job
    return {"ok": True, "job": job, "probe": lambda: _train_progress(job, paths["log"])}


def _train_progress(job_name: str, log_path: str) -> str:
    """进度指纹：日志 CSV 行数（每 step 一行）——行数变了就说明在动。"""
    try:
        path = str(log_path or "")
        if path and os.path.isfile(path):
            n = sum(1 for _ in open(path, "rb"))
            return "csv-lines=%d" % n
        j = _host.job(job_name) if job_name else None
        return "running=%s" % bool(j and j.running)
    except Exception as exc:                         # noqa: BLE001
        return "read-error:%s" % exc


def _read_tail(path: str, cap: int = 65536) -> str:
    """读文件尾部（日志卡用）：太大就只给最后 cap 字节，别把整个 CSV 塞给渲染层。"""
    try:
        path = str(path or "")
        if not path or not os.path.isfile(path):
            return ""
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > cap:
                f.seek(size - cap)
                f.readline()                        # 丢掉半行
            return f.read()
    except Exception:                               # noqa: BLE001
        return ""


def _read_loss_csv(path: str) -> dict[str, Any]:
    """读训练 loss CSV（表头 step,train_loss,val_loss,lr,elapsed）——别拿「每行最后一个数字」
    当 loss（那是 elapsed）。列缺失时才退回按数字顺序猜。"""
    rows: list[dict[str, float]] = []
    if not (path and os.path.isfile(path)):
        return {"rows": rows}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception:                                # noqa: BLE001
        return {"rows": rows}
    head = [h.strip().lower() for h in lines[0].split(",")] if lines else []
    def idx(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return -1
    i_step, i_tr, i_val = idx("step"), idx("train_loss", "loss"), idx("val_loss", "val")
    for ln in lines[1:]:
        parts = [x.strip() for x in ln.split(",")]
        try:
            row = {}
            if i_step >= 0:
                row["step"] = float(parts[i_step])
            if i_tr >= 0:
                row["train"] = float(parts[i_tr])
            if i_val >= 0:
                row["val"] = float(parts[i_val])
            if row:
                rows.append(row)
        except (ValueError, IndexError):
            continue
    return {"rows": rows[-600:]}


def _step_validate(plan: dict[str, Any], task) -> dict[str, Any]:
    """验证：读训练 loss 曲线 + 数据体量，交给托管 AI 判断「这个模型能不能用」。

    口径（用户 2026-10-17）：效果不佳**不自动重训**，只出结论；样本点不够就写「结论不确定」，
    不能把「日志太少」误报成「训练失败」。
    """
    tr = plan.get("train") or {}
    parsed = _read_loss_csv(str(tr.get("_log") or ""))
    rows = parsed.get("rows") or []
    trains = [r["train"] for r in rows if r.get("train") is not None]
    vals = [r["val"] for r in rows if r.get("val") is not None]
    facts = _env_facts()
    data = {
        "steps_logged": len(rows),
        "steps_planned": tr.get("steps"),
        "train_loss_last": trains[-1] if trains else None,
        "train_loss_min": min(trains) if trains else None,
        "train_loss_first": trains[0] if trains else None,
        "val_loss_last": vals[-1] if vals else None,
        "val_loss_min": min(vals) if vals else None,
        "datasets": facts.get("datasets"),
        "dataset_kb": int(_dataset_bytes(facts) / 1024),
        "kind": tr.get("kind"),
    }
    if len(rows) < 3:
        verdict = {"verdict": "unknown", "score": 0,
                   "reason": "训练日志只有 %d 个记录点，样本不足，无法判定好坏（多为步数太少或提前结束）"
                             % len(rows),
                   "next": "把步数调到能跨过 eval_every 的量再跑一轮"}
        return {"ok": True, "metrics": data, "verdict": verdict, "probe": None, "instant": True}
    verdict: dict[str, Any] = {}
    try:
        # 推理型模型（deepseek-flash 等）先吐 reasoning_content，max_tokens 给小了正文会被截断 → 给足
        reply = _ai(timeout=120.0, max_tokens=2048, temperature=0.2).chat([
            {"role": "system", "content":
             "你是模型训练验收员。根据 loss 曲线与数据体量判断这次训练是否值得用，只输出 JSON："
             '{"verdict":"pass 或 fail","score":0到1,"reason":"一两句中文理由",'
             '"next":"如果 fail 建议的最小改动（不要建议自动重训）"}'},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ])
        verdict = _json_from(reply) or {}
    except Exception as exc:                         # noqa: BLE001
        verdict = {"verdict": "unknown", "score": 0, "reason": "验证 AI 调用失败：%s" % exc}
    if not verdict.get("verdict"):
        verdict["verdict"] = "pass" if (trains and trains[-1] < 4.0) else "unknown"
    return {"ok": True, "metrics": data, "verdict": verdict, "probe": None, "instant": True}


# ---------------------------------------------------------------- AI 关卡（切任务前）

def _ask_gate(task, done_step: str, done_summary: str, next_step: str,
              plan: dict[str, Any], force: bool) -> None:
    """上一件事做完、要切下一件事时，叫托管 AI 判断能否继续（最多 2 分钟）。"""
    if _STOP.is_set():
        return
    prompt = (
        "托管任务刚完成一步，请判断是否继续下一步。\n"
        "已完成：%s —— %s\n下一步：%s\n整体方案：%s\n风险：%s\n"
        '只输出 JSON：{"decision":"continue 或 abort","reason":"一句话理由"}'
        % (STEP_LABEL.get(done_step, done_step), done_summary, STEP_LABEL.get(next_step, next_step),
           plan.get("summary") or "", "；".join(plan.get("risks") or []) or "—"))
    decision, reason = "continue", "AI 未答复，按继续处理"
    t0 = time.time()
    try:
        reply = _ai(timeout=AI_GATE_SECONDS, max_tokens=1600, temperature=0.1).chat([
            {"role": "system", "content": "你是托管训练的监督者，只在有确凿风险时中止（abort）。"},
            {"role": "user", "content": prompt}])
        obj = _json_from(reply) or {}
        decision = "abort" if str(obj.get("decision", "")).lower().startswith("abort") else "continue"
        reason = str(obj.get("reason") or "").strip() or reason
    except Exception as exc:                         # noqa: BLE001
        reason = "AI 分析失败（%s），按继续处理" % exc
    waited = round(time.time() - t0, 1)
    with _LOCK:
        _DECISIONS.append({"at": time.strftime("%H:%M:%S"), "after": done_step, "next": next_step,
                           "decision": decision, "reason": reason, "waited_s": waited,
                           "forced": bool(force and decision == "abort")})
    if decision == "abort" and force:
        _emit(task, "warn", "[托管] AI 想中止（%s），但已勾选「强制托管训练完成」→ 只做风险提示，继续 %s"
              % (reason, STEP_LABEL.get(next_step, next_step)))
        return
    if decision == "abort":
        _emit(task, "warn", "[托管] 托管 AI 中止：%s" % reason)
        _STOP.set()
        return
    _emit(task, "gate", "[托管] 托管 AI 放行（%ss）：%s → %s ｜ %s"
          % (waited, STEP_LABEL.get(done_step, done_step), STEP_LABEL.get(next_step, next_step), reason))


def _ask_stuck(task, step_key: str, last_progress: float) -> bool:
    """5 分钟没进度 → 问托管 AI 是不是真卡住；返回要不要停。"""
    if _STOP.is_set():
        return False
    try:
        reply = _ai(timeout=STALL_ASK_SECONDS, max_tokens=1200, temperature=0.1).chat([
            {"role": "system", "content": "你是托管训练的看门狗。判断任务是否真的卡死，只输出 JSON："
                                          '{"stuck":true 或 false,"reason":"一句话"}'},
            {"role": "user", "content": "[%s] 已经 %.0f 秒没有任何进度输出（训练/采集日志不增长）。"
                                        % (STEP_LABEL.get(step_key, step_key), time.time() - last_progress)}])
        obj = _json_from(reply) or {}
        stuck = bool(obj.get("stuck"))
        reason = str(obj.get("reason") or "")
    except Exception as exc:                         # noqa: BLE001
        stuck, reason = True, "卡死判定 AI 无应答（%s），按卡住处理" % exc
    _emit(task, "warn" if stuck else "gate",
          "[托管] 无进度超 5 分钟 → 托管 AI 判定：%s ｜ %s" % ("已卡住" if stuck else "仍在跑", reason))
    return stuck


# ---------------------------------------------------------------- 主流程

def _wait_step(task, key: str, probe: Optional[Callable[[], str]], instant: bool = False) -> str:
    """等一步跑完：返回 done / failed / stopped / stalled。顺带做 5 分钟卡死看门狗。"""
    if instant or probe is None:
        return "done"
    last_sig = ""
    last_change = time.time()
    asked = False
    while True:
        if _STOP.is_set():
            return "stopped"
        if _PAUSE.is_set():
            time.sleep(0.3)                         # 暂停：守住当前步，不推进、不判卡死
            last_change = time.time()
            continue
        time.sleep(POLL)
        if key == "train":
            j = _host.job(str(_CUR_JOB)) if _CUR_JOB else None
            running = bool(j and j.running)
            if not running:
                code = getattr(j, "exit_code", None) if j else None
                if code in (0, None):
                    return "done"
                # 失败：把子进程日志尾巴留进 _LAST_FAIL，报告里能直接看到原因（用户 2026-10-18）
                try:
                    lines = [str(x) for x in (j.lines if j else []) if str(x).strip()]
                except Exception:                    # noqa: BLE001
                    lines = []
                _set_fail("\n".join(lines[-3:]) if lines else "进程退出码 %s" % code)
                return "failed"
        elif key == "distill":
            if not _host.running_task("response_collect"):
                return "done"
        elif key == "webcurate":
            if not _host.running_task("collect"):
                return "done"
        try:
            sig = probe()
        except Exception as exc:                     # noqa: BLE001
            sig = "probe-error:%s" % exc
        if sig != last_sig:
            last_sig, last_change, asked = sig, time.time(), False
            _emit(task, "progress", "[%s] %s" % (STEP_LABEL.get(key, key), sig))
            continue
        if not asked and (time.time() - last_change) >= STALL_SECONDS:
            asked = True
            if _ask_stuck(task, key, last_change):
                _stop_current(key)
                return "stalled"


_CUR_JOB = ""


def _stop_current(key: str) -> None:
    try:
        if key == "train":
            _host.stop_training()
        elif key == "distill":
            import response as resp
            resp.collect_stop({})
        elif key == "webcurate":
            import webcurate as wc
            wc.api_stop({})
    except Exception:                                # noqa: BLE001
        pass


def _run(emit, task, group) -> None:
    global _REPORT, _ERROR, _FINISHED_AT, _CUR_JOB, _CUR_STEP
    plan = dict(_PLAN)
    force = bool(((_cfg().get("app") or {}).get("host_force_complete")))
    reuse = str((plan.get("dataset") or {}).get("reuse") or "").strip()
    steps: list[str] = []
    if not reuse:
        steps.append("distill")
    if (plan.get("web") or {}).get("enabled"):
        steps.append("webcurate")
    steps += ["train", "validate", "report"]
    _STEPS[:] = [{"key": k, "label": STEP_LABEL[k], "state": "pending"} for k in steps]
    _LOG_LINES[:] = []
    _SUBSTEPS.clear()
    _STAGE.clear()
    global _DIST_CUR, _WEB_CUR
    _DIST_CUR, _WEB_CUR = 0, 0
    _PAUSE.clear()
    _CUR_STEP = ""
    task.set_stats(steps=len(steps), current=0, state="running")
    _emit(task, "sys", "[托管] 开始：%s" % (plan.get("summary") or ""))
    repo_now = _repo_audit()
    if reuse:
        _emit(task, "sys", "[托管] 仓库体检：复用数据集「%s」（%d 条），本次不再蒸馏"
              % (reuse, int((next((r for r in (repo_now.get("usable") or [])
                                   if r.get("name") == reuse), {}) or {}).get("lines") or 0)))
    elif (repo_now.get("usable") or []):
        _emit(task, "sys", "[托管] 仓库体检：有 %d 个可用数据集，但方案选择重新蒸馏（%d 条）"
              % (len(repo_now["usable"]), int((plan.get("dataset") or {}).get("count") or 0)))
    else:
        _emit(task, "sys", "[托管] 仓库体检：没有可用数据集（%d 个都不足 %d 条），先蒸馏新数据"
              % (len(repo_now.get("datasets") or []), REPO_MIN_LINES))
    if force:
        _emit(task, "warn", "[托管] 已勾选「强制托管训练完成」：托管 AI 只能提示风险，不会中止（卡死除外）")
    summaries: dict[str, str] = {}
    try:
        for i, key in enumerate(steps):
            while _PAUSE.is_set() and not _STOP.is_set():
                time.sleep(0.3)
            if _STOP.is_set():
                break
            _CUR_STEP = key                      # 之后这一步里所有 _emit 都归到它名下（小步明细）
            for st in _STEPS:
                if st["key"] == key:
                    st["state"] = "running"
            task.set_stats(current=i + 1, step=key, state="running")
            _emit(task, "step", "[托管] 第 %d/%d 步：%s" % (i + 1, len(steps), STEP_LABEL[key]))
            _CUR_JOB = ""
            try:
                if key == "distill":
                    res = _step_distill(plan, task)
                elif key == "webcurate":
                    res = _step_webcurate(plan, task)
                elif key == "train":
                    res = _step_train(plan, task)
                    _CUR_JOB = str(res.get("job") or "")
                elif key == "validate":
                    res = _step_validate(plan, task)
                else:
                    res = {"ok": True, "instant": True}
            except Exception as exc:                 # noqa: BLE001
                res = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            if not res.get("ok"):
                _ERROR = str(res.get("error") or "未知错误")
                _stage_fail(key, _ERROR)
                _emit(task, "error", "[托管] %s 启动失败：%s" % (STEP_LABEL[key], _ERROR))
                for st in _STEPS:
                    if st["key"] == key:
                        st["state"] = "failed"
                break
            outcome = _wait_step(task, key, res.get("probe"), bool(res.get("instant")))
            if key == "validate":
                verdict = res.get("verdict") or {}
                summaries["validate"] = "结论 %s（%s）" % (verdict.get("verdict"), verdict.get("reason"))
                _emit(task, "verdict", "[验证] %s → %s ｜ %s" % (
                    json.dumps(res.get("metrics"), ensure_ascii=False),
                    verdict.get("verdict"), verdict.get("reason")))
            if outcome == "stalled":
                _ERROR = "%s 无进度超 5 分钟，托管 AI 判定卡住并已停止" % STEP_LABEL[key]
                _stage_fail(key, "无进度超 5 分钟")
                for st in _STEPS:
                    if st["key"] == key:
                        st["state"] = "stopped"
                break
            if outcome == "stopped":
                _stage_fail(key, "已取消")
                for st in _STEPS:
                    if st["key"] == key:
                        st["state"] = "stopped"
                break
            if outcome == "failed":
                _ERROR = "%s 进程异常退出" % STEP_LABEL[key]
                _stage_fail(key, "进程异常退出（退出码非 0）")
                if _LAST_FAIL:
                    _ERROR += "：" + _LAST_FAIL
                for st in _STEPS:
                    if st["key"] == key:
                        st["state"] = "failed"
                break
            _stage_all(key)
            for st in _STEPS:
                if st["key"] == key:
                    st["state"] = "done"
            summaries[key] = summaries.get(key) or ("完成")
            _emit(task, "done-step", "[托管] %s 完成" % STEP_LABEL[key])
            next_key = steps[i + 1] if i + 1 < len(steps) else ""
            if next_key:
                _ask_gate(task, key, summaries.get(key, "完成"), next_key, plan, force)
                if _STOP.is_set():
                    break
        # 收尾：生成结论
        _REPORT = _make_report(plan, summaries, _ERROR)
        try:
            path = _save_report(_REPORT)
            _emit(task, "report", "[托管] 结论已保存：%s" % path)
        except Exception as exc:                     # noqa: BLE001
            _emit(task, "warn", "[托管] 结论保存失败：%s" % exc)
        state = "stopped" if _STOP.is_set() else ("failed" if _ERROR else "done")
        task.set_stats(state=state)
        _emit(task, "sys", "[托管] 结束：%s" % ("已停止" if state == "stopped" else ("失败" if state == "failed" else "完成")))
    finally:
        _FINISHED_AT = time.time()
        _save_last()


def _make_report(plan: dict[str, Any], summaries: dict[str, str], error: str) -> str:
    lines = ["# 托管结论", "", "## 方案", plan.get("summary") or "",
             "", "## 预估（开工前）"]
    est = plan.get("estimate") or {}
    lines.append("- 预计耗时 %s 分钟（蒸馏 %s / 网页 %s / 训练 %s）"
                 % (est.get("minutes"), est.get("minutes_distill"), est.get("minutes_web"), est.get("minutes_train")))
    lines.append("- 预计存储 %s MB · 峰值内存/显存 %s MB · token 约 %s"
                 % (est.get("disk_mb"), est.get("mem_peak_mb"), est.get("tokens")))
    lines += ["", "## 步骤"]
    for st in _STEPS:
        lines.append("- %s：%s" % (st["label"], {"done": "完成", "failed": "失败",
                                                 "stopped": "被停止", "pending": "未执行",
                                                 "running": "运行中"}.get(st["state"], st["state"])))
    if summaries.get("validate"):
        lines += ["", "## 验证", summaries["validate"]]
    if _DECISIONS:
        lines += ["", "## 托管 AI 的决策"]
        for d in _DECISIONS:
            lines.append("- %s 之后 → %s：%s（%ss）%s" % (STEP_LABEL.get(d["after"], d["after"]),
                                                        STEP_LABEL.get(d["next"], d["next"]),
                                                        d["decision"], d["waited_s"],
                                                        "［强制模式：仅提示］" if d["forced"] else ""))
            lines.append("  - 理由：%s" % d["reason"])
    if error:
        lines += ["", "## 问题", error]
    return "\n".join(lines)


def _last_path() -> str:
    proj = _host.project(_cfg())
    d = os.path.join(proj.root, "host")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "last_run.json")


def _save_last() -> None:
    """上一次托管的结果落盘（用户 2026-10-18）：重启 App 后页面还能看到方案/步骤/结论。"""
    try:
        data = {"plan": _PLAN, "steps": _STEPS, "decisions": _DECISIONS,
                "report": _REPORT, "error": _ERROR, "train_log": _TRAIN_LOG,
                "log": _LOG_LINES[-2000:],
                "substeps": {k: v[-400:] for k, v in _SUBSTEPS.items()},
                "stages": _STAGE,
                "started_at": _STARTED_AT, "finished_at": _FINISHED_AT}
        with open(_last_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:                                # noqa: BLE001
        pass


def reset_state() -> None:
    """清空托管内存态（切/建工程时调用，用户 2026-10-18）。

    不清的话，新工程页面还会显示上一个工程的方案、进度、AI 决策、运行日志。
    """
    global _PLAN, _REPORT, _ERROR, _STARTED_AT, _FINISHED_AT, _TRAIN_LOG, _STATE_ROOT
    global _CUR_STEP, _CUR_JOB, _DIST_CUR, _WEB_CUR
    with _LOCK:
        _PLAN.clear()
        _STEPS.clear()
        _DECISIONS.clear()
        _LOG_LINES.clear()
        _SUBSTEPS.clear()
        _STAGE.clear()
        _REPORT = ""
        _ERROR = ""
        _TRAIN_LOG = ""
        _STARTED_AT = 0.0
        _FINISHED_AT = 0.0
        _CUR_STEP = ""
        _CUR_JOB = ""
        _DIST_CUR = 0
        _WEB_CUR = 0
        _STATE_ROOT = ""


def _load_last() -> None:
    global _PLAN, _STEPS, _DECISIONS, _REPORT, _ERROR, _STARTED_AT, _FINISHED_AT, _TRAIN_LOG, _STATE_ROOT
    try:                                          # 换工程了：旧工程的状态不能带过来（用户 2026-10-18）
        root = os.path.normcase(os.path.abspath(_host.project(_cfg()).root))
        if _STATE_ROOT and _STATE_ROOT != root:
            reset_state()
    except Exception:                             # noqa: BLE001
        pass
    with _LOCK:
        if _PLAN or _REPORT:
            return
        try:
            with open(_last_path(), "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:                            # noqa: BLE001
            return
        _PLAN = data.get("plan") or {}
        if _PLAN:
            _sanitize_plan(_PLAN)                      # 旧方案也照新规则显示（2026-10-18）
        _STEPS[:] = data.get("steps") or []
        _TRAIN_LOG = str(data.get("train_log") or "")
        _LOG_LINES[:] = data.get("log") or []
        _SUBSTEPS.clear()
        for k, v in (data.get("substeps") or {}).items():
            _SUBSTEPS[str(k)] = list(v or [])
        _STAGE.clear()
        for k, v in (data.get("stages") or {}).items():
            _STAGE[str(k)] = [dict(r) for r in (v or [])]
        _DECISIONS[:] = data.get("decisions") or []
        _REPORT = str(data.get("report") or "")
        _ERROR = str(data.get("error") or "")
        _STARTED_AT = float(data.get("started_at") or 0)
        _FINISHED_AT = float(data.get("finished_at") or 0)
        try:
            _STATE_ROOT = os.path.normcase(os.path.abspath(_host.project(_cfg()).root))
        except Exception:                         # noqa: BLE001
            _STATE_ROOT = ""


def _save_report(text: str) -> str:
    proj = _host.project(_cfg())
    d = os.path.join(proj.root, "host")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "report_%s.md" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ---------------------------------------------------------------- 接口

def state(query: dict[str, Any]) -> dict[str, Any]:
    since = int(query.get("since") or 0)
    _load_last()
    st = _host.task_status(TASK_NAME, since) or {}
    with _LOCK:
        plan = dict(_PLAN) if _PLAN else None
        steps = [dict(s) for s in _STEPS]
        decisions = list(_DECISIONS)
        report = _REPORT
    app_cfg = _cfg().get("app") or {}
    return {
        "ok": True,
        "running": bool(st.get("running")),
        "exists": bool(st.get("exists")),
        "phase": (st.get("stats") or {}).get("state") or ("running" if st.get("running") else "idle"),
        "stats": st.get("stats") or {},
        "events": st.get("events") or [],
        "next": st.get("next") or 0,
        "plan": plan,
        "steps": steps,
        "decisions": decisions,
        "report": report,
        "paused": _PAUSE.is_set(),
        "substeps": {k: list(v) for k, v in _SUBSTEPS.items()},
        "stages": {k: [dict(r) for r in v] for k, v in _STAGE.items()},
        "train_log": _read_tail(_TRAIN_LOG),
        "log": list(_LOG_LINES),
        "train_log_path": _TRAIN_LOG,
        "error": _ERROR,
        "force": bool(app_cfg.get("host_force_complete")),
        "api": {"base_url": _default_api()["base_url"], "model": _default_api()["model"],
                "key_set": bool(_default_api()["api_key"])},
        "started_at": _STARTED_AT,
        "finished_at": _FINISHED_AT,
    }


def plan_draft(body: dict[str, Any]) -> dict[str, Any]:
    global _PLAN
    brief = str(body.get("brief") or "").strip()
    if not brief:
        return {"ok": False, "error": "请先描述你想要的对话模型"}
    try:
        plan = _draft_plan(brief)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    plan["brief"] = brief
    with _LOCK:
        _PLAN = plan
    return {"ok": True, "plan": plan}


def confirm(body: dict[str, Any]) -> dict[str, Any]:
    """用户审阅完（可能改过字段）→ 开跑。改过的方案重新算预估。"""
    global _PLAN, _REPORT, _ERROR, _STARTED_AT, _FINISHED_AT
    if _host.running_task(GROUP):
        return {"ok": False, "error": "托管已经在运行"}
    plan = body.get("plan")
    if not isinstance(plan, dict) or not plan:
        with _LOCK:
            plan = dict(_PLAN)
    if not plan:
        return {"ok": False, "error": "还没有方案，请先生成并审阅"}
    # 审阅里可能改了数值 → 重新收敛 + 重算预估
    plan.setdefault("dataset", {})
    plan.setdefault("web", {})
    plan.setdefault("train", {})
    plan.setdefault("validate", {"samples": 6})
    # 托管铁律 + 仓库复用归一化（用户 2026-10-18）
    _sanitize_plan(plan)
    plan["estimate"] = _estimate(plan, _env_facts())
    with _LOCK:
        _PLAN = plan
        _DECISIONS.clear()
        _REPORT = ""
        _ERROR = ""
        _set_fail("")
        _STARTED_AT = time.time()
        _FINISHED_AT = 0.0
    _STOP.clear()
    try:
        _host.start_task(TASK_NAME, _run, stop=lambda: _STOP.set(), group=GROUP)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "plan": plan}


def stop(_body: dict[str, Any]) -> dict[str, Any]:
    _STOP.set()
    for key in ("train", "distill", "webcurate"):
        _stop_current(key)
    return {"ok": True, "note": "已请求停止托管（当前任务会一并停）"}


def estimate(body: dict[str, Any]) -> dict[str, Any]:
    plan = body.get("plan") if isinstance(body.get("plan"), dict) else _PLAN
    if not plan:
        return {"ok": False, "error": "没有方案"}
    return {"ok": True, "estimate": _estimate(plan, _env_facts())}


def pause(query: dict[str, Any]) -> dict[str, Any]:
    """暂停托管（用户 2026-10-18）：不再推进下一步、不做卡死判定；当前这一步继续跑到完。"""
    _PAUSE.set()
    st = _host.task_status(TASK_NAME, 0) or {}
    if st.get("running"):
        _log_only("[托管] 已暂停：当前这一步跑完后停住，不再推进下一步")
    return {"ok": True, "paused": True, "running": bool(st.get("running"))}


def resume(query: dict[str, Any]) -> dict[str, Any]:
    """继续托管（用户 2026-10-18）。"""
    was = _PAUSE.is_set()
    _PAUSE.clear()
    st = _host.task_status(TASK_NAME, 0) or {}
    if was and st.get("running"):
        _log_only("[托管] 已继续：按原方案往下推进")
    return {"ok": True, "paused": False, "running": bool(st.get("running"))}


def _log_only(text: str) -> None:
    """只写日志行（没有 task 可 emit 时用，比如暂停/继续）。"""
    ts = time.strftime("%H:%M:%S")
    _LOG_LINES.append("[%s] %s" % (ts, text))
    del _LOG_LINES[:-2000]
    if _CUR_STEP:
        bucket = _SUBSTEPS.setdefault(_CUR_STEP, [])
        bucket.append("[%s] %s" % (ts, text))
        del bucket[:-400]


def running_status(query: dict[str, Any]) -> dict[str, Any]:
    """轻量「托管在跑吗」（用户 2026-10-18）：导航灰化每 2s 轮询用，别带 steps/report/train_log 那一坨。"""
    st = _host.task_status(TASK_NAME, 0) or {}
    running = bool(st.get("running"))
    return {
        "ok": True,
        "running": running,
        "paused": _PAUSE.is_set(),
        "phase": (st.get("stats") or {}).get("state") or ("running" if running else "idle"),
        "step": (st.get("stats") or {}).get("step") or "",
    }


_SIZE_CACHE: dict[str, Any] = {"path": "", "at": 0.0, "mb": 0.0}


def _dir_mb(path: str, ttl: float = 60.0) -> float:
    """工程目录占用（MB）：60 秒缓存，避免每次轮询都全盘 walk（用户 2026-10-18）。"""
    now = time.time()
    if _SIZE_CACHE.get("path") == path and (now - float(_SIZE_CACHE.get("at") or 0)) < ttl:
        return float(_SIZE_CACHE.get("mb") or 0.0)
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    mb = round(total / 1048576.0, 1)
    _SIZE_CACHE.update({"path": path, "at": now, "mb": mb})
    return mb


def _eai_mem_mb() -> Optional[float]:
    """EAI 这个 App 占多少内存：CDP 调试端口那个 Electron 主进程的整棵树 + 桥进程自己。"""
    try:
        import psutil
    except Exception:                                    # noqa: BLE001
        return None
    procs: list[Any] = []
    try:
        for p in psutil.process_iter(["pid", "cmdline"]):
            cmd = p.info.get("cmdline") or []
            if any(str(a).startswith("--remote-debugging-port") for a in cmd):
                procs.append(p)
                try:
                    procs.extend(p.children(recursive=True))
                except Exception:                        # noqa: BLE001
                    pass
    except Exception:                                    # noqa: BLE001
        pass
    try:
        me = psutil.Process(os.getpid())
        procs.append(me)
        procs.extend(me.children(recursive=True))
    except Exception:                                    # noqa: BLE001
        pass
    seen: set[int] = set()
    total = 0
    for p in procs:
        try:
            if p.pid in seen:
                continue
            seen.add(p.pid)
            total += p.memory_info().rss
        except Exception:                                # noqa: BLE001
            continue
    return round(total / 1048576.0, 1) if seen else None


def machine_status(query: dict[str, Any]) -> dict[str, Any]:
    """本机状态（用户 2026-10-18）：EAI 占用 / 本机剩余内存 / 训练设备 / 本工程占用。"""
    cfg = _cfg()
    tr = cfg.get("train") or {}
    free_mb = total_mb = None
    try:
        import psutil
        vm = psutil.virtual_memory()
        free_mb = round(vm.available / 1048576.0, 1)
        total_mb = round(vm.total / 1048576.0, 1)
    except Exception:                                    # noqa: BLE001
        pass
    root = ""
    project_mb = 0.0
    try:
        root = _host.project(cfg).root
        project_mb = _dir_mb(root)
    except Exception:                                    # noqa: BLE001
        pass
    return {
        "ok": True,
        "eai_mb": _eai_mem_mb(),
        "free_mb": free_mb,
        "total_mb": total_mb,
        "device": str(tr.get("device") or "cpu"),
        "project_mb": project_mb,
        "project_root": root,
        "ts": time.time(),
    }


def register(router) -> None:
    router.get("/api/host/state", state)
    router.get("/api/host/machine", machine_status)
    router.get("/api/host/running", running_status)
    router.get("/api/train/running", lambda query=None: _host.train_running())
    router.post("/api/host/pause", pause)
    router.post("/api/host/resume", resume)
    router.post("/api/host/plan", plan_draft)
    router.post("/api/host/confirm", confirm)
    router.post("/api/host/stop", stop)
    router.post("/api/host/estimate", estimate)
