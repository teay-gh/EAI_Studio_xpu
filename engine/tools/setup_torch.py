"""EAI 训练后端环境：安装 / 切换 / 校验（XPU · CUDA · CPU）。

为什么要这个脚本
    PyTorch 官方没有「XPU + CUDA 二合一」的轮子：
      · 想要 Intel 核显/Arc 训练 → 必须用 +xpu 构建（不含 CUDA）
      · 想要 N 卡训练           → 必须用 cuXXX 构建（不含 XPU）
    所以「装好 XPU 和 CUDA 环境」= 让这个脚本能一键切成任一种，并把结果记进
    torch_backend.json，应用设置页据此显示当前环境到底是哪一种后端。

用法
    python tools/setup_torch.py --check                  # 只报告当前环境，不改动
    python tools/setup_torch.py --backend cuda --dry-run  # 只解析并打印将执行的命令
    python tools/setup_torch.py --backend xpu            # 真装（会下载数百 MB~GB）
    python tools/setup_torch.py --backend auto           # 按本机显卡自动挑
    python tools/setup_torch.py --backend cpu            # 回到 CPU 构建

要点
    · 安装后用**新进程**复验（当前进程里 torch 已加载，测不出新装的包）。
    · 版本默认沿用当前已装的 torch 版本，避免换后端顺带升/降版本。
    · 找不到该版本的轮子时给出候选，不静默换版本。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.dirname(HERE)                      # ENGINE_DIR/
ROOT = os.path.dirname(ENGINE_DIR)                      # EAI/
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
MARKER = os.path.join(ENGINE_DIR, "torch_backend.json")

INDEX = {
    "cpu": "https://download.pytorch.org/whl/cpu",
    "xpu": "https://download.pytorch.org/whl/xpu",
}
CUDA_INDEXES = ("cu130", "cu129", "cu128", "cu126")   # 依次尝试，谁有对应版本就用谁
_UA = {"User-Agent": "EAI-setup-torch/1.0"}


def py() -> str:
    return VENV_PY if os.path.exists(VENV_PY) else sys.executable


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


# ---------- 环境探测（都在子进程里跑，保证读到的是磁盘上的包） ----------

CHECK_SRC = r"""
import json, torch
info = {"torch": torch.__version__, "cuda_build": torch.version.cuda or "",
        "has_xpu": hasattr(torch, "xpu"), "xpu": False, "cuda": False,
        "xpu_name": "", "cuda_name": "", "xpu_arch": [], "device_count": 0}
try:
    if info["has_xpu"]:
        info["xpu"] = bool(torch.xpu.is_available())
        info["xpu_arch"] = list(torch.xpu.get_arch_list())
        if info["xpu"]:
            info["device_count"] = torch.xpu.device_count()
            info["xpu_name"] = torch.xpu.get_device_name(0)
except Exception as e:
    info["xpu_err"] = f"{type(e).__name__}: {e}"
try:
    info["cuda"] = bool(torch.cuda.is_available())
    if info["cuda"]:
        info["cuda_name"] = torch.cuda.get_device_name(0)
except Exception as e:
    info["cuda_err"] = f"{type(e).__name__}: {e}"
