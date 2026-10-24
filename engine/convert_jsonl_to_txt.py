"""把蒸馏 JSONL 转成训练用 TXT。

- 含 article 字段 → 纯正文 txt（预训练）
- 含 question/answer → \\x01问\\x02答\\x03 txt（微调）

用法：
  python convert_jsonl_to_txt.py
  python convert_jsonl_to_txt.py --src D:\\EAIPro\\Data_Article --mode article
  python convert_jsonl_to_txt.py --src D:\\EAIPro\\Data_ZL --mode pair
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

Q_MARK, A_MARK, E_MARK = "\x01", "\x02", "\x03"


def detect_mode(obj: dict) -> str:
    if obj.get("article") is not None:
        return "article"
    if obj.get("question") is not None:
        return "pair"
    return "skip"


def convert_file(path: str, mode: str | None = None) -> tuple[str, int, int]:
    """返回 (输出路径, 条数, 跳过数)。"""
    base = os.path.splitext(os.path.basename(path))[0]
    # 去掉 _rejected 后缀干扰
    out_dir = os.path.join(os.path.dirname(path), "txt")
    os.makedirs(out_dir, exist_ok=True)

    articles: list[str] = []
    pairs: list[str] = []
    skipped = 0
    n = 0

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            m = mode or detect_mode(obj)
            if m == "article":
                body = str(obj.get("article") or "").strip()
                if not body:
                    skipped += 1
                    continue
                articles.append(body)
                n += 1
            elif m == "pair":
                q = str(obj.get("question") or "").strip()
                a = str(obj.get("answer") or "").strip()
                if not q or not a:
                    skipped += 1
                    continue
                pairs.append(f"{Q_MARK}{q}{A_MARK}{a}{E_MARK}")
                n += 1
            else:
                skipped += 1

    if articles and not pairs:
        out = os.path.join(out_dir, f"{base}_articles.txt")
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            for body in articles:
                f.write(body)
                f.write("\n\n")
        return out, n, skipped
    if pairs and not articles:
        out = os.path.join(out_dir, f"{base}_sft.txt")
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            for line in pairs:
                f.write(line)
                f.write("\n")
        return out, n, skipped
    if articles and pairs:
        out_a = os.path.join(out_dir, f"{base}_articles.txt")
        out_p = os.path.join(out_dir, f"{base}_sft.txt")
        with open(out_a, "w", encoding="utf-8", newline="\n") as f:
            for body in articles:
                f.write(body)
                f.write("\n\n")
        with open(out_p, "w", encoding="utf-8", newline="\n") as f:
            for line in pairs:
                f.write(line)
                f.write("\n")
        return out_a + " + " + out_p, n, skipped
    return "", n, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"D:\EAIPro\Data_Article",
                    help="JSONL 所在目录或单个 .jsonl 文件")
    ap.add_argument("--mode", choices=["article", "pair", "auto"], default="auto")
    args = ap.parse_args()

    src = args.src
    if os.path.isfile(src):
        files = [src]
    else:
        files = sorted(glob.glob(os.path.join(src, "*.jsonl")))
        files = [p for p in files if "rejected" not in os.path.basename(p).lower()]

    if not files:
        print(f"未找到 JSONL：{src}")
        return 1

    mode = None if args.mode == "auto" else args.mode
    total_n = 0
    for path in files:
        out, n, skipped = convert_file(path, mode)
        total_n += n
        print(f"{os.path.basename(path)}  →  {out or '(无输出)'}  条数={n} 跳过={skipped}")

    print(f"完成，共写出 {total_n} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
