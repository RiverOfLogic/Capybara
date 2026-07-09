"""工具注册表

ToolRegistry 负责：
- 注册 / 注销工具
- 按名称获取与调用工具
- 列出工具、生成 LLM tool calling 所需的 schema
- 计算工具 schema 哈希（供会话一致性检查复用）
"""

import json
from hashlib import sha256
from typing import Any, Dict, List

from core.exceptions import ToolException
from .base import Tool, ToolResult


class ToolRegistry:
    """工具注册表

    用法示例：
    ```python
    registry = ToolRegistry()
    registry.register(EchoTool())
    result = registry.call("echo", text="hello")
    schemas = registry.get_schemas()  # 传给 invoke_with_tools(tools=schemas)
    ```
    """

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具，重名抛 ToolException"""
        if not getattr(tool, "name", ""):
            raise ToolException("工具必须提供非空的 name")
        if tool.name in self._tools:
            raise ToolException(f"工具名称已存在: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """注销工具，不存在抛 ToolException"""
        if name not in self._tools:
            raise ToolException(self._not_found_message(name))
        del self._tools[name]

    def get(self, name: str) -> Tool:
        """按名称获取工具，不存在抛 ToolException（信息含可用工具列表）"""
        if name not in self._tools:
            raise ToolException(self._not_found_message(name))
        return self._tools[name]

    def list_tools(self) -> List[Tool]:
        """列出所有已注册工具"""
        return list(self._tools.values())

    def get_schemas(self) -> List[Dict[str, Any]]:
        """生成所有工具的 OpenAI tool schema 列表"""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def call(self, name: str, /, **kwargs: Any) -> ToolResult:
        """按名称同步调用工具（name 为仅位置参数，避免与工具自身的 name 参数冲突）"""
        return self.get(name).run(**kwargs)

    async def acall(self, name: str, /, **kwargs: Any) -> ToolResult:
        """按名称异步调用工具（name 为仅位置参数，避免与工具自身的 name 参数冲突）"""
        return await self.get(name).arun(**kwargs)

    def schema_hash(self) -> str:
        """对排序后的 schemas 做 sha256，作为工具集合的一致性指纹"""
        payload = json.dumps(self.get_schemas(), sort_keys=True, ensure_ascii=False)
        return sha256(payload.encode("utf-8")).hexdigest()

    def _not_found_message(self, name: str) -> str:
        available = ", ".join(sorted(self._tools)) or "(无)"
        return f"未找到工具: {name}。可用工具: {available}"

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry(tools={sorted(self._tools)})"