print(json.dumps(info, ensure_ascii=False))
"""


def probe() -> dict:
    r = run([py(), "-c", CHECK_SRC])
    out = (r.stdout or "").strip().splitlines()
    if not out:
        return {"error": (r.stderr or "探测失败")[-400:]}
    return json.loads(out[-1])


def gpu_names() -> list[str]:
    """本机显卡名（Windows WMI；非 Windows 返回空）。"""
    if os.name != "nt":
        return []
    r = run(["powershell.exe", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController).Name"])
    return [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]


def guess_backend() -> str:
    names = " ".join(gpu_names()).lower()
    if "nvidia" in names or "geforce" in names or "quadro" in names:
        return "cuda"
    if "intel" in names and ("arc" in names or "graphics" in names):
        return "xpu"
    return "cpu"


# ---------- 轮子解析 ----------

def list_wheels(index_url: str, pkg: str = "torch") -> list[str]:
    try:
        with urllib.request.urlopen(f"{index_url}/{pkg}/", timeout=45) as f:  # noqa: S310
            html = f.read().decode("utf-8", "replace")
    except Exception:
        return []
    return sorted(set(re.findall(r"torch-[0-9][0-9.]*(?:\+[a-z0-9]+)?-cp3\d+-cp3\d+[a-z]*-win_amd64\.whl",
                                html)))


def python_tag() -> str:
    """按 venv 里那个 python 的版本算 wheel 标签（用户 2026-10-20）。

    原来用 sys.version_info = **正在跑这个脚本的解释器**。若用系统 python（比如 3.11）
    去跑脚本、而 .venv 是 3.13，就会挑到 cp311 的轮子，uv 装上去要么失败要么装错版本。
    """
    out = (run([py(), "-c", "import sys;print('cp%d%d' % sys.version_info[:2])"]).stdout or "").strip()
    tag = out.splitlines()[-1].strip() if out else ""
    if re.fullmatch(r"cp3\d+", tag):
        return tag
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def resolve(backend: str, version: str | None) -> dict:
    """返回 {index, spec, wheel, candidates}；找不到就 candidates 里给候选。"""
    tag = python_tag()
    if backend in ("cpu", "xpu"):
        wheels = list_wheels(INDEX[backend])
        return _pick(wheels, INDEX[backend], backend, version, tag)
    for name in CUDA_INDEXES:
        idx = f"https://download.pytorch.org/whl/{name}"
        wheels = list_wheels(idx)
        hit = _pick(wheels, idx, name, version, tag)
        if hit.get("wheel"):
            return hit
    return {"index": "", "spec": "", "wheel": "", "candidates": [], "error": "所有 CUDA 索引都没有匹配轮子"}


def _pick(wheels: list[str], index: str, tag_backend: str, version: str | None, tag: str) -> dict:
    local = {"cpu": "cpu", "xpu": "xpu"}.get(tag_backend, "")
    pool = [w for w in wheels if f"-{tag}-{tag}-" in w] or [w for w in wheels if f"-{tag}" in w]
    if version:
        want = [w for w in pool if w.startswith(f"torch-{version}")]
        if not want and local:
            want = [w for w in pool if w.startswith(f"torch-{version}+{local}")]
        pool = want or pool
    # 版本号按数字排序取最新
    def key(w: str):
        m = re.match(r"torch-([0-9.]+)(?:\+(\w+))?", w)
        return [int(x) for x in (m.group(1).rstrip(".").split("."))] if m else [0]
    pool.sort(key=key)
    wheel = pool[-1] if pool else ""
    ver = re.match(r"torch-([0-9.]+(?:\+\w+)?)", wheel).group(1) if wheel else ""
    return {"index": index, "spec": f"torch=={ver}" if ver else "", "wheel": wheel,
            "candidates": pool[-4:], "backend": tag_backend}


# ---------- 安装 / 报告 ----------

def _have_uv() -> bool:
    try:
        return subprocess.call(["uv", "--version"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except Exception:                              # noqa: BLE001
        return False


def install(spec: dict, dry: bool) -> int:
    if not spec.get("wheel"):
        print("✗ 没解析到可用轮子：", spec.get("error") or spec.get("candidates"))
        return 2
    tail = ["--index-url", spec["index"],
            # 依赖包（intel-sycl-rt / mkl / onemkl-* 等）优先走国内镜像，官方源兜底
            "--extra-index-url", "https://pypi.tuna.tsinghua.edu.cn/simple",
            "--extra-index-url", "https://pypi.org/simple"]
    if _have_uv():
        cmd = ["uv", "pip", "install", "--python", py(), spec["spec"]] + tail + [
            # uv 默认只认「第一个含该包的索引」= PyPI，于是 +xpu/+cuXXX 这类本地标签版本永远解析不到；
            # 官方建议用 unsafe-best-match 让它在所有索引里挑版本。
            "--index-strategy", "unsafe-best-match"]
    else:
        # 没装 uv 就用 venv 自己的 pip（用户 2026-10-20：原来直接 FileNotFoundError）
        # pip 没有 --index-strategy，但它会跨索引挑最高版本；spec 里带了 +cu130/+xpu 本地标签，
        # PyPI 上没有同名版本，因此只会命中官方轮子索引。
        cmd = [py(), "-m", "pip", "install", spec["spec"]] + tail
    print("将执行：", " ".join(cmd))
    if dry:
        return 0
    print("开始安装（首次会下载数百 MB~GB，请耐心）…")
    return subprocess.call(cmd)


def report(info: dict, names: list[str]) -> None:
    print("显卡        :", " / ".join(names) or "（读不到）")
    print("torch       :", info.get("torch", "?"), "| CUDA 构建版本:", info.get("cuda_build") or "无")
    print("CUDA 可用   :", info.get("cuda"), info.get("cuda_name", ""))
    print("XPU  可用   :", info.get("xpu"), info.get("xpu_name", ""),
          "| 支持架构:", info.get("xpu_arch", []))
    print("建议后端    :", guess_backend())


def write_marker(backend: str, info: dict) -> None:
    import datetime
    data = {"backend": backend, "torch": info.get("torch", ""),
            "cuda_build": info.get("cuda_build", ""), "xpu_available": bool(info.get("xpu")),
            "cuda_available": bool(info.get("cuda")),
            "xpu_name": info.get("xpu_name", ""), "cuda_name": info.get("cuda_name", ""),
            "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}
    with open(MARKER, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("已写", MARKER)


def main() -> int:
    ap = argparse.ArgumentParser(description="EAI 训练后端安装/校验")
    ap.add_argument("--backend", choices=["auto", "xpu", "cuda", "cpu"], default="check")
    ap.add_argument("--version", help="指定 torch 版本（默认沿用当前已装版本）")
    ap.add_argument("--dry-run", action="store_true", help="只解析并打印命令，不安装")
    ap.add_argument("--check", action="store_true", help="只报告当前环境")
    ap.add_argument("--force", action="store_true",
                    help="本机没 NVIDIA 卡也硬装 CUDA 构建（会把现有的 +xpu 换掉）")
    a = ap.parse_args()

    before = probe()
    names = gpu_names()
    if a.check or a.backend == "check":
        report(before, names)
        return 0

    backend = guess_backend() if a.backend == "auto" else a.backend
    # 显卡与构建是二选一（用户 2026-10-20）：CUDA 轮子不含 XPU，装上就把 Arc 训练废掉；
    # 而且没有 N 卡时 cuda.is_available() 仍然是 False，纯属白装。先拦一道。
    if backend == "cuda" and not a.force:
        joined = " ".join(names).lower()
        if not any(k in joined for k in ("nvidia", "geforce", "quadro", "tesla")):
            print("✗ 本机没检测到 NVIDIA 显卡：", " / ".join(names) or "（读不到）")
            print("  当前 torch =", before.get("torch", "?"),
                  "| XPU 可用 =", before.get("xpu"))
            print("  CUDA 构建（+cuXXX）不含 XPU，装上之后现在的 Arc 训练会失效，"
                  "而没有 N 卡 cuda.is_available() 依然是 False。")
            print("  确实要预装（换机器/做镜像）就加 --force 再跑一次。")
            return 2
    version = a.version or (before.get("torch", "") or "").split("+")[0] or None
    print(f"目标后端：{backend}（版本沿用 {version or '最新'}）")
    spec = resolve(backend, version)
    rc = install(spec, a.dry_run)
    if rc or a.dry_run:
        return rc

    after = probe()
    print("\n安装后复验（新进程）：")
    report(after, names)
    ok = bool(after.get("xpu")) if backend == "xpu" else \
         (bool(after.get("cuda")) if backend == "cuda" else True)
    write_marker(backend, after)
    print("结果：", "✓ 可用" if ok else "✗ 装了但探测不到设备（多为驱动/运行时缺失，见下）")
    if not ok and backend == "xpu":
        print("  · 需要 Intel 显卡驱动带 compute runtime（Level Zero）；建议升级到最新 Arc 驱动")
    if not ok and backend == "cuda":
        print("  · 需要 NVIDIA 驱动 + 对应 CUDA 运行时")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
