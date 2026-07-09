"""Minimal Agent base class for the current core package."""

from abc import ABC, abstractmethod
import asyncio
from datetime import datetime
from typing import Any, AsyncGenerator, Optional, TYPE_CHECKING

from .config import Config
from .exceptions import ConfigException
from .lifecycle import AgentEvent, EventType, LifecycleHook
from .llm import AgentsLLM
from .message import Message
from .session_store import SessionStore

if TYPE_CHECKING:  # 仅类型注解需要；运行时导入会触发 tools↔core 循环依赖
    from tools import ToolRegistry


class Agent(ABC):
    """Base Agent with minimal runnable infrastructure."""

    def __init__(
        self,
        name: str,
        llm: AgentsLLM,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        tool_registry: Optional["ToolRegistry"] = None,
    ):
        self.name = name
        self.llm = llm
        self.system_prompt = system_prompt
        self.config = config or Config()
        self.tool_registry = tool_registry

        self._messages: list[Message] = []
        self._start_time = datetime.now()
        self._session_metadata = {
            "created_at": self._start_time.isoformat(),
            "total_tokens": 0,
            "total_steps": 0,
            "duration_seconds": 0,
        }

        self.session_store: Optional[SessionStore] = None
        if self.config.session_enabled:
            self.session_store = SessionStore(session_dir=self.config.session_dir)

    @property
    def _history(self) -> list[Message]:
        """Backward-compatible history attribute."""
        return self._messages

    @_history.setter
    def _history(self, value: list[Message]) -> None:
        self._messages = list(value)

    @abstractmethod
    def run(self, input_text: str, **kwargs) -> str:
        """Run the Agent synchronously."""
        pass

    async def arun(
        self,
        input_text: str,
        on_start: LifecycleHook = None,
        on_step: LifecycleHook = None,
        on_finish: LifecycleHook = None,
        on_error: LifecycleHook = None,
        **kwargs,
    ) -> str:
        """Run the synchronous Agent in an executor and emit lifecycle hooks."""
        await self._emit_event(
            EventType.AGENT_START,
            on_start,
            input_text=input_text,
        )

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self.run(input_text, **kwargs),
            )

            await self._emit_event(
                EventType.AGENT_FINISH,
                on_finish,
                result=result,
            )
            return result

        except Exception as exc:
            await self._emit_event(
                EventType.AGENT_ERROR,
                on_error,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def arun_stream(
        self,
        input_text: str,
        **kwargs,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Run the Agent and yield coarse lifecycle events."""
        yield AgentEvent.create(
            EventType.AGENT_START,
            self.name,
            input_text=input_text,
        )

        try:
            result = await self.arun(input_text, **kwargs)
            yield AgentEvent.create(
                EventType.AGENT_FINISH,
                self.name,
                result=result,
            )
        except Exception as exc:
            yield AgentEvent.create(
                EventType.AGENT_ERROR,
                self.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def _emit_event(
        self,
        event_type: EventType,
        hook: LifecycleHook,
        **data,
    ) -> AgentEvent:
        """Create an event and call an optional lifecycle hook."""
        event = AgentEvent.create(event_type, self.name, **data)

        if hook:
            try:
                await asyncio.wait_for(
                    hook(event),
                    timeout=self.config.hook_timeout_seconds,
                )
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

        return event

    def add_message(self, message: Message) -> None:
        """Append a message to the conversation history."""
        self._messages.append(message)

    def clear_history(self) -> None:
        """Clear conversation history."""
        self._messages.clear()

    def get_history(self) -> list[Message]:
        """Return a shallow copy of the conversation history."""
        return list(self._messages)

    def save_session(self, session_name: Optional[str] = None) -> str:
        """Persist current Agent state using SessionStore."""
        if not self.session_store:
            raise ConfigException(
                "Session persistence is disabled. Set Config.session_enabled=True."
            )

        self._refresh_session_metadata()
        return self.session_store.save(
            agent_config=self._get_agent_config(),
            history=self._messages,
            tool_schema_hash=self._compute_tool_schema_hash(),
            read_cache=self._get_read_cache(),
            metadata=self._session_metadata,
            session_name=session_name,
        )

    def load_session(self, filepath: str) -> None:
        """Load message history and metadata from a saved session file."""
        if not self.session_store:
            raise ConfigException(
                "Session persistence is disabled. Set Config.session_enabled=True."
            )

        session_data = self.session_store.load(filepath)
        self._messages = [
            Message.from_dict(message)
            for message in session_data.get("history", [])
        ]
        self._session_metadata = session_data.get("metadata", {})

    def list_sessions(self) -> list[dict]:
        """List saved sessions for this Agent's session store."""
        if not self.session_store:
            return []
        return self.session_store.list_sessions()

    def _refresh_session_metadata(self) -> None:
        elapsed = datetime.now() - self._start_time
        self._session_metadata.update(
            {
                "total_tokens": self._estimate_history_tokens(),
                "total_steps": sum(
                    1 for message in self._messages if message.role == "assistant"
                ),
                "duration_seconds": round(elapsed.total_seconds(), 2),
                "saved_at": datetime.now().isoformat(),
            }
        )

    def _estimate_history_tokens(self) -> int:
        from .context_manager import estimate_tokens
        return estimate_tokens(self._messages)

    def _get_agent_config(self) -> dict[str, Any]:
        config = {
            "name": self.name,
            "agent_type": self.__class__.__name__,
            "llm_model": getattr(self.llm, "model", "unknown"),
        }
        if hasattr(self.llm, "base_url"):
            config["llm_base_url"] = self.llm.base_url
        return config

    def _compute_tool_schema_hash(self) -> str:
        if self.tool_registry is not None:
            return self.tool_registry.schema_hash()
        return "no-tools"

    def _get_read_cache(self) -> dict[str, dict]:
        return {}

    def __str__(self) -> str:
        return f"Agent(name={self.name}, model={getattr(self.llm, 'model', 'unknown')})"

    def __repr__(self) -> str:
        return self.__str__()
