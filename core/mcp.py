"""MCPClient — 零依赖的 MCP（Model Context Protocol）stdio 客户端

让本项目作为 **客户端** 消费外部 MCP server 暴露的工具：启动 server 子进程，
通过 stdin/stdout 上的 **行分隔 JSON-RPC 2.0** 通信，完成 initialize 握手后即可
`list_tools()` / `call_tool()`。配合 `agents.mcp_tool.register_mcp_tools` 可把这些
工具包装成本项目的 `Tool` 注册进 `ToolRegistry`，与内置工具无缝共存。

仅用标准库（subprocess + json + threading + queue），不引入官方 mcp SDK，
契合本项目「无额外依赖、stdlib 测试、自包含」的风格。当前仅支持 stdio 传输。

用法：
```python
from core.mcp import MCPClient

with MCPClient(command="python", args=["my_server.py"], name="demo") as client:
    print(client.list_tools())
    print(client.call_tool("add", {"a": 2, "b": 3}))
```
"""

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from .exceptions import AgentsException


class MCPError(AgentsException):
    """MCP 通信 / 协议错误。"""


def _extract_text(content: Any) -> str:
    """把 MCP 工具返回的 content 数组拍平成文本（供 LLM 阅读）。"""
    if isinstance(content, str):
        return content
    parts: List[str] = []
    for item in content or []:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        ctype = item.get("type")
        if ctype == "text":
            parts.append(item.get("text", ""))
        elif ctype == "resource":
            res = item.get("resource", {}) or {}
            parts.append(res.get("text") or f"[resource {res.get('uri', '')}]")
        else:
            parts.append(f"[{ctype or 'unknown'} content]")
    return "\n".join(parts)


class MCPClient:
    """通过 stdio 与单个 MCP server 子进程通信的同步客户端。

    线程模型：一个后台读线程把 server 的每行 JSON 放入队列；主线程发请求后
    在队列上等待匹配 id 的响应（忽略通知与其它 id）。请求串行发起，足够健壮。
    """

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
        name: Optional[str] = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.name = name or command

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._queue: "queue.Queue" = queue.Queue()
        self._next_id = 0
        self._started = False
        self._tools_cache: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> "MCPClient":
        """启动 server 子进程并完成 initialize 握手。"""
        if self._started:
            return self

        env = os.environ.copy()
        if self.env:
            env.update(self.env)

        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=self.cwd,
                text=True,
                encoding="utf-8",
                bufsize=1,  # 行缓冲
            )
        except (OSError, ValueError) as exc:
            raise MCPError(f"启动 MCP server 失败（{self.command}）：{exc}")

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        # initialize 握手：请求 + initialized 通知
        self._request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "Capybara", "version": "0.1"},
        })
        self._notify("notifications/initialized", {})
        self._started = True
        return self

    def close(self) -> None:
        """关闭 stdin 并终止 server 子进程（幂等）。"""
        proc, self._proc = self._proc, None
        self._started = False
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        # 关闭管道句柄，避免 ResourceWarning（reader 线程已随 stdout EOF 退出）
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.join(timeout=2)
            self._reader = None

    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # MCP 能力
    # ------------------------------------------------------------------

    def list_tools(self, *, refresh: bool = False) -> List[Dict[str, Any]]:
        """返回 server 的工具定义列表（[{name, description, inputSchema}, ...]）。"""
        if self._tools_cache is None or refresh:
            result = self._request("tools/list", {})
            self._tools_cache = result.get("tools", []) or []
        return self._tools_cache

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """调用 server 工具，返回拍平后的文本内容；server 报错则抛 MCPError。"""
        result = self._request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })
        text = _extract_text(result.get("content", []))
        if result.get("isError"):
            raise MCPError(text or f"MCP 工具 {name} 返回错误")
        return text

    # ------------------------------------------------------------------
    # 传输内部实现
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        """后台线程：逐行读取 server stdout，解析 JSON 入队；EOF 时放哨兵 None。"""
        try:
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._queue.put(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 忽略非 JSON 行（部分 server 会打印日志到 stdout）
        except Exception:
            pass
        finally:
            self._queue.put(None)

    def _send(self, obj: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("MCP 进程未启动或已关闭")
        try:
            self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            raise MCPError(f"写入 MCP 进程失败：{exc}")

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(f"MCP 请求超时：{method}")
            try:
                msg = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise MCPError(f"MCP 请求超时：{method}")
            if msg is None:
                raise MCPError("MCP 进程已退出（stdout 关闭）")
            if msg.get("id") != rid:
                continue  # 通知或其它 id：忽略（请求串行发起）
            if "error" in msg and msg["error"]:
                err = msg["error"]
                raise MCPError(
                    f"MCP 错误 {err.get('code')}: {err.get('message')}"
                )
            return msg.get("result", {}) or {}
