"""ReplaceLinesTool — 按行号区间替换文件内容（原子写入）"""

from pathlib import Path
from typing import Any

from .base import Tool, ToolResult
from ._safety import validate_path
from ._edit_match import atomic_write_text
from core.exceptions import ToolException


class ReplaceLinesTool(Tool):
    """用 new_text 替换文件 [start_line, end_line]（1 基、闭区间）范围内的行。

    适合「按 read_file 给出的行号精确定位」的编辑，避免 old_str 完全匹配的负担。
    new_text 为空字符串表示删除该范围的行。写入为临时文件 + os.replace 原子操作。
    """

    name = "replace_lines"
    description = (
        "用 new_text 替换文件第 start_line 到 end_line 行（均为 1 基、闭区间）。"
        "适合按 read_file 显示的行号精确编辑；new_text 为空表示删除这些行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于工作区根目录）",
            },
            "start_line": {
                "type": "integer",
                "description": "起始行号（1 基，闭区间）",
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号（1 基，闭区间，须 ≥ start_line）",
            },
            "new_text": {
                "type": "string",
                "description": "替换后的新内容（不含行号；空字符串表示删除这些行）",
            },
        },
        "required": ["path", "start_line", "end_line", "new_text"],
    }

    def __init__(self, workspace_root: str = ".") -> None:
        self._workspace_root = Path(workspace_root)

    def run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs.get("path", "")
        new_text: str = kwargs.get("new_text", "")

        if not path:
            return ToolResult.fail("参数 path 不能为空")
        try:
            start_line = int(kwargs.get("start_line"))
            end_line = int(kwargs.get("end_line"))
        except (TypeError, ValueError):
            return ToolResult.fail("start_line / end_line 必须是整数")

        try:
            target = validate_path(path, self._workspace_root)
        except ToolException as exc:
            return ToolResult.fail(str(exc))

        if not target.exists():
            return ToolResult.fail(f"文件不存在: {path}")
        if target.is_dir():
            return ToolResult.fail(f"路径是目录，不是文件: {path}")

        try:
            original = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult.fail("非文本文件，无法修改")
        except OSError as exc:
            return ToolResult.fail(f"读取失败: {exc}")

        lines = original.split("\n")
        # 文件以换行结尾时 split 会多出一个尾部空串，编辑时先剥离、最后还原
        trailing_newline = original.endswith("\n")
        if trailing_newline and lines and lines[-1] == "":
            lines = lines[:-1]
        total = len(lines)

        if start_line < 1 or end_line < start_line:
            return ToolResult.fail(
                f"行号区间非法：start_line={start_line}, end_line={end_line}"
            )
        if start_line > total:
            return ToolResult.fail(
                f"start_line={start_line} 超出文件行数（共 {total} 行）"
            )
        end = min(end_line, total)

        replacement = new_text.split("\n") if new_text != "" else []
        new_lines = lines[:start_line - 1] + replacement + lines[end:]
        new_content = "\n".join(new_lines)
        if trailing_newline:
            new_content += "\n"

        try:
            atomic_write_text(target, new_content)
        except OSError as exc:
            return ToolResult.fail(f"写入失败: {exc}")

        removed = end - start_line + 1
        return ToolResult.succeed(
            content=f"已替换 {path} 第 {start_line}-{end} 行（{removed} 行 → {len(replacement)} 行）",
            lines_removed=removed,
            lines_added=len(replacement),
        )
