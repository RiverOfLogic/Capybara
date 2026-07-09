"""代码智能子系统共享工具：语言路由、代码文件遍历、索引目录解析。

被 ctags / fts / treesitter 三个后端复用，统一「跳过哪些目录、认哪些扩展名、
索引产物放哪」的约定，避免各后端各写一套。
"""

import os
from pathlib import Path
from typing import Iterator, Optional, Tuple

# 与 tools/search_text.py 一致的跳过目录，外加索引目录本身
SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".mypy_cache", ".ruff_cache",
    ".agent", ".venv", "venv", ".idea", ".vs", "dist", "build",
}

# 文件扩展名 → 语言标识（贯穿三后端）
LANGUAGE_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cxx": "cpp", ".cc": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".hh": "cpp",
    ".java": "java",
    ".rs": "rust",
    ".go": "go",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
}

CODE_EXTS = frozenset(LANGUAGE_BY_EXT)

# 默认索引目录（相对工作区根），存放 ctags 的 tags 与 FTS 的 index.db
DEFAULT_INDEX_DIR = ".agent"


def language_of(path) -> Optional[str]:
    """按扩展名判定语言，未知返回 None。"""
    return LANGUAGE_BY_EXT.get(Path(path).suffix.lower())


def index_dir_path(workspace_root, index_dir: str = DEFAULT_INDEX_DIR) -> Path:
    """解析并确保索引目录存在，返回绝对路径。"""
    d = (Path(workspace_root) / index_dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def iter_code_files(
    root, lang_filter: Optional[str] = None
) -> Iterator[Tuple[Path, str]]:
    """遍历 root 下的代码文件，产出 (绝对路径, 语言)。

    跳过 SKIP_DIRS；只认 CODE_EXTS；可按语言过滤。
    """
    root = Path(root)
    if root.is_file():
        lang = language_of(root)
        if lang and (not lang_filter or lang == lang_filter):
            yield root, lang
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in sorted(filenames):
            lang = LANGUAGE_BY_EXT.get(Path(filename).suffix.lower())
            if lang is None:
                continue
            if lang_filter and lang != lang_filter:
                continue
            yield Path(dirpath) / filename, lang


def read_text(fp: Path) -> Optional[str]:
    """读 utf-8 文本，二进制/无权限返回 None。"""
    try:
        return fp.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def rel_to(fp: Path, root) -> str:
    """相对工作区根的展示路径（失败则原样）。"""
    try:
        return str(fp.resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(fp)
