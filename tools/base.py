"""工具抽象基类与统一执行结果对象

提供：
- ToolResult：统一的工具执行结果（ok / content / error / metadata）。
- Tool：工具抽象基类，约定 name / description / parameters / run()，
  默认提供 arun() 与 to_openai_schema()。
"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ToolResult:
    """统一的工具执行结果对象"""

    ok: bool
    content: str = ""
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def succeed(cls, content: str = "", **metadata: Any) -> "ToolResult":
        """构造成功结果"""
        return cls(ok=True, content=content, error=None, metadata=dict(metadata))

    @classmethod
    def fail(cls, error: str, **metadata: Any) -> "ToolResult":
        """构造失败结果"""
        return cls(ok=False, content="", error=error, metadata=dict(metadata))

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（方便日志记录与序列化）"""
        return {
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "metadata": self.metadata,
        }


class Tool(ABC):
    """工具抽象基类

    子类需要提供：
    - name: 工具名称（在注册表中唯一）
    - description: 工具说明（给 LLM 看的自然语言描述）
    - parameters: JSON Schema 的 parameters 对象，形如
        {"type": "object", "properties": {...}, "required": [...]}
      无参工具使用 {"type": "object", "properties": {}}。
    - run(**kwargs) -> ToolResult: 同步执行逻辑。

    约定：run() 应捕获自身预期内的异常并返回 ToolResult.fail(...)，
    而不是向上抛出，以便 Agent 循环能稳定处理工具失败。
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """同步执行工具"""
        raise NotImplementedError

    async def arun(self, **kwargs: Any) -> ToolResult:
        """异步执行工具（默认用线程池包装同步 run）"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.run(**kwargs))

    def to_openai_schema(self) -> Dict[str, Any]:
        """生成 OpenAI function calling 的 tool schema

        返回结构与 OpenAIAdapter.invoke_with_tools 期望的格式一致。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def __str__(self) -> str:
        return f"Tool(name={self.name})"

    def __repr__(self) -> str:
        return self.__str__()
