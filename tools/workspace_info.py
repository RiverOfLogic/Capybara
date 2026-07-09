"""WorkspaceInfoTool — 返回工作区概要信息"""

import os
from collections import Counter
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult

_KEY_FILES = [
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Makefile",
    ".env",
    "README.md",
    "README.rst",
    "Dockerfile",
    ".gitignore",
]

_EXT_TO_LANG = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".cpp": "C++",
    ".c": "C",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".sh": "Shell",
}

_SCAN_DIRS = [".", "core", "src", "lib", "app"]


class WorkspaceInfoTool(Tool):
    """返回工作区根目录、顶层结构、语言线索和关键配置文件列表"""

    name = "workspace_info"
    description = "返回工作区根目录路径、顶层文件结构、主要编程语言和关键配置文件"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, workspace_root: str = ".") -> None:
        self._workspace_root = Path(workspace_root)

    def run(self, **kwargs: Any) -> ToolResult:
        root = self._workspace_root.resolve()

        # 顶层文件/目录列表（一层）
        top_entries: list[str] = []
        try:
            for entry in sorted(root.iterdir()):
                if entry.name.startswith(".") and entry.name not in {".env", ".gitignore"}:
                    continue
                suffix = "/" if entry.is_dir() else ""
                top_entries.append(f"  {entry.name}{suffix}")
        except PermissionError:
            top_entries.append("  [无访问权限]")

        # 语言线索：扫描特定目录的文件扩展名
        ext_counter: Counter = Counter()
        for scan_dir in _SCAN_DIRS:
            scan_path = root / scan_dir
            if not scan_path.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(scan_path):
                dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".git", "node_modules"}]
                for fname in filenames:
                    ext = Path(fname).suffix.lower()
                    if ext in _EXT_TO_LANG:
                        ext_counter[ext] += 1

        lang_lines: list[str] = []
        if ext_counter:
            for ext, count in ext_counter.most_common(3):
                lang_lines.append(f"  {_EXT_TO_LANG[ext]}（{ext}，{count} 个文件）")
        else:
            lang_lines.append("  未检测到已知语言文件")

        # 关键配置文件
        key_file_lines: list[str] = []
        for fname in _KEY_FILES:
            exists = "✓" if (root / fname).exists() else "✗"
            key_file_lines.append(f"  {exists} {fname}")

        lines = [
            f"工作区根目录: {root}",
            "",
            "顶层结构:",
            *top_entries,
            "",
            "主要语言:",
            *lang_lines,
            "",
            "关键配置文件:",
            *key_file_lines,
        ]

        return ToolResult.succeed(content="\n".join(lines))
