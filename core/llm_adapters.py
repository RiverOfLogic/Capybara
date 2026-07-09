import time
import asyncio
import json
from abc import ABC, abstractmethod
from typing import Optional, Iterator, List, Dict, Any, Union, AsyncIterator
from .exceptions import LLMException

from .llm_response import LLMResponse, StreamStats, LLMToolResponse, ToolCall


class BaseLLMAdapter(ABC):
    def __init__(self, api_key: str, base_url: Optional[str], timeout: int, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.model = model
        self._client = None
        self._async_client = None

    @abstractmethod
    def create_client(self) -> Any:
        """创建客户端实例"""
        pass

    def create_async_client(self) -> Any:
        """创建异步客户端实例（子类可选实现）"""
        return None
    
    @abstractmethod
    def invoke(self, messages: List[Dict], **kwargs) -> LLMResponse:
        """非流式调用"""
        pass

    @abstractmethod
    def stream_invoke(self, messages: List[Dict], **kwargs) -> Iterator[str]:
        """流式调用，返回生成器"""
        pass

    async def astream_invoke(self, messages: List[Dict], **kwargs) -> AsyncIterator[str]:
        """异步流式调用（子类可选实现真正的异步）

        默认实现：使用队列 + 线程池包装同步流式方法
        """
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _stream_to_queue():
            try:
                for chunk in self.stream_invoke(messages, **kwargs):
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(queue.put(e), loop)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        # 在线程池中运行同步流式方法
        loop.run_in_executor(None, _stream_to_queue)

        # 从队列中逐个取出 chunk
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def ainvoke(self, messages: List[Dict], **kwargs) -> LLMResponse:
        """异步非流式调用（默认实现：线程池包装同步 invoke；子类可覆盖为原生异步）"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.invoke(messages, **kwargs))

    @abstractmethod
    def invoke_with_tools(self, messages: List[Dict], tools: List[Dict], **kwargs) -> LLMToolResponse:
        """工具调用（Function Calling）"""
        pass

    async def ainvoke_with_tools(
        self, messages: List[Dict], tools: List[Dict], **kwargs
    ) -> LLMToolResponse:
        """异步工具调用（默认实现：线程池包装同步；子类可覆盖为原生异步）"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.invoke_with_tools(messages, tools, **kwargs)
        )

    def _is_thinking_model(self, model_name: str) -> bool:
        """判断是否为thinking model"""
        thinking_keywords = ["reasoner", "o1", "o3", "thinking"]
        model_lower = model_name.lower()
        return any(keyword in model_lower for keyword in thinking_keywords)

