"""ReadFileTool — 读取文件内容（带行号）"""

from pathlib import Path
from typing import Any, Optional

from .base import Tool, ToolResult
from ._safety import validate_path
from core.exceptions import ToolException


class ReadFileTool(Tool):
    """读取工作区内文件内容，支持行范围切片"""

    name = "read_file"
    description = "读取指定文件内容，输出带行号，支持 start_line/end_line 指定范围"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于工作区根目录）",
            },
            "start_line": {
                "type": "integer",
                "description": "起始行号（1-based，含），默认从头开始",
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号（1-based，含），默认到文件末尾",
            },
        },
        "required": ["path"],
    }

    def __init__(self, workspace_root: str = ".", max_lines: int = 2000) -> None:
        self._workspace_root = Path(workspace_root)
        self._max_lines = max_lines

    def run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs.get("path", "")
        start_line: Optional[int] = kwargs.get("start_line")
        end_line: Optional[int] = kwargs.get("end_line")

        if not path:
            return ToolResult.fail("参数 path 不能为空")

        try:
            target = validate_path(path, self._workspace_root)
        except ToolException as exc:
            return ToolResult.fail(str(exc))

        if not target.exists():
            return ToolResult.fail(f"文件不存在: {path}")
        if target.is_dir():
            return ToolResult.fail(f"路径是目录，不是文件: {path}")

        try:
            raw = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult.fail("非文本文件，无法读取")
        except OSError as exc:
            return ToolResult.fail(f"读取失败: {exc}")

        all_lines = raw.splitlines(keepends=True)
        total_lines = len(all_lines)

        # 转换为 0-based 索引
        s = (start_line - 1) if start_line is not None else 0
        e = end_line if end_line is not None else total_lines
        s = max(0, s)
        e = min(total_lines, e)
        sliced = all_lines[s:e]

        truncated = False
        if len(sliced) > self._max_lines:
            sliced = sliced[: self._max_lines]
            truncated = True

        lines_out: list[str] = []
        for i, line in enumerate(sliced):
            lineno = s + i + 1
            lines_out.append(f"{lineno:4d}  {line.rstrip()}")

        content = "\n".join(lines_out)
        if truncated:
            content += f"\n[已截断，共 {total_lines} 行，仅显示前 {self._max_lines} 行]"

        return ToolResult.succeed(
            content=content,
            total_lines=total_lines,
            shown_lines=len(sliced),
        )
