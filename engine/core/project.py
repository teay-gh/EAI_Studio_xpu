"""工程文件系统：一个工程 = 一个文件夹 + 根目录「工程名.eproj」。

约定：
- 工程根目录下放「工程名.eproj」（JSON，文件名 = 文件夹名），其余产出按固定子目录存放。
  例：D:\EAI\EAI_GC\ 里就是 EAI_GC.eproj。
- 所有工作区路径都相对工程根，不再让用户手选数据集 / 输出目录。
- 该文件自动登记各子目录的文件清单（scan 结果写回 files 字段）。
- 旧名 project.eai_proj / project.eproj 打开时自动改名为「工程名.eproj」（用户 2026-10-18）。

目录布局：
    <工程根>/
      <工程名>.eproj
      data/article/     语料蒸馏 JSONL + txt
      data/response/    响应蒸馏 JSONL + txt
      data/corpora/     合并后的预训练 / 微调语料
      checkpoints/      模型权重
      logs/             训练日志
      exports/          导出副本
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

PROJECT_SUFFIX = ".eproj"          # 工程文件后缀：文件名 = 文件夹名 + .eproj
FORMAT = "eproj"
VERSION = 1
# 旧文件名 / 旧 format：打开老工程时自动迁移到「工程名.eproj」（用户 2026-10-18 定稿）
LEGACY_FILES = ("project.eai_proj", "project.eproj")
LEGACY_FORMATS = ("eai_proj",)


def project_filename(root: str) -> str:
    """工程文件名的标准写法：<文件夹名>.eproj（根目录无名字时用默认名）。"""
    base = os.path.basename(os.path.abspath(root).rstrip("\\/")) or DEFAULT_NAME
    return base + PROJECT_SUFFIX

# 固定子目录（相对工程根）
FOLDERS: dict[str, str] = {
    "data_article": "data/article",
    "data_response": "data/response",
    "corpora": "data/corpora",
    "checkpoints": "checkpoints",
    "logs": "logs",
    "exports": "exports",
    "chats": "chats",
}

# 训练语料默认文件名（落在 data/corpora/ 下）
PRETRAIN_CORPUS = "pretrain_corpus.txt"
SFT_CORPUS = "sft_corpus.txt"

DEFAULT_NAME = "未命名工程"


class Project:
    """一个打开中的工程。"""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.meta: dict[str, Any] = {
            "format": FORMAT,
            "version": VERSION,
            "name": os.path.basename(self.root) or DEFAULT_NAME,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "modified": "",
            "folders": dict(FOLDERS),
            "files": {},
        }
        self.project_file = os.path.join(self.root, project_filename(self.root))
        self.migrated_from = ""          # 旧名 project.eproj 迁移过来时记旧文件名（用户 2026-10-18）
        self.needs_rewrite = False       # 文件里还是旧 format 时，打开后重写一次

    # ---------- 路径 ----------
    def path(self, key: str) -> str:
        """返回子目录绝对路径（key 见 FOLDERS）。"""
        rel = FOLDERS.get(key) or key
        return os.path.join(self.root, rel)

    @property
    def article_dir(self) -> str:
        return self.path("data_article")

    @property
    def response_dir(self) -> str:
        return self.path("data_response")

    @property
    def corpora_dir(self) -> str:
        return self.path("corpora")

    @property
    def checkpoints_dir(self) -> str:
        return self.path("checkpoints")

    @property
    def logs_dir(self) -> str:
        return self.path("logs")

    @property
    def exports_dir(self) -> str:
        return self.path("exports")

    @property
    def pretrain_corpus(self) -> str:
        return os.path.join(self.corpora_dir, PRETRAIN_CORPUS)

    @property
    def sft_corpus(self) -> str:
        return os.path.join(self.corpora_dir, SFT_CORPUS)

    def ensure_tree(self) -> None:
        """创建全部子目录（幂等）。"""
        os.makedirs(self.root, exist_ok=True)
        for rel in FOLDERS.values():
            os.makedirs(os.path.join(self.root, rel), exist_ok=True)

    def rel(self, abspath: str) -> str:
        """绝对路径 → 相对工程根（失败则原样返回）。"""
        try:
            return os.path.relpath(abspath, self.root).replace("\\", "/")
        except ValueError:
            return abspath

    # ---------- 工程文件 ----------
    def save(self) -> None:
        self.ensure_tree()
        self.meta["modified"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.meta["name"] = self.meta.get("name") or os.path.basename(self.root)
        self.scan_into_meta()
        with open(self.project_file, "w", encoding="utf-8") as f:
            json.dump(self.meta, f, ensure_ascii=False, indent=2)

    def migrate_legacy(self) -> str:
        """旧工程文件 → 「工程名.eproj」（就地重命名，不动数据；用户 2026-10-18）。

        认三种情况：project.eai_proj / project.eproj 老文件名，以及工程文件夹改过名后
        留下的「旧文件夹名.eproj」。
        """
        if os.path.isfile(self.project_file):
            return ""
        cands = [os.path.join(self.root, old) for old in LEGACY_FILES]
        try:
            for name in sorted(os.listdir(self.root)):
                p = os.path.join(self.root, name)
                if (name.lower().endswith(PROJECT_SUFFIX) and os.path.isfile(p)
                        and name != os.path.basename(self.project_file)):
                    cands.append(p)
        except OSError:
            pass
        for old_path in cands:
            if not os.path.isfile(old_path):
                continue
            try:
                os.replace(old_path, self.project_file)
                self.migrated_from = os.path.basename(old_path)   # 记下来，open_project 会立刻重写一次
                return self.migrated_from
            except OSError:
                return ""
        return ""

    def load(self) -> bool:
        self.migrate_legacy()
        if not os.path.isfile(self.project_file):
            return False
        try:
            with open(self.project_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        raw_fmt = str(data.get("format") or "")
        self.meta.update(data)
        if raw_fmt != FORMAT:
            self.needs_rewrite = True             # format 还是 eproj（或缺失）：落下后按新格式重写
        self.meta["format"] = FORMAT
        self.meta["version"] = VERSION
        # 子目录映射保持受控，避免被旧文件带偏
        self.meta["folders"] = dict(FOLDERS)
        self.ensure_tree()
        return True

    def scan_into_meta(self) -> dict[str, list[str]]:
        """扫描各子目录，把文件清单写入工程文件（工程名.eproj）的 files 字段。"""
        inventory: dict[str, list[str]] = {}
        for key, rel in FOLDERS.items():
            folder = os.path.join(self.root, rel)
            entries: list[str] = []
            if os.path.isdir(folder):
                for dirpath, _dirnames, filenames in os.walk(folder):
                    for name in sorted(filenames):
                        if name.startswith("."):
                            continue
                        full = os.path.join(dirpath, name)
                        entries.append(self.rel(full))
            inventory[key] = entries
        self.meta["files"] = inventory
        return inventory

    def list_files(self, key: str) -> list[str]:
        return list(self.meta.get("files", {}).get(key) or [])

    # ---------- 便捷 ----------
    @property
    def name(self) -> str:
        return str(self.meta.get("name") or DEFAULT_NAME)

    @name.setter
    def name(self, value: str) -> None:
        self.meta["name"] = (value or "").strip() or DEFAULT_NAME


# ============================================================
# 工程管理（创建 / 打开 / 最近）
# ============================================================

RECENT_KEY = "recent_projects"


def create_project(root: str, name: str = "") -> Project:
    """在 root 下建立工程骨架并写出「工程名.eproj」。"""
    proj = Project(root)
    proj.ensure_tree()
    if name:
        proj.name = name
    proj.save()
    return proj


def open_project(root: str) -> Project | None:
    """打开已有工程；若无工程文件则自动补建（旧名会先迁移成「工程名.eproj」）。"""
    proj = Project(root)
    if not os.path.isdir(root):
        return None
    if not proj.load():
        proj.ensure_tree()
        proj.save()
    else:
        proj.ensure_tree()
        if proj.migrated_from or proj.needs_rewrite:   # 旧名迁移 / 旧 format：立刻按新格式落盘（用户 2026-10-18）
            proj.save()
    return proj


def find_project_file(root: str) -> str | None:
    """找工程文件：先认「工程名.eproj」，再认旧名，最后认目录里任意 *.eproj。"""
    path = os.path.join(root, project_filename(root))
    if os.path.isfile(path):
        return path
    for old in LEGACY_FILES:
        p = os.path.join(root, old)
        if os.path.isfile(p):
            return p
    try:
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if name.lower().endswith(PROJECT_SUFFIX) and os.path.isfile(p):
                return p
    except OSError:
        pass
    return None


def is_project_dir(root: str) -> bool:
    """是不是工程目录（旧名 / 文件夹改过名的 *.eproj 都算，迁移前的老工程仍能被识别）。"""
    return find_project_file(root) is not None


def scan_data_files(proj: Project, kind: str = "all") -> list[str]:
    """列出工程内数据相关文件（供数据集页扫描）。

    kind: article | response | corpora | all
    """
    keys = {
        "article": ["data_article"],
        "response": ["data_response"],
        "corpora": ["corpora"],
        "all": ["data_article", "data_response", "corpora"],
    }.get(kind, ["data_article", "data_response", "corpora"])
    out: list[str] = []
    for key in keys:
        for rel in proj.list_files(key):
            full = os.path.join(proj.root, rel.replace("/", os.sep))
            if os.path.isfile(full):
                out.append(full)
    return out


# ---------- 最近工程（存全局 config.json 的 app.recent_projects） ----------

def recent_list(cfg: dict[str, Any]) -> list[str]:
    items = (cfg.get("app") or {}).get(RECENT_KEY) or []
    return [p for p in items if isinstance(p, str) and p]


def push_recent(cfg: dict[str, Any], root: str) -> None:
    root = os.path.abspath(root)
    items = recent_list(cfg)
    if root in items:
        items.remove(root)
    items.insert(0, root)
    cfg.setdefault("app", {})[RECENT_KEY] = items[:12]


def pop_recent(cfg: dict[str, Any], root: str) -> None:
    root = os.path.abspath(root)
    items = recent_list(cfg)
    if root in items:
        items.remove(root)
    cfg.setdefault("app", {})[RECENT_KEY] = items
