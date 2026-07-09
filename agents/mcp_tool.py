"""MCPTool — 把 MCP server 暴露的工具包装成本项目的 Tool

`MCPTool` 把单个 MCP 工具定义（name / description / inputSchema）适配成
`tools.base.Tool`：`parameters` 直接复用 MCP 的 `inputSchema`，`run()` 转调
`MCPClient.call_tool`。这样 MCP 工具就能和内置 7 个工具一样进 `ToolRegistry`、
被 ReAct 循环里的 LLM 调用。

`register_mcp_tools(registry, client, prefix=...)` 批量发现并注册某个 server 的
全部工具，返回注册后的工具名列表。
"""

from typing import Any, Dict, List

from tools.base import Tool, ToolResult


class MCPTool(Tool):
    """把一个 MCP 工具定义包装为可注册的 Tool。"""

    def __init__(
        self,
        client: Any,
        tool_def: Dict[str, Any],
        exposed_name: str = "",
    ) -> None:
        self._client = client
        self._raw_name = tool_def.get("name", "")
        # 注册名可加前缀（避免与内置工具或多 server 间重名）
        self.name = exposed_name or self._raw_name
        self.description = (
            tool_def.get("description") or f"MCP 工具 {self._raw_name}"
        )
        schema = tool_def.get("inputSchema") or tool_def.get("input_schema")
        self.parameters = (
            schema if isinstance(schema, dict) and schema
            else {"type": "object", "properties": {}}
        )

    def run(self, **kwargs: Any) -> ToolResult:
        try:
            text = self._client.call_tool(self._raw_name, kwargs)
        except Exception as exc:
            return ToolResult.fail(
                f"MCP 工具 {self._raw_name} 调用失败：{type(exc).__name__}: {exc}"
            )
        return ToolResult.succeed(text, mcp_tool=self._raw_name)


def register_mcp_tools(registry: Any, client: Any, prefix: str = "") -> List[str]:
    """发现 client 的全部工具并以 `prefix+name` 注册进 registry，返回注册名列表。"""
    names: List[str] = []
    for tool_def in client.list_tools():
        raw = tool_def.get("name")
        if not raw:
            continue
        exposed = f"{prefix}{raw}"
        registry.register(MCPTool(client, tool_def, exposed_name=exposed))
        names.append(exposed)
    return names
