"""ApplyPatchTool — search-replace 文件局部修改（原子写入）"""

from pathlib import Path
from typing import Any

from .base import Tool, ToolResult
from ._safety import validate_path
from ._edit_match import EditMatchError, atomic_write_text, find_and_replace
from core.exceptions import ToolException


class ApplyPatchTool(Tool):
    """用 search-replace 方式对文件做精确局部修改

    优先精确匹配（old_str 须恰好出现一次）；精确匹配失败时退而做「忽略行首尾
    空白」的按行模糊匹配，唯一命中则替换并在结果中标注「模糊匹配」。
    出现多次（歧义）或始终找不到时返回 fail。写入为临时文件 + os.replace 原子操作。
    """

    name = "apply_patch"
    description = (
        "将文件中匹配 old_str 的部分替换为 new_str。优先精确匹配（须唯一）；"
        "精确匹配失败时自动尝试忽略行首尾空白的模糊匹配。出现多次或找不到则报错。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于工作区根目录）",
            },
            "old_str": {
                "type": "string",
                "description": "要被替换的精确原文（含空格/换行，需与文件内容完全一致）",
            },
            "new_str": {
                "type": "string",
                "description": "替换后的新内容",
            },
        },
        "required": ["path", "old_str", "new_str"],
    }

    def __init__(self, workspace_root: str = ".") -> None:
        self._workspace_root = Path(workspace_root)

    def run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs.get("path", "")
        old_str: str = kwargs.get("old_str", "")
        new_str: str = kwargs.get("new_str", "")

        if not path:
            return ToolResult.fail("参数 path 不能为空")
        if not old_str:
            return ToolResult.fail("参数 old_str 不能为空")

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

        try:
            new_content, fuzzy = find_and_replace(original, old_str, new_str)
        except EditMatchError as exc:
            return ToolResult.fail(str(exc))

        try:
            atomic_write_text(target, new_content)
        except OSError as exc:
            return ToolResult.fail(f"写入失败: {exc}")

        old_lines = old_str.count("\n") + 1
        new_lines = new_str.count("\n") + 1
        note = "（模糊匹配）" if fuzzy else ""
        return ToolResult.succeed(
            content=f"已修改 {path}{note}（{old_lines} 行 → {new_lines} 行）",
            old_lines=old_lines,
            new_lines=new_lines,
            fuzzy=fuzzy,
        )
