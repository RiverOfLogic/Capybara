"""SearchTextTool — 在工作区内正则搜索文本"""

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult
from ._safety import validate_path
from core.exceptions import ToolException

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".mypy_cache", ".ruff_cache"}


class SearchTextTool(Tool):
    """在工作区内搜索匹配正则的文本行，返回 文件:行号: 内容 格式"""

    name = "search_text"
    description = "在指定路径下搜索匹配正则表达式的文本行"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "正则表达式搜索模式",
            },
            "path": {
                "type": "string",
                "description": "搜索起点目录或文件（相对于工作区根目录），默认工作区根目录",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "是否大小写敏感，默认 false",
            },
            "file_pattern": {
                "type": "string",
                "description": "文件名 glob 过滤（如 '*.py'），默认 '*'",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace_root: str = ".", max_results: int = 200) -> None:
        self._workspace_root = Path(workspace_root)
        self._max_results = max_results

    def run(self, **kwargs: Any) -> ToolResult:
        pattern: str = kwargs.get("pattern", "")
        path: str = kwargs.get("path", ".")
        case_sensitive: bool = bool(kwargs.get("case_sensitive", False))
        file_pattern: str = kwargs.get("file_pattern", "*")

        if not pattern:
            return ToolResult.fail("参数 pattern 不能为空")

        # 编译正则
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult.fail(f"非法正则表达式: {exc}")

        try:
            target = validate_path(path, self._workspace_root)
        except ToolException as exc:
            return ToolResult.fail(str(exc))

        if not target.exists():
            return ToolResult.fail(f"路径不存在: {path}")

        results: list[str] = []
        truncated = False

        def _search_file(filepath: Path) -> None:
            nonlocal truncated
            if truncated:
                return
            try:
                text = filepath.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                return  # 跳过二进制或无权限文件

            # 计算相对于工作区根的显示路径
            try:
                rel = filepath.relative_to(self._workspace_root.resolve())
            except ValueError:
                rel = filepath

            for lineno, line in enumerate(text.splitlines(), start=1):
                if truncated:
                    break
                if regex.search(line):
                    results.append(f"{rel}:{lineno}: {line.rstrip()}")
                    if len(results) >= self._max_results:
                        truncated = True
                        break

        if target.is_file():
            _search_file(target)
        else:
            for dirpath, dirnames, filenames in os.walk(target):
                # 原地过滤，跳过不需要的目录
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for filename in sorted(filenames):
                    if not fnmatch.fnmatch(filename, file_pattern):
                        continue
                    _search_file(Path(dirpath) / filename)

        content = "\n".join(results)
        if truncated:
            content += f"\n[已截断，超出最大结果数 {self._max_results}]"
        if not results:
            content = f"未找到匹配: {pattern!r}"

        return ToolResult.succeed(content=content, match_count=len(results))
