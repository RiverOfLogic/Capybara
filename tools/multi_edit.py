"""MultiEditTool — 对单个文件一次提交多处 search-replace（原子，全成功才落盘）"""

from pathlib import Path
from typing import Any, List

from .base import Tool, ToolResult
from ._safety import validate_path
from ._edit_match import EditMatchError, atomic_write_text, find_and_replace
from core.exceptions import ToolException


class MultiEditTool(Tool):
    """对一个文件按顺序应用多处替换，任一处失败则全部不写（原子）。

    每处 edit 与 apply_patch 同款匹配（先精确、再忽略行首尾空白的模糊匹配）。
    edits 按给定顺序作用在「不断演进的内容」上——后面的 old_str 应针对前面
    替换后的状态书写。
    """

    name = "multi_edit"
    description = (
        "对同一个文件一次性应用多处 search-replace。edits 为数组，每项含 old_str/new_str，"
        "按顺序作用；任一处匹配失败则整体不写入（原子）。适合一次改动文件中的多个位置。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于工作区根目录）",
            },
            "edits": {
                "type": "array",
                "description": "多处替换，按顺序应用",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_str": {"type": "string", "description": "要被替换的原文"},
                        "new_str": {"type": "string", "description": "替换后的新内容"},
                    },
                    "required": ["old_str", "new_str"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    def __init__(self, workspace_root: str = ".") -> None:
        self._workspace_root = Path(workspace_root)

    def run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs.get("path", "")
        edits: List[dict] = kwargs.get("edits") or []

        if not path:
            return ToolResult.fail("参数 path 不能为空")
        if not isinstance(edits, list) or not edits:
            return ToolResult.fail("参数 edits 必须是非空数组")

        try:
            target = validate_path(path, self._workspace_root)
        except ToolException as exc:
            return ToolResult.fail(str(exc))

        if not target.exists():
            return ToolResult.fail(f"文件不存在: {path}")
        if target.is_dir():
            return ToolResult.fail(f"路径是目录，不是文件: {path}")

        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult.fail("非文本文件，无法修改")
        except OSError as exc:
            return ToolResult.fail(f"读取失败: {exc}")

        # 全部应用到内存副本，任一失败即整体放弃（原子）
        fuzzy_count = 0
        for idx, edit in enumerate(edits, 1):
            if not isinstance(edit, dict):
                return ToolResult.fail(f"第 {idx} 处 edit 格式错误（应为对象）")
            try:
                content, fuzzy = find_and_replace(
                    content, edit.get("old_str", ""), edit.get("new_str", ""),
                )
            except EditMatchError as exc:
                return ToolResult.fail(f"第 {idx} 处替换失败：{exc}（已放弃全部改动）")
            fuzzy_count += 1 if fuzzy else 0

        try:
            atomic_write_text(target, content)
        except OSError as exc:
            return ToolResult.fail(f"写入失败: {exc}")

        note = f"，其中 {fuzzy_count} 处为模糊匹配" if fuzzy_count else ""
        return ToolResult.succeed(
            content=f"已对 {path} 应用 {len(edits)} 处替换{note}",
            edits_applied=len(edits),
            fuzzy_count=fuzzy_count,
        )
