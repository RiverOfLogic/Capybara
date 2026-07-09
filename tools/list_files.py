"""ListFilesTool — 列出目录树形结构"""

import os
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult
from ._safety import validate_path
from core.exceptions import ToolException

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".mypy_cache", ".ruff_cache"}
_SKIP_EXTS = {".pyc", ".pyo"}


class ListFilesTool(Tool):
    """列出工作区目录树形结构"""

    name = "list_files"
    description = "列出指定目录的树形结构（递归）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要列出的目录路径（相对于工作区根目录），默认为工作区根目录",
            },
            "max_depth": {
                "type": "integer",
                "description": "最大递归深度，默认 3",
            },
        },
    }

    def __init__(self, workspace_root: str = ".") -> None:
        self._workspace_root = Path(workspace_root)

    def run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs.get("path", ".")
        max_depth: int = int(kwargs.get("max_depth", 3))

        try:
            target = validate_path(path, self._workspace_root)
        except ToolException as exc:
            return ToolResult.fail(str(exc))

        if not target.exists():
            return ToolResult.fail(f"路径不存在: {path}")
        if not target.is_dir():
            return ToolResult.fail(f"不是目录: {path}")

        lines: list[str] = [str(target)]
        file_count = 0
        dir_count = 0

        def _walk(current: Path, prefix: str, depth: int) -> None:
            nonlocal file_count, dir_count
            if depth > max_depth:
                lines.append(f"{prefix}[已截断，超出最大深度 {max_depth}]")
                return

            try:
                entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name))
            except PermissionError:
                lines.append(f"{prefix}[无访问权限]")
                return

            for i, entry in enumerate(entries):
                if entry.name in _SKIP_DIRS:
                    continue
                if entry.suffix in _SKIP_EXTS:
                    continue

                connector = "└── " if i == len(entries) - 1 else "├── "
                extension = "    " if i == len(entries) - 1 else "│   "

                if entry.is_dir():
                    dir_count += 1
                    lines.append(f"{prefix}{connector}{entry.name}/")
                    _walk(entry, prefix + extension, depth + 1)
                else:
                    file_count += 1
                    lines.append(f"{prefix}{connector}{entry.name}")

        _walk(target, "", 1)

        return ToolResult.succeed(
            content="\n".join(lines),
            file_count=file_count,
            dir_count=dir_count,
        )
