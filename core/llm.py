import os 
from dotenv import load_dotenv
from typing import Optional, Iterator, List, Dict, Union, AsyncIterator

from .exceptions import AgentsException
from .llm_adapters import create_adapter, BaseLLMAdapter
from .llm_response import LLMResponse, StreamStats, LLMToolResponse

# 加载 .env 中的环境变量
load_dotenv()

class AgentsLLM:
    def __init__(self,
                 model:Optional[str]=None, 
                 api_key:Optional[str]=None,
                 base_url:Optional[str]=None,
                 temperature:float = 0.7,
                 max_tokens:Optional[int]=None,
                 timeout:Optional[int]=None,
                 **kwargs):
        self.model = model or os.getenv("LLM_MODEL_ID")
        self.api_key = api_key or os.getenv("LLM_API_KEY")
        self.base_url = base_url or os.getenv("LLM_BASE_URL")
        self.timeout = timeout or int(os.getenv("LLM_TIMEOUT","60"))

        self.temperature = temperature
        self.max_tokens = max_tokens
        self.kwargs = kwargs

        if not self.model:
            raise AgentsException("必须提供模型名称（model参数或LLM_MODEL_ID环境变量）")
        if not self.api_key:
            raise AgentsException("必须提供API密钥（api_key参数或LLM_API_KEY环境变量）")
        if not self.base_url:
            raise AgentsException("必须提供服务地址（base_url参数或LLM_BASE_URL环境变量）")
        
        #创建适配器
        self._adapter:BaseLLMAdapter = create_adapter(self.api_key,
                                                      self.base_url,
                                                      self.timeout,
                                                      self.model)
        self.last_call_stats:Optional[StreamStats] = None

    def think(self, messages:List[Dict[str,str]], temperature:Optional[float]=None) -> Iterator[str]:
        print(f"正在调用{self.model}模型...")

        kwargs = {
            "temperature": temperature if temperature is not None else self.temperature,
        }
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
 
        try:
            print("√大模型响应成功：")
            for chunk in self._adapter.stream_invoke(messages,**kwargs):
                print(chunk, end="",flush=True)
                yield chunk
            print()

            if hasattr(self._adapter, 'last_stats'):
                self.last_call_stats = self._adapter.last_stats
        
        except Exception as e:
            print(f"x调用LLM api时发生错误： {e}")
            raise

    def invoke(self,messages:List[Dict[str,str]],**kwargs) -> LLMResponse:
        call_kwargs = {
            "temperature": kwargs.pop("temperature", self.temperature)
        }
        if self.max_tokens:
            call_kwargs["max_tokens"] = kwargs.pop("max_tokens", self.max_tokens)
        call_kwargs.update(kwargs)
        return self._adapter.invoke(messages, **call_kwargs)
    
    def stream_invoke(self,messages:List[Dict[str,str]],**kwargs) -> Iterator:
        call_kwargs = {
            "temperature": kwargs.pop("temperature", self.temperature)
        }
        if self.max_tokens:
            call_kwargs["max_tokens"] = kwargs.pop("max_tokens",self.max_tokens)
        call_kwargs.update(kwargs)

        for chunk in self._adapter.stream_invoke(messages,**call_kwargs):
            yield chunk

        if hasattr(self._adapter,'last_stats'):
            self.last_call_stats = self._adapter.last_stats
    
    def invoke_with_tools(
            self,
            messages:List[Dict[str,str]],
            tools: List[Dict],
            tool_choice: Union[str,Dict]="auto",
            **kwargs
    ) -> LLMToolResponse:
        
        call_kwargs = {
            "temperature":kwargs.pop("temperature",self.temperature),
            "tool_choice":tool_choice
        }
        if self.max_tokens:
            call_kwargs["max_tokens"]=kwargs.pop("max_tokens",self.max_tokens)
        call_kwargs.update(kwargs)

        return self._adapter.invoke_with_tools(messages,tools,**call_kwargs)
    
    async def ainvoke(self,messages:List[Dict[str,str]],**kwargs) -> LLMResponse:
        """原生异步非流式调用（委派 adapter 的 AsyncOpenAI 客户端）"""
        call_kwargs = {
            "temperature": kwargs.pop("temperature", self.temperature)
        }
        if self.max_tokens:
            call_kwargs["max_tokens"] = kwargs.pop("max_tokens", self.max_tokens)
        call_kwargs.update(kwargs)
        return await self._adapter.ainvoke(messages, **call_kwargs)

    async def astream_invoke(
            self,
            messages:List[Dict[str,str]],
            **kwargs
    ) -> AsyncIterator[str]:
        async for chunk in self._adapter.astream_invoke(messages,**kwargs):
            yield chunk

        if hasattr(self._adapter,"last_stats"):
            self.last_call_stats = self._adapter.last_stats

    async def satream_invoke(
            self,
            messages:List[Dict[str,str]],
            **kwargs
    ) -> AsyncIterator[str]:
        async for chunk in self.astream_invoke(messages,**kwargs):
            yield chunk
    
    async def ainvoke_with_tools(
        self,
        messages: List[Dict],
        tools: List[Dict],
        tool_choice: Union[str, Dict] = "auto",
        **kwargs
    ) -> LLMToolResponse:
        """原生异步工具调用（委派 adapter 的 AsyncOpenAI 客户端）"""
        call_kwargs = {
            "temperature": kwargs.pop("temperature", self.temperature),
            "tool_choice": tool_choice,
        }
        if self.max_tokens:
            call_kwargs["max_tokens"] = kwargs.pop("max_tokens", self.max_tokens)
        call_kwargs.update(kwargs)
        return await self._adapter.ainvoke_with_tools(messages, tools, **call_kwargs)
        







if __name__ == '__main__':
    try:
        llmClient = AgentsLLM()
        
        exampleMessages = [
            {"role": "system", "content": "You are a helpful assistant that writes Python code."},
            {"role": "user", "content": "写一个快速排序算法"}
        ]
        
        print("--- 调用LLM ---")
        responseText = llmClient.think(exampleMessages)
        if responseText:
            print("\n\n--- 完整模型响应 ---")
            print(responseText)

    except ValueError as e:
        print(e)