class OpenAIAdapter(BaseLLMAdapter):
    """OpenAI兼容接口适配器（默认）

    支持：
    - OpenAI官方API
    - 所有OpenAI兼容接口（DeepSeek、Qwen、Kimi、智谱等）
    - Thinking Models（o1、deepseek-reasoner等）
    """

    def create_client(self) -> Any:
        """创建OpenAI客户端"""
        from openai import OpenAI

        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout
        )

    def create_async_client(self) -> Any:
        """创建OpenAI异步客户端"""
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout
        )
    @staticmethod
    def _extract_usage(response) -> Dict[str, int]:
        """从响应中提取 usage（同步/异步/流式共用）"""
        if hasattr(response, 'usage') and response.usage:
            return {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return {}

    def _build_response(self, response, latency_ms: int) -> LLMResponse:
        """把 chat.completions 响应解析为 LLMResponse（同步/异步共用）"""
        choice = response.choices[0]
        content = choice.message.content or ""
        reasoning_content = None

        # Thinking model特殊处理
        if self._is_thinking_model(self.model):
            # OpenAI o1系列：reasoning_content在message中
            if hasattr(choice.message, 'reasoning_content'):
                reasoning_content = choice.message.reasoning_content
            # DeepSeek reasoner：可能在其他字段
            elif hasattr(choice, 'reasoning_content'):
                reasoning_content = choice.reasoning_content

        return LLMResponse(
            content=content,
            model=self.model,
            usage=self._extract_usage(response),
            latency_ms=latency_ms,
            reasoning_content=reasoning_content,
        )

    def _build_tool_response(self, response, latency_ms: int) -> LLMToolResponse:
        """把 Function Calling 响应解析为 LLMToolResponse（同步/异步共用）"""
        message = response.choices[0].message

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments
                ))

        return LLMToolResponse(
            content=message.content,
            tool_calls=tool_calls,
            model=response.model,
            usage=self._extract_usage(response),
            latency_ms=latency_ms,
        )

    def invoke(self, messages: List[Dict], **kwargs) -> LLMResponse:
        """非流式调用"""
        if not self._client:
            self._client = self.create_client()

        start_time = time.time()

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                **kwargs
            )
            return self._build_response(response, int((time.time() - start_time) * 1000))

        except Exception as e:
            raise LLMException(f"OpenAI API调用失败: {str(e)}")

    async def ainvoke(self, messages: List[Dict], **kwargs) -> LLMResponse:
        """原生异步非流式调用（使用 OpenAI AsyncOpenAI 客户端，不占线程池）"""
        if not self._async_client:
            self._async_client = self.create_async_client()

        start_time = time.time()

        try:
            response = await self._async_client.chat.completions.create(
                model=self.model,
                messages=messages,
                **kwargs
            )
            return self._build_response(response, int((time.time() - start_time) * 1000))

        except Exception as e:
            raise LLMException(f"OpenAI API异步调用失败: {str(e)}")

    def stream_invoke(self, messages: List[Dict], **kwargs) -> Iterator[str]:
        """流式调用"""
        if not self._client:
            self._client = self.create_client()
        
        start_time = time.time()
        
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
                **kwargs
            )
            
            collected_content = []
            reasoning_content = None
            usage = {}
            
            for chunk in response:
                choices = getattr(chunk, "choices", None)
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    if delta is not None:
                        # 提取内容
                        content = getattr(delta, "content", None)
                        if content:
                            collected_content.append(content)
                            yield content

                        # Thinking model的推理过程
                        if self._is_thinking_model(self.model):
                            reasoning_delta = getattr(delta, "reasoning_content", None)
                            if reasoning_delta:
                                if reasoning_content is None:
                                    reasoning_content = ""
                                reasoning_content += reasoning_delta

                # 提取usage（流式最后一个chunk可能包含）
                if hasattr(chunk, 'usage') and chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }

            latency_ms = int((time.time() - start_time) * 1000)

            # 返回统计信息（存储到适配器，供外部获取）
            self.last_stats = StreamStats(
                model=self.model,
                usage=usage,
                latency_ms=latency_ms,
                reasoning_content=reasoning_content
            )

        except Exception as e:
            raise LLMException(f"OpenAI API流式调用失败: {str(e)}")

    async def astream_invoke(self, messages: List[Dict], **kwargs) -> AsyncIterator[str]:
        """真正的异步流式调用（使用 OpenAI 原生异步客户端）"""
        if not self._async_client:
            self._async_client = self.create_async_client()

        start_time = time.time()

        try:
            response = await self._async_client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
                **kwargs
            )

            collected_content = []
            reasoning_content = None
            usage = {}

            async for chunk in response:
                choices = getattr(chunk, "choices", None)
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    if delta is not None:
                        # 提取内容
                        content = getattr(delta, "content", None)
                        if content:
                            collected_content.append(content)
                            yield content

                        # Thinking model的推理过程
                        if self._is_thinking_model(self.model):
                            reasoning_delta = getattr(delta, "reasoning_content", None)
                            if reasoning_delta:
                                if reasoning_content is None:
                                    reasoning_content = ""
                                reasoning_content += reasoning_delta

                # 提取usage（流式最后一个chunk可能包含）
                if hasattr(chunk, 'usage') and chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }

            latency_ms = int((time.time() - start_time) * 1000)

            # 返回统计信息（存储到适配器，供外部获取）
            self.last_stats = StreamStats(
                model=self.model,
                usage=usage,
                latency_ms=latency_ms,
                reasoning_content=reasoning_content
            )

        except Exception as e:
            raise LLMException(f"OpenAI API异步流式调用失败: {str(e)}")

    def invoke_with_tools(self, messages: List[Dict], tools: List[Dict],
                         tool_choice: Union[str, Dict] = "auto", **kwargs) -> LLMToolResponse:
        """工具调用（Function Calling）"""
        if not self._client:
            self._client = self.create_client()

        start_time = time.time()
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                **kwargs
            )
            return self._build_tool_response(response, int((time.time() - start_time) * 1000))

        except Exception as e:
            raise LLMException(f"OpenAI Function Calling调用失败: {str(e)}")

    async def ainvoke_with_tools(self, messages: List[Dict], tools: List[Dict],
                                 tool_choice: Union[str, Dict] = "auto", **kwargs) -> LLMToolResponse:
        """原生异步工具调用（使用 OpenAI AsyncOpenAI 客户端，不占线程池）"""
        if not self._async_client:
            self._async_client = self.create_async_client()

        start_time = time.time()
        try:
            response = await self._async_client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                **kwargs
            )
            return self._build_tool_response(response, int((time.time() - start_time) * 1000))

        except Exception as e:
            raise LLMException(f"OpenAI Function Calling异步调用失败: {str(e)}")

def create_adapter(
    api_key: str,
    base_url: Optional[str],
    timeout: int,
    model: str
) -> BaseLLMAdapter:

    # 默认使用OpenAI适配器（兼容所有OpenAI格式接口）
    return OpenAIAdapter(api_key, base_url, timeout, model)
       
