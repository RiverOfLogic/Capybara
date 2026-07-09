"""WriteFileTool — 写入文件（自动创建父目录）"""

from pathlib import Path
from typing import Any

from .base import Tool, ToolResult
from ._safety import validate_path
from core.exceptions import ToolException


class WriteFileTool(Tool):
    """将内容写入工作区内的文件，若父目录不存在则自动创建"""

    name = "write_file"
    description = "将指定内容写入文件（UTF-8），文件不存在则创建，存在则覆盖"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于工作区根目录）",
            },
            "content": {
                "type": "string",
                "description": "要写入的文本内容",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace_root: str = ".") -> None:
        self._workspace_root = Path(workspace_root)

    def run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs.get("path", "")
        content: str = kwargs.get("content", "")

        if not path:
            return ToolResult.fail("参数 path 不能为空")

        try:
            target = validate_path(path, self._workspace_root)
        except ToolException as exc:
            return ToolResult.fail(str(exc))

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.fail(f"写入失败: {exc}")

        byte_count = len(content.encode("utf-8"))
        return ToolResult.succeed(
            content=f"已写入 {byte_count} 字节 → {path}",
            bytes_written=byte_count,
        )
