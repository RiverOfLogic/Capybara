"""CodingAgent — 能读代码、搜索、修改、运行命令的编程 Agent

实现 ReAct 循环：
  LLM 判断 → 若有 tool_calls 则执行工具并把结果加回 messages → 循环
  → 直到 LLM 不再调用工具（给出最终答案）或达到 max_steps 上限

同步入口 run()，异步流式入口 arun_stream()（逐事件输出），异步入口 arun()。
"""

import asyncio
import difflib
import json
import os
import sys
from typing import AsyncGenerator, Awaitable, Callable, Optional

from core.agent import Agent
from core.config import Config
from core.exceptions import ConfigException
from core.lifecycle import AgentEvent, EventType
from core.llm import AgentsLLM
from core.message import Message
from core.trace import TraceLogger
from core.context_manager import (
    estimate_tokens,
    truncate_output,
    compact_history,
    compact_run_messages,
)
from core.resilience import (
    CircuitBreaker,
    CircuitBreakerOpen,
    aretry_call,
    retry_call,
)
from .edit_tracker import EditTracker
from .project_context import detect_project_context, format_project_context
from .verify import parse_test_output
from .productivity import DevLogTool, TodoWriteTool
from .skills import SkillLoader, SkillTool
from tools import (
    ApplyPatchTool,
    FindDefinitionTool,
    FindReferencesTool,
    GetFileRelationsTool,
    ListFilesTool,
    MultiEditTool,
    ReadFileTool,
    ReplaceLinesTool,
    RepoMapTool,
    RunCommandTool,
    SearchCodebaseTool,
    SearchTextTool,
    ToolRegistry,
    WorkspaceInfoTool,
    WriteFileTool,
)


def _chunk_text(text: str, size: int = 24) -> list[str]:
    """把文本按固定长度切片，供 LLM_CHUNK 逐段输出。空串返回空列表。"""
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


def _build_default_registry(
    workspace_root: str, cfg: Optional[Config] = None
) -> ToolRegistry:
    """创建并注册全部 14 个内置编程工具。

    代码智能五件套（find_definition / search_codebase / find_references /
    repo_map / get_file_relations）从 cfg 读取 ctags 路径与索引目录。
    """
    ctags_path = getattr(cfg, "ctags_path", "") if cfg else ""
    index_dir = getattr(cfg, "code_index_dir", ".agent") if cfg else ".agent"
    registry = ToolRegistry()
    registry.register(WorkspaceInfoTool(workspace_root))
    registry.register(ListFilesTool(workspace_root))
    registry.register(ReadFileTool(workspace_root))
    registry.register(SearchTextTool(workspace_root))
    registry.register(FindDefinitionTool(workspace_root, ctags_path=ctags_path, index_dir=index_dir))
    registry.register(SearchCodebaseTool(workspace_root, index_dir=index_dir))
    registry.register(FindReferencesTool(workspace_root))
    registry.register(RepoMapTool(workspace_root))
    registry.register(GetFileRelationsTool(workspace_root))
    registry.register(WriteFileTool(workspace_root))
    registry.register(ApplyPatchTool(workspace_root))
    registry.register(MultiEditTool(workspace_root))
    registry.register(ReplaceLinesTool(workspace_root))
    registry.register(RunCommandTool(workspace_root))
    return registry


def _default_system_prompt(workspace_root: str, registry: ToolRegistry) -> str:
    """生成描述工具列表、行为准则与代码修改工作流的系统提示（英文，供 LLM 使用）。"""
    tool_lines = "\n".join(
        f"  - {tool.name}: {tool.description}"
        for tool in registry.list_tools()
    )
    return (
        f"You are a coding assistant. Workspace root: {workspace_root}\n\n"
        "## Available Tools\n"
        f"{tool_lines}\n\n"
        "## Behavior Guidelines\n"
        "1. Prefer using tools to gather the information you need before answering; "
        "never guess at file contents.\n"
        "2. Once you have enough information, give your final answer directly without "
        "calling more tools.\n"
        "3. If a tool returns an error, report it to the user honestly and explain why.\n\n"
        "## Code Change Workflow (must be followed strictly whenever you modify files)\n\n"
        "**Step 1: Read**\n"
        "Before modifying any file, you must first read its full contents with read_file.\n"
        "Use the code-intelligence tools to locate code instead of blindly search_text-ing "
        "everything:\n"
        "  - find_definition(name): find a definition by symbol name (function/class/type);\n"
        "  - find_references(name): find call sites/references;\n"
        "  - search_codebase(query_text): when you don't know the exact symbol name, search "
        "fuzzily with natural language or an error snippet;\n"
        "  - get_file_relations(file): see what a file imports and what imports it (assess "
        "the blast radius of a change);\n"
        "  - repo_map(): before making changes, pull a global symbol map to build overall "
        "understanding.\n"
        "If you still need text-level search or a directory listing, use search_text / "
        "list_files.\n\n"
        "**Step 2: Make a plan**\n"
        "Briefly state your change plan in your reply (1-3 sentences): what to change, "
        "where, and why.\n"
        "No separate tool call is needed for this — just write it in text.\n\n"
        "**Step 3: Make the change**\n"
        "- Prefer the editing tools for existing files; only use write_file when creating a "
        "brand-new file.\n"
        "- For a single change, use apply_patch (keep old_str close to the original text; "
        "minor whitespace differences are matched fuzzily).\n"
        "- For multiple changes in the same file, use multi_edit (submitted as one atomic "
        "batch — all changes succeed or none are written).\n"
        "- If you already know the exact line numbers, use replace_lines (replace by line "
        "range, no need for exact text matching).\n\n"
        "**Step 4: Verify**\n"
        "After making changes, you must run at least one verification command, for example:\n"
        "  - python -m compileall <dir>    (syntax check)\n"
        "  - python -m unittest <module>   (unit tests)\n"
        "  - python <file>                 (run the script to verify)\n\n"
        "**Step 5: Summarize**\n"
        "Your final reply must include the following structured content:\n\n"
        "**Summary of Changes**\n"
        "- Files changed: <all modified file paths>\n"
        "- Reason for change: <explain why you made this change>\n"
        "- Verification command: <the command you ran and a summary of its output>\n"
        "- Residual risk: <describe any potential issues, or write 'None'>"
    )


class CodingAgent(Agent):
    """编程 Agent：通过 ReAct 循环调用工具完成代码任务。

    用法示例：
    ```python
    from core.llm import AgentsLLM
    from agents import CodingAgent

    llm = AgentsLLM()
    agent = CodingAgent(name="coder", llm=llm, workspace_root=".")
    result = agent.run("请读取 core/message.py 并说明 Message.from_dict 的作用。")
    print(result)
    ```
    """

    # 改文件类工具（执行前抓快照、确认时展示 diff 预览）
    _EDIT_TOOLS: frozenset = frozenset(
        {"write_file", "apply_patch", "multi_edit", "replace_lines"}
    )
    # 执行前需要用户确认的工具集合（所有改文件操作 + 运行命令）
    _CONFIRM_TOOLS: frozenset = _EDIT_TOOLS | frozenset({"run_command"})

    def __init__(
        self,
        name: str,
        llm: AgentsLLM,
        workspace_root: str = ".",
        max_steps: int = 20,
        system_prompt: Optional[str] = None,
        tool_registry: Optional[ToolRegistry] = None,
        config: Optional[Config] = None,
        verbose: bool = False,
        require_confirm: bool = False,
        auto_approve: bool = False,
        max_concurrency: int = 4,
        tool_timeout: Optional[float] = None,
        llm_timeout: Optional[float] = None,
        project_context: bool = False,
        track_edits: bool = False,
        llm_max_retries: int = 2,
        retry_backoff: float = 0.5,
        token_budget: Optional[int] = None,
        enable_subagent: bool = False,
        mcp_servers: Optional[list[dict]] = None,
        verify_command: Optional[str] = None,
    ) -> None:
        cfg = config if config is not None else Config()
        # 提前置位：MCP 连接发生在 super().__init__ 之前，需要 _log / _trace 可用
        self._mcp_clients: list = []
        self.verbose = verbose
        self.tracer: Optional[TraceLogger] = None
        registry = tool_registry or _build_default_registry(workspace_root, cfg)

        # 子 Agent 编排：注册委派工具，让 LLM 可把子任务交给独立子 Agent
        if enable_subagent:
            from .subagent import SubAgentTool  # 局部导入避免循环依赖
            registry.register(SubAgentTool(
                llm=self._build_subagent_llm(llm, cfg),
                workspace_root=workspace_root,
                max_steps=cfg.subagent_max_steps,
                name=f"{name}-sub",
                parent=self,  # 子 Agent 继承父级确认/auto 安全姿态
            ))

        # P3 可选工具：按 config 开关注册（在建 prompt 前，使工具列表完整）
        if cfg.todowrite_enabled:
            registry.register(TodoWriteTool(cfg.todowrite_persistence_dir, name))
        if cfg.devlog_enabled:
            registry.register(DevLogTool(cfg.devlog_persistence_dir, name))
        if cfg.skills_enabled:
            registry.register(SkillTool(SkillLoader(cfg.skills_dir)))

        # MCP：作为客户端连接外部 server，把其工具注册进来（在建 prompt 前，使工具列表完整）。
        # 来源：显式 mcp_servers 参数优先，否则回退 config.mcp_servers（.capybara.toml 配置驱动），
        # 这样任何传 config=cfg 的调用方（CLI / tui_backend / 库用户）都能用配置文件配 MCP。
        mcp_specs = mcp_servers if mcp_servers is not None else (getattr(cfg, "mcp_servers", None) or [])
        for spec in mcp_specs:
            self._connect_mcp_server(registry, spec)

        # 项目上下文自举：检测语言/测试命令/关键文件，注入默认 system prompt
        self.project_context: Optional[dict] = None
        if system_prompt is None:
            prompt = _default_system_prompt(workspace_root, registry)
            if project_context:
                self.project_context = detect_project_context(workspace_root)
                prompt = prompt + "\n\n" + format_project_context(self.project_context)
            if verify_command:
                prompt += (
                    "\n\n## Automatic Verification\n"
                    f"Before giving the final answer for this task, the system will "
                    f"automatically run `{verify_command}` to verify it; if it fails, the "
                    "failure details will be sent back to you to keep fixing. Focus on "
                    "getting the code right — you don't need to keep running this command "
                    "yourself."
                )
        else:
            prompt = system_prompt

        super().__init__(
            name=name,
            llm=llm,
            system_prompt=prompt,
            config=cfg,
            tool_registry=registry,
        )
        self.workspace_root = workspace_root
        self.max_steps = max_steps
        self.verbose = verbose
        self.require_confirm = require_confirm
        self._auto_approve = auto_approve  # 运行期可被 _confirm 的 a 选项置位
        # 异步确认钩子：同步 run() 用 input() 的 _confirm；异步 arun_stream() 路径本身
        # 没有确认环节，需要确认 UI（如 TUI 后端）时在此挂一个 async 回调
        # (tool_name, args) -> bool。默认 None → 异步路径行为与既有一致（不确认）。
        self._async_confirm: Optional[
            Callable[[str, dict], Awaitable[bool]]
        ] = None
        self.max_concurrency = max_concurrency
        self.tool_timeout = tool_timeout
        self.llm_timeout = llm_timeout

        # 改动追踪：写文件类工具执行前后快照，结束生成 diff 总结
        self._edit_tracker: Optional[EditTracker] = (
            EditTracker(workspace_root) if track_edits else None
        )

        # 稳健性：LLM 调用重试 + 熔断 + 单次任务 token 预算上限
        self.llm_max_retries = llm_max_retries
        self.retry_backoff = retry_backoff
        self.token_budget = token_budget
        self._run_tokens = 0

        # 验证—修复闭环：设了 verify_command 后，LLM 给出最终答案前由 Agent
        # 独立跑一遍验证；失败则把结构化失败回灌并继续修复，最多 max_fix_iters 轮。
        self.verify_command = verify_command
        self.max_fix_iters = self.config.max_fix_iters
        self._verify_runner = RunCommandTool(workspace_root)
        self._fix_iters = 0
        self._made_edits = False
        self._circuit: Optional[CircuitBreaker] = (
            CircuitBreaker(
                failure_threshold=self.config.circuit_failure_threshold,
                recovery_timeout=self.config.circuit_recovery_timeout,
            )
            if self.config.circuit_enabled
            else None
        )

        # 可观测性：trace 记录器（config.trace_enabled 为真时启用）
        self.tracer: Optional[TraceLogger] = None
        if self.config.trace_enabled:
            self.tracer = TraceLogger(
                trace_dir=self.config.trace_dir,
                sanitize=self.config.trace_sanitize,
            )

        # 自动保存：每 N 条消息落盘一次（固定文件名，覆盖式）
        self._autosave_name = f"autosave-{name}"

        # 文件读缓存：记录 read_file 成功读过的文件 size/mtime，随会话落盘，
        # 恢复时可比对磁盘是否漂移（见 _get_read_cache / load_session）。
        self._read_cache: dict[str, dict] = {}

    @staticmethod
    def _build_subagent_llm(main_llm: AgentsLLM, cfg: Config) -> AgentsLLM:
        """按需为子 Agent 构造轻量 LLM；缺凭证或构造失败一律回退主 LLM。

        仅当 config.subagent_use_light_llm 为真、且主 LLM 暴露 api_key/base_url
        时，才用 subagent_light_llm_model 新建一个 AgentsLLM 复用同一服务端点。
        任何异常都安全回退为 main_llm，确保 StubLLM 等无凭证场景行为不变。
        """
        if not cfg.subagent_use_light_llm:
            return main_llm
        api_key = getattr(main_llm, "api_key", None)
        base_url = getattr(main_llm, "base_url", None)
        if not api_key or not base_url:
            return main_llm
        try:
            return AgentsLLM(
                model=cfg.subagent_light_llm_model,
                api_key=api_key,
                base_url=base_url,
            )
        except Exception:
            return main_llm

    @classmethod
    def production(
        cls,
        name: str,
        llm: AgentsLLM,
        workspace_root: str = ".",
        *,
        require_confirm: bool = True,
        token_budget: Optional[int] = 200_000,
        config: Optional[Config] = None,
        **overrides,
    ) -> "CodingAgent":
        """生产预设：一行拿到把全栈能力协同打开的 CodingAgent。

        默认开启：可观测（trace）+ 持久（会话自动保存）+ 自举（项目上下文）
        + 安全（确认/auto）+ 改动追踪 + 子 Agent + 预算/并发。
        Config 相关开关在副本上设置（不污染传入的 config）；其余构造参数可经
        关键字 **overrides 覆盖（覆盖优先）。

        ```python
        agent = CodingAgent.production("coder", AgentsLLM(), workspace_root="./proj")
        print(agent.run("给项目加一个 CLI 入口并跑测试验证"))
        ```
        """
        cfg = (config.model_copy() if config is not None else Config())
        cfg.trace_enabled = True
        cfg.auto_save_enabled = True
        cfg.todowrite_enabled = True
        cfg.devlog_enabled = True
        # skills 需要真实存在的目录，否则保持关闭以免空目录场景出错
        if os.path.isdir(cfg.skills_dir):
            cfg.skills_enabled = True
        # 代码智能：ctags 路径统一由 config.ctags_path（.capybara.toml）提供，或走系统 PATH；
        # 不再读环境变量（单一配置来源，见 resolve_ctags）。

        kwargs = dict(
            verbose=True,
            require_confirm=require_confirm,
            project_context=True,
            track_edits=True,
            enable_subagent=True,
            max_concurrency=4,
            token_budget=token_budget,
        )
        kwargs.update(overrides)
        return cls(
            name=name,
            llm=llm,
            workspace_root=workspace_root,
            config=cfg,
            **kwargs,
        )

    def add_message(self, message: Message) -> None:
        """追加消息并按需触发自动保存。"""
        super().add_message(message)
        self._maybe_autosave()

    def _maybe_autosave(self) -> None:
        """若启用自动保存，每 auto_save_interval 条消息保存一次（失败不影响主流程）。"""
        if not self.config.auto_save_enabled or self.session_store is None:
            return
        interval = max(1, self.config.auto_save_interval)
        n = len(self._messages)
        if n > 0 and n % interval == 0:
            try:
                self.save_session(self._autosave_name)
                self._trace("auto_saved", messages=n, name=self._autosave_name)
            except Exception:
                pass

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _trace(self, event: str, **data) -> None:
        """旁路记录 trace 事件；tracer 未启用或出错都不影响主流程。"""
        if self.tracer is None:
            return
        try:
            self.tracer.record(event, **data)
        except Exception:
            pass

    def export_trace_html(self) -> Optional[str]:
        """导出当前 trace 的 HTML 报告，返回路径（未启用 trace 时返回 None）。"""
        if self.tracer is None:
            return None
        return self.tracer.write_html_report(
            include_raw=self.config.trace_html_include_raw_response
        )

    # ------------------------------------------------------------------
    # MCP：作为客户端消费外部 server 的工具
    # ------------------------------------------------------------------

    def _connect_mcp_server(self, registry: ToolRegistry, spec: dict) -> list[str]:
        """启动并连接一个 MCP server，把其工具注册进 registry，返回注册名列表。

        spec 形如：{"name", "command", "args", "env", "cwd", "timeout", "prefix"}。
        默认前缀 `{name}__`（无 name 时为 `mcp__`），避免与内置工具或多 server 重名。
        """
        from core.mcp import MCPClient
        from .mcp_tool import register_mcp_tools

        name = spec.get("name") or spec.get("command", "mcp")
        client = MCPClient(
            command=spec["command"],
            args=spec.get("args"),
            env=spec.get("env"),
            cwd=spec.get("cwd"),
            timeout=spec.get("timeout", 30.0),
            name=name,
        )
        client.start()
        self._mcp_clients.append(client)

        prefix = spec.get("prefix")
        if prefix is None:
            prefix = f"{name}__" if name else "mcp__"
        names = register_mcp_tools(registry, client, prefix=prefix)

        self._log(f"  [MCP] 已连接 {name}，注册 {len(names)} 个工具：{', '.join(names)}")
        self._trace("mcp_connected", server=name, tools=names)
        return names

    def add_mcp_server(
        self,
        command: str,
        *,
        args: Optional[list] = None,
        env: Optional[dict] = None,
        cwd: Optional[str] = None,
        name: Optional[str] = None,
        prefix: Optional[str] = None,
        timeout: float = 30.0,
    ) -> list[str]:
        """运行期连接一个 MCP server 并注册其工具，返回注册名列表。

        注册后的工具经 `get_schemas()` 立即对 LLM 可见（无需重建 prompt）。
        """
        spec = {
            "command": command, "args": args, "env": env, "cwd": cwd,
            "name": name, "prefix": prefix, "timeout": timeout,
        }
        return self._connect_mcp_server(self.tool_registry, spec)

    def close(self) -> None:
        """关闭所有 MCP server 子进程（幂等）。建议在用完 Agent 后调用。"""
        for client in self._mcp_clients:
            try:
                client.close()
            except Exception:
                pass
        self._mcp_clients = []

    def _maybe_snapshot(self, tool_name: str, args: dict) -> None:
        """写文件类工具执行前，抓取目标文件原内容快照（用于 diff 总结）。"""
        if self._edit_tracker is None:
            return
        if tool_name in self._EDIT_TOOLS and isinstance(args.get("path"), str):
            self._edit_tracker.snapshot(args["path"])

    def get_last_diff_summary(self) -> str:
        """返回上一次任务的改动 diff 总结（未启用追踪时给出提示）。"""
        if self._edit_tracker is None:
            return "改动追踪未启用：构造时传 track_edits=True 可开启。"
        return self._edit_tracker.format_summary()

    def _record_read(self, tool_name: str, args: dict) -> None:
        """read_file 成功后记录该文件的 size/mtime，供会话持久化与漂移检测。

        任何异常都吞掉——读缓存是旁路信息，绝不能影响主流程。
        """
        if tool_name != "read_file":
            return
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return
        try:
            st = os.stat(os.path.join(self.workspace_root, path))
            self._read_cache[path] = {"size": st.st_size, "mtime": st.st_mtime}
        except Exception:
            pass

    def _get_read_cache(self) -> dict[str, dict]:
        """覆盖基类：返回本会话 read_file 读过的文件指纹（随会话落盘）。"""
        return self._read_cache

    def _detect_read_drift(self, saved_cache: dict) -> list[str]:
        """对比保存的读缓存与当前磁盘，返回 size/mtime 已变化的文件路径列表。"""
        drifted: list[str] = []
        for path, fp in saved_cache.items():
            if not isinstance(fp, dict):
                continue
            try:
                st = os.stat(os.path.join(self.workspace_root, path))
            except Exception:
                drifted.append(path)  # 文件已不存在也算漂移
                continue
            if st.st_size != fp.get("size") or st.st_mtime != fp.get("mtime"):
                drifted.append(path)
        return drifted

    @staticmethod
    def _trunc(text: str, n: int = 80) -> str:
        """把多行内容压成单行预览并截断。"""
        s = str(text).replace("\n", "⏎")
        return s if len(s) <= n else s[:n] + "…"

    def _unified_diff(self, old: str, new: str, path: str, max_lines: int = 20) -> str:
        """生成带缩进的 unified diff 片段（截断到 max_lines 行）。"""
        diff = list(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
        ))
        if len(diff) > max_lines:
            diff = diff[:max_lines] + ["…(diff 已截断)"]
        return "\n".join("    " + line for line in diff)

    def _edit_preview(self, tool_name: str, args: dict) -> str:
        """为改文件类工具生成确认时展示的 diff/改动预览。"""
        path = args.get("path", "")
        if tool_name == "write_file":
            content = args.get("content", "")
            try:
                abs_path = os.path.join(self.workspace_root, path)
                old = (open(abs_path, encoding="utf-8").read()
                       if os.path.isfile(abs_path) else None)
            except Exception:
                old = None
            if old is None:
                lines = content.splitlines()[:8]
                body = "\n    ".join(lines)
                more = "\n    …" if len(content.splitlines()) > 8 else ""
                return f"  [确认] 新建/覆盖文件：{path}\n    {body}{more}"
            return f"  [确认] 写入文件：{path}\n{self._unified_diff(old, content, path)}"
        if tool_name == "apply_patch":
            return (f"  [确认] apply_patch：{path}\n"
                    f"    - {self._trunc(args.get('old_str', ''))}\n"
                    f"    + {self._trunc(args.get('new_str', ''))}")
        if tool_name == "multi_edit":
            edits = args.get("edits") or []
            lines = [f"  [确认] multi_edit：{path}（{len(edits)} 处）"]
            for i, e in enumerate(edits[:5], 1):
                lines.append(f"    {i}. - {self._trunc(e.get('old_str', ''))}")
                lines.append(f"       + {self._trunc(e.get('new_str', ''))}")
            if len(edits) > 5:
                lines.append("    …")
            return "\n".join(lines)
        if tool_name == "replace_lines":
            return (f"  [确认] replace_lines：{path} "
                    f"第 {args.get('start_line')}-{args.get('end_line')} 行\n"
                    f"    + {self._trunc(args.get('new_text', ''))}")
        return f"  [确认] 即将调用 {tool_name}：{args}"

    def _confirm(self, tool_name: str, args: dict) -> bool:
        """向用户展示即将执行的操作并等待确认，返回 True 表示同意。

        auto 模式下（已开启或本次选择 a）直接放行，不再询问。
        """
        # 已处于 auto 模式：直接同意，不打扰
        if self._auto_approve:
            self._log(f"  [auto] 自动同意 {tool_name}")
            return True

        print("\n" + "─" * 50, flush=True)
        if tool_name == "run_command":
            print(f"  [确认] 即将运行命令：\n    {args.get('command', '')}", flush=True)
        elif tool_name in self._EDIT_TOOLS:
            print(self._edit_preview(tool_name, args), flush=True)
        else:
            print(f"  [确认] 即将调用 {tool_name}：{args}", flush=True)
        print("─" * 50, flush=True)
        answer = input("  同意执行？[y=同意 / N=拒绝 / a=auto（后续全部同意）] ").strip().lower()

        if answer in ("a", "auto", "all", "全部"):
            self._auto_approve = True
            print("  已开启 auto 模式：后续写文件 / 运行命令不再询问。", flush=True)
            self._trace("auto_mode_enabled")
            return True
        return answer in ("y", "yes", "是")

    def _prepare_messages(self, input_text: str) -> list[dict]:
        """构建传给 LLM 的 raw message 列表，并把用户消息记入历史。

        结构：[system?] + 历史(_messages 回灌，实现多轮记忆) + [当前 user]
        """
        raw_messages: list[dict] = []
        if self.system_prompt:
            raw_messages.append({"role": "system", "content": self.system_prompt})
        for msg in self._messages:
            raw_messages.append({"role": msg.role, "content": msg.content})

        # 跨轮历史过长时压缩（只动无 tool 消息的历史，安全）
        raw_messages, compacted = compact_history(
            raw_messages,
            context_window=self.config.context_window,
            threshold=self.config.compression_threshold,
            min_retain_rounds=self.config.min_retain_rounds,
            summarizer=self._summarize_history if self.config.enable_smart_compression else None,
        )
        if compacted:
            self._log("  [上下文] 历史过长，已压缩为摘要")
            self._trace("history_compacted", remaining=len(raw_messages))

        raw_messages.append({"role": "user", "content": input_text})
        self.add_message(Message(role="user", content=input_text))
        return raw_messages

    def _truncate_tool_output(self, content: str) -> str:
        """按 config 的工具输出上限截断喂回 LLM 的内容（不改真实文件/返回对象）。"""
        text, truncated = truncate_output(
            content,
            max_lines=self.config.tool_output_max_lines,
            max_bytes=self.config.tool_output_max_bytes,
            direction=self.config.tool_output_truncate_direction,
        )
        if truncated:
            self._trace("tool_output_truncated", original_bytes=len(content.encode("utf-8")))
        return text

    def _summarize_history(self, old_messages: list[dict]) -> str:
        """跨轮历史压缩摘要（供 compact_history 用）。

        old_messages 只含 user/assistant 的纯对话轮次（compact_history 已保证无 tool
        消息），因此摘要目标是「对话层面的上下文」：用户提过什么需求、助手给过什么结论、
        有没有需要延续遵守的偏好或约定——不涉及任何工具调用细节。
        失败则回退 cheap 摘要。
        """
        try:
            convo = "\n".join(
                f"{m.get('role')}: {m.get('content', '')}" for m in old_messages
            )
            prompt = [
                {"role": "system", "content": (
                    "You are a conversation-history summarizer. Below is a transcript of "
                    "several past turns between a user and a coding assistant. Summarize it "
                    "concisely in English to help the assistant remember the key context as "
                    "the conversation continues:\n"
                    "1. What goals or requests the user raised in these turns;\n"
                    "2. What key conclusions or deliverables the assistant provided;\n"
                    "3. Any preferences or constraints the user explicitly stated that must "
                    "keep being honored going forward.\n"
                    "Do not restate specific code snippets or tool execution details — keep "
                    "only the points that are still useful for the conversation ahead, as one "
                    "concise paragraph."
                )},
                {"role": "user", "content": f"Please summarize the following conversation history:\n\n{convo}"},
            ]
            resp = self.llm.invoke(prompt)
            text = (resp.content or "").strip()
            if text:
                return f"[History Summary] {text}"
        except Exception:
            pass
        from core.context_manager import _cheap_summary
        return _cheap_summary(old_messages)

    @staticmethod
    def _render_run_messages(messages: list[dict]) -> str:
        """把 ReAct 循环内的 assistant(tool_calls)+tool 分组渲染成可读文本。

        逐条 role/content 拼接会丢掉 tool_calls 结构（assistant 纯调用工具时 content
        往往是 None）且看不出 tool 消息对应哪个工具，所以这里显式回查
        tool_call_id → 工具名，把「调用了什么工具、什么参数、返回了什么」摊平成文本，
        供 _summarize_run 的摘要 prompt 使用。
        """
        id_to_name: dict[str, str] = {}
        lines: list[str] = []
        for m in messages:
            role = m.get("role", "")
            if role == "assistant":
                content = (m.get("content") or "").strip()
                if content:
                    lines.append(f"assistant: {content}")
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name", "?")
                    id_to_name[tc.get("id")] = name
                    lines.append(f"assistant called tool: {name}({fn.get('arguments', '')})")
            elif role == "tool":
                name = id_to_name.get(m.get("tool_call_id"), "?")
                content = (m.get("content") or "").strip()
                lines.append(f"tool result[{name}]: {content}")
            else:
                content = (m.get("content") or "").strip()
                if content:
                    lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _summarize_run(self, old_messages: list[dict]) -> str:
        """单任务内压缩摘要（供 compact_run_messages 用）。

        old_messages 是 ReAct 循环里较早的 assistant(tool_calls)+tool 分组，摘要目标是
        「执行层面的进展」：做过哪些关键操作、取得了什么结果、有没有失败需要避免重蹈——
        与跨轮历史摘要关注的「对话层面的上下文」是两回事，因此不复用 _summarize_history
        的 prompt。失败则回退 cheap 摘要。
        """
        try:
            convo = self._render_run_messages(old_messages)
            prompt = [
                {"role": "system", "content": (
                    "You are the execution-progress summarizer for a coding agent. Below "
                    "are earlier execution steps from this task (tool calls and their "
                    "results). Summarize them concisely in English as a status update, so "
                    "the agent can keep completing the task without re-reading the raw "
                    "logs:\n"
                    "1. What key actions have already been taken and what results they "
                    "produced — state conclusions only, don't copy raw output, long code "
                    "blocks, or JSON;\n"
                    "2. Any failed or errored attempts — call these out explicitly so the "
                    "agent doesn't repeat the same mistake;\n"
                    "3. Current progress (done / in progress / stuck and unresolved).\n"
                    "Output one concise paragraph."
                )},
                {"role": "user", "content": f"Please summarize the following execution steps:\n\n{convo}"},
            ]
            resp = self.llm.invoke(prompt)
            text = (resp.content or "").strip()
            if text:
                return f"[Execution Summary] {text}"
        except Exception:
            pass
        from core.context_manager import _cheap_summary
        return _cheap_summary(old_messages)

    def compact_now(self) -> tuple[int, int]:
        """主动把较早的对话历史折叠成一条摘要，返回 (压缩前条数, 压缩后条数)。

        与 _prepare_messages 里「超阈值才压缩」不同，这里无视阈值强制折叠——供交互式
        TUI 的 /compact 或库用户主动调用。保留最近 min_retain_rounds 轮，更早的经
        _summarize_history（失败自动回退 cheap 摘要）折成一条 system 摘要消息。历史过短
        （保留最近轮后已无可折叠内容）则原样返回、计数不变。

        只动 self._messages（纯 user/assistant 跨轮历史，不含 ReAct 内的 tool 消息），
        因此折叠安全、不会破坏 assistant↔tool 配对。
        """
        before = len(self._messages)
        raw = [{"role": m.role, "content": m.content} for m in self._messages]
        new_raw, changed = compact_history(
            raw,
            context_window=self.config.context_window,
            threshold=0.0,  # 阈值置 0 → 强制触发折叠逻辑，不看当前长度
            min_retain_rounds=self.config.min_retain_rounds,
            summarizer=(
                self._summarize_history if self.config.enable_smart_compression else None
            ),
        )
        if changed:
            self._messages = [
                Message(role=m["role"], content=m.get("content", "")) for m in new_raw
            ]
            self._trace("compacted_manually", before=before, after=len(self._messages))
        return before, len(self._messages)

    def run(self, input_text: str, **kwargs) -> str:
        """执行 ReAct 循环，返回最终答案字符串。"""
        self._log(f"\n[Agent:{self.name}] 任务开始")
        self._log(f"  > {input_text[:120]}{'...' if len(input_text) > 120 else ''}")

        if self.tracer is not None:
            self.tracer.start_run()
        self._trace("agent_start", input=input_text, mode="sync")
        self._run_tokens = 0
        self._fix_iters = 0
        self._made_edits = False
        if self._edit_tracker is not None:
            self._edit_tracker.reset()

        raw_messages = self._prepare_messages(input_text)
        prefix_len = len(raw_messages)
        schemas = self.tool_registry.get_schemas()

        current_step = 0
        try:
            for step in range(self.max_steps):
                current_step = step + 1
                self._log(f"\n[步骤 {current_step}/{self.max_steps}] LLM 思考中...")
                self._trace("step_start", step=current_step)
                self._trace(
                    "context_tokens", step=current_step,
                    tokens=estimate_tokens(raw_messages),
                )

                # 单任务内 tool 输出过多 → 折叠早期 assistant+tool 分组为摘要
                raw_messages, run_compacted = compact_run_messages(
                    raw_messages, prefix_len=prefix_len,
                    token_threshold=self._run_compaction_threshold(),
                    keep_recent_groups=self.config.run_compaction_keep_recent_groups,
                    summarizer=self._summarize_run if self.config.enable_smart_compression else None,
                )
                if run_compacted:
                    self._log("  [上下文] 单任务内工具输出过多，已折叠早期步骤为摘要")
                    self._trace(
                        "run_messages_compacted", step=current_step,
                        remaining=len(raw_messages),
                    )

                # token 预算超限 → 跳出循环，走兜底总结
                if self._over_budget():
                    self._log(f"  [预算] 已用 {self._run_tokens} token 超出上限 {self.token_budget}")
                    self._trace(
                        "budget_exceeded", step=current_step,
                        tokens=self._run_tokens, budget=self.token_budget,
                    )
                    break

                response = self._call_llm_sync(
                    lambda: self.llm.invoke_with_tools(
                        raw_messages, tools=schemas, tool_choice="auto"
                    )
                )
                self._account_tokens(response, raw_messages)
                self._trace(
                    "llm_call",
                    step=current_step,
                    model=getattr(response, "model", ""),
                    latency_ms=getattr(response, "latency_ms", 0),
                    usage=getattr(response, "usage", {}) or {},
                    has_tool_calls=bool(response.tool_calls),
                    num_messages=len(raw_messages),
                )

                # LLM 不再调用工具 → 最终答案（先过验证闸门）
                if not response.tool_calls:
                    action, note = self._verify_gate(raw_messages, current_step)
                    if action == "retry":
                        continue  # 验证失败，已回灌反馈，继续修复
                    self._log(f"[步骤 {current_step}] 最终答案已生成（{len(response.content or '')} 字符）")
                    result = (response.content or "") + note
                    self.add_message(Message(role="assistant", content=result))
                    self._record_edit_summary()
                    self._trace("agent_finish", step=current_step, result=result)
                    return result

                self._log(
                    f"[步骤 {current_step}] LLM 决定调用 {len(response.tool_calls)} 个工具："
                    + ", ".join(tc.name for tc in response.tool_calls)
                )

                # 把 assistant 消息（含 tool_calls 结构）追加到 raw_messages
                raw_messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in response.tool_calls
                    ],
                })

                # 执行每个工具调用，把结果追加到 raw_messages
                for tc in response.tool_calls:
                    try:
                        args = json.loads(tc.arguments) if tc.arguments else {}
                    except json.JSONDecodeError:
                        args = {}

                    # 打印工具调用参数（截断长值）
                    args_preview = ", ".join(
                        f"{k}={repr(v)[:60]}" for k, v in args.items()
                    )
                    self._log(f"  → 调用 {tc.name}({args_preview})")

                    # 需要用户确认的工具：拒绝时注入失败结果，跳过实际调用
                    if self.require_confirm and tc.name in self._CONFIRM_TOOLS:
                        if not self._confirm(tc.name, args):
                            self._log(f"  ✗ {tc.name} 被用户取消")
                            self._trace(
                                "tool_call", step=current_step, name=tc.name,
                                arguments=args, ok=False, summary="用户取消",
                            )
                            raw_messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": "[用户取消] 操作被用户拒绝，请调整方案或向用户说明原因。",
                            })
                            continue

                    # 写文件类工具：执行前抓快照，供结束后 diff 总结
                    self._maybe_snapshot(tc.name, args)
                    tool_result = self.tool_registry.call(tc.name, **args)

                    if tool_result.ok:
                        self._record_read(tc.name, args)
                        if tc.name in self._EDIT_TOOLS:
                            self._made_edits = True
                        preview = tool_result.content.replace("\n", " ")[:80]
                        self._log(f"  ✓ {tc.name} 成功（{len(tool_result.content)} 字符）: {preview}...")
                        content = self._truncate_tool_output(tool_result.content)
                        self._trace(
                            "tool_call", step=current_step, name=tc.name,
                            arguments=args, ok=True,
                            summary=tool_result.content[:200],
                        )
                    else:
                        self._log(f"  ✗ {tc.name} 失败: {tool_result.error}")
                        content = f"[工具错误] {tool_result.error}"
                        self._trace(
                            "tool_call", step=current_step, name=tc.name,
                            arguments=args, ok=False, summary=tool_result.error,
                        )

                    raw_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    })

            # 超出 max_steps → 强制一次无工具调用，获取基于已有信息的总结
            self._log(f"\n[Agent:{self.name}] 已达到最大步数 {self.max_steps}，生成兜底答案...")
            raw_messages.append({
                "role": "user",
                "content": "已达到最大步数，请根据目前已获取的信息给出最终答案。",
            })
            final = self._call_llm_sync(lambda: self.llm.invoke(raw_messages))
            self._trace(
                "llm_call", step=current_step,
                model=getattr(final, "model", ""),
                latency_ms=getattr(final, "latency_ms", 0),
                usage=getattr(final, "usage", {}) or {},
                has_tool_calls=False, num_messages=len(raw_messages),
                fallback=True,
            )
            result = final.content or ""
            self.add_message(Message(role="assistant", content=result))
            self._record_edit_summary()
            self._trace(
                "agent_finish", step=current_step, result=result,
                max_steps_reached=True,
            )
            return result

        except Exception as exc:
            self._trace(
                "agent_error", step=current_step,
                error=str(exc), error_type=type(exc).__name__,
            )
            raise

    def _record_edit_summary(self) -> None:
        """若启用改动追踪，把本次变更文件列表记入 trace。"""
        if self._edit_tracker is None:
            return
        files = self._edit_tracker.changed_files()
        if files:
            self._trace("edit_summary", files=files, count=len(files))

    # ------------------------------------------------------------------
    # 验证—修复闭环
    # ------------------------------------------------------------------

    def _verify_gate(self, raw_messages: list, step: int) -> tuple[str, str]:
        """最终答案前的验证闸门，返回 (action, note)。

        action == "retry"：验证失败且未达上限，已把失败反馈回灌 raw_messages，应继续循环。
        action == "accept"：可接受最终答案；note 为附加到答案末尾的提示（仅在达上限仍失败时非空）。

        仅在「设了 verify_command 且本轮确实改过文件」时触发，避免对纯问答跑测试。
        """
        if not self.verify_command or not self._made_edits:
            return ("accept", "")

        result = self._run_verify(step)
        if result.passed:
            self._log(f"  [验证] 通过：{result.summary}")
            self._trace("verify_passed", step=step, summary=result.summary)
            return ("accept", "")

        if self._fix_iters < self.max_fix_iters:
            self._fix_iters += 1
            self._log(
                f"  [验证] 失败（第 {self._fix_iters}/{self.max_fix_iters} 轮修复）：{result.summary}"
            )
            self._trace(
                "verify_failed", step=step, iteration=self._fix_iters,
                summary=result.summary, failures=result.failures,
            )
            raw_messages.append({"role": "user", "content": self._verify_feedback(result)})
            return ("retry", "")

        # 已达修复上限仍未通过：接受答案，但附加提示
        self._log(f"  [验证] 仍未通过，已达修复上限 {self.max_fix_iters}：{result.summary}")
        self._trace(
            "verify_exhausted", step=step,
            iterations=self._fix_iters, summary=result.summary,
        )
        note = (
            f"\n\n[自动验证] 注意：`{self.verify_command}` 仍未通过"
            f"（已达修复上限 {self.max_fix_iters} 轮）：{result.summary}"
        )
        return ("accept", note)

    def _run_verify(self, step: int):
        """运行 verify_command 并解析结果（失败输出在 ToolResult.error 中）。"""
        tr = self._verify_runner.run(command=self.verify_command)
        exit_code = tr.metadata.get("exit_code", 0 if tr.ok else 1)
        output = tr.content if tr.ok else (tr.error or "")
        result = parse_test_output(output, exit_code)
        self._trace(
            "verify_run", step=step, command=self.verify_command,
            passed=result.passed, framework=result.framework, summary=result.summary,
        )
        return result

    def _verify_feedback(self, result) -> str:
        """把验证失败渲染成回灌给 LLM 的修复提示。"""
        parts = [
            f"[自动验证] 运行 `{self.verify_command}` 未通过"
            f"（第 {self._fix_iters}/{self.max_fix_iters} 轮修复）。",
            f"结果：{result.summary}",
        ]
        if result.failures:
            parts.append("失败项：")
            parts.extend(f"  - {f}" for f in result.failures)
        if result.raw_tail:
            parts.append("输出尾部：")
            parts.append(result.raw_tail)
        parts.append(
            "请根据以上失败继续修改代码修复问题；改完不必自己跑测试，我会自动重新验证。"
        )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 异步 / 流式入口
    # ------------------------------------------------------------------

    def _event(self, event_type: EventType, **data) -> AgentEvent:
        """构造一个 AgentEvent（带 agent 名称）。"""
        return AgentEvent.create(event_type, self.name, **data)

    def _circuit_guard(self) -> None:
        """熔断器打开时快速失败。"""
        if self._circuit is not None and not self._circuit.allow():
            raise CircuitBreakerOpen(
                f"熔断器已打开（连续失败达 {self._circuit.failure_threshold} 次），暂时拒绝 LLM 调用"
            )

    def _on_retry(self, attempt: int, exc: Exception) -> None:
        self._log(f"  [重试] 第 {attempt} 次重试，因 {type(exc).__name__}: {exc}")
        self._trace(
            "llm_retry", attempt=attempt,
            error=str(exc), error_type=type(exc).__name__,
        )

    def _call_llm_sync(self, fn):
        """同步 LLM 调用：熔断检查 + 重试 + 成功/失败计入熔断器。"""
        self._circuit_guard()
        try:
            result = retry_call(
                fn, max_retries=self.llm_max_retries,
                backoff=self.retry_backoff, on_retry=self._on_retry,
            )
        except Exception:
            if self._circuit is not None:
                self._circuit.record_failure()
            raise
        if self._circuit is not None:
            self._circuit.record_success()
        return result

    async def _call_llm_async(self, make_coro):
        """异步 LLM 调用：熔断检查 + 超时 + 重试 + 成功/失败计入熔断器。"""
        self._circuit_guard()

        async def attempt():
            coro = make_coro()
            if self.llm_timeout is None:
                return await coro
            return await asyncio.wait_for(coro, timeout=self.llm_timeout)

        try:
            result = await aretry_call(
                attempt, max_retries=self.llm_max_retries,
                backoff=self.retry_backoff, on_retry=self._on_retry,
            )
        except Exception:
            if self._circuit is not None:
                self._circuit.record_failure()
            raise
        if self._circuit is not None:
            self._circuit.record_success()
        return result

    def _account_tokens(self, response, messages) -> None:
        """累计本次任务消耗的 token（优先用 usage，否则估算）。"""
        usage = getattr(response, "usage", {}) or {}
        n = usage.get("total_tokens") or 0
        if not n:
            n = estimate_tokens(messages)
        self._run_tokens += n

    def _over_budget(self) -> bool:
        return self.token_budget is not None and self._run_tokens > self.token_budget

    def _run_compaction_threshold(self) -> int:
        """单任务内压缩的绝对 token 阈值：显式配置优先，否则取 context_window*compression_threshold。"""
        explicit = self.config.run_compaction_token_threshold
        if explicit is not None:
            return explicit
        return int(self.config.context_window * self.config.compression_threshold)

    async def _run_one_tool(self, tc, sem: asyncio.Semaphore):
        """执行单个工具调用，返回 (tc, content, ok)。异常/超时不抛出，转为失败结果。"""
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except json.JSONDecodeError:
            args = {}

        # 需要确认的工具（写文件/运行命令）：挂了异步确认钩子时，执行前先征询用户。
        # 放在 async with sem 之前，避免确认期间占用并发槽。拒绝则不执行、返回与
        # 同步路径一致的取消结果（钩子异常一律按拒绝处理，安全优先）。
        if (
            self.require_confirm
            and self._async_confirm is not None
            and tc.name in self._CONFIRM_TOOLS
        ):
            try:
                approved = await self._async_confirm(tc.name, args)
            except Exception:
                approved = False
            if not approved:
                return tc, "[用户取消] 操作被用户拒绝，请调整方案或向用户说明原因。", False

        # 写文件类工具：执行前抓快照，供结束后 diff 总结
        self._maybe_snapshot(tc.name, args)

        async with sem:
            try:
                coro = self.tool_registry.acall(tc.name, **args)
                if self.tool_timeout is not None:
                    result = await asyncio.wait_for(coro, timeout=self.tool_timeout)
                else:
                    result = await coro
            except asyncio.TimeoutError:
                return tc, f"[工具超时] {tc.name} 超过 {self.tool_timeout}s 未返回", False
            except Exception as exc:  # 工具崩溃不应中断整个事件流
                return tc, f"[工具错误] {type(exc).__name__}: {exc}", False

        if result.ok:
            self._record_read(tc.name, args)
            return tc, self._truncate_tool_output(result.content), True
        return tc, f"[工具错误] {result.error}", False

    async def _run_tools_concurrently(self, tool_calls):
        """并发执行一批工具调用（受 max_concurrency 限制），结果按原顺序返回。"""
        sem = asyncio.Semaphore(self.max_concurrency)
        tasks = [self._run_one_tool(tc, sem) for tc in tool_calls]
        return await asyncio.gather(*tasks)

    async def arun_stream(
        self, input_text: str, **kwargs
    ) -> AsyncGenerator[AgentEvent, None]:
        """异步执行 ReAct 循环，逐事件 yield AgentEvent，供实时展示。

        事件序列（典型）：
          AGENT_START → (STEP_START → LLM_START → LLM_FINISH
                          → [TOOL_CALL* → TOOL_RESULT/TOOL_ERROR*] → STEP_FINISH)*
          → LLM_CHUNK* → AGENT_FINISH
        出错时发 AGENT_ERROR 后向上抛出。工具失败/超时只发 TOOL_ERROR，不中断流。
        """
        if self.tracer is not None:
            self.tracer.start_run()
        self._trace("agent_start", input=input_text, mode="async")
        self._run_tokens = 0
        if self._edit_tracker is not None:
            self._edit_tracker.reset()

        yield self._event(EventType.AGENT_START, input_text=input_text)
        current_step = 0
        try:
            raw_messages = self._prepare_messages(input_text)
            prefix_len = len(raw_messages)
            schemas = self.tool_registry.get_schemas()

            for step in range(self.max_steps):
                current_step = step + 1
                yield self._event(EventType.STEP_START, step=step + 1)
                self._trace("step_start", step=step + 1)
                self._trace(
                    "context_tokens", step=step + 1,
                    tokens=estimate_tokens(raw_messages),
                )

                # 单任务内 tool 输出过多 → 折叠早期 assistant+tool 分组为摘要
                raw_messages, run_compacted = compact_run_messages(
                    raw_messages, prefix_len=prefix_len,
                    token_threshold=self._run_compaction_threshold(),
                    keep_recent_groups=self.config.run_compaction_keep_recent_groups,
                    summarizer=self._summarize_run if self.config.enable_smart_compression else None,
                )
                if run_compacted:
                    self._trace(
                        "run_messages_compacted", step=step + 1,
                        remaining=len(raw_messages),
                    )

                # token 预算超限 → 跳出循环，走兜底总结
                if self._over_budget():
                    self._trace(
                        "budget_exceeded", step=step + 1,
                        tokens=self._run_tokens, budget=self.token_budget,
                    )
                    break

                yield self._event(EventType.LLM_START, step=step + 1)

                response = await self._call_llm_async(
                    lambda: self.llm.ainvoke_with_tools(
                        raw_messages, tools=schemas, tool_choice="auto"
                    )
                )
                self._account_tokens(response, raw_messages)
                self._trace(
                    "llm_call",
                    step=step + 1,
                    model=getattr(response, "model", ""),
                    latency_ms=getattr(response, "latency_ms", 0),
                    usage=getattr(response, "usage", {}) or {},
                    has_tool_calls=bool(response.tool_calls),
                    num_messages=len(raw_messages),
                )
                yield self._event(
                    EventType.LLM_FINISH,
                    step=step + 1,
                    has_tool_calls=bool(response.tool_calls),
                    usage=getattr(response, "usage", {}) or {},
                )

                # 最终答案：切片成 LLM_CHUNK 逐段输出
                if not response.tool_calls:
                    result = response.content or ""
                    for piece in _chunk_text(result):
                        yield self._event(EventType.LLM_CHUNK, text=piece)
                    self.add_message(Message(role="assistant", content=result))
                    self._record_edit_summary()
                    self._trace("agent_finish", step=step + 1, result=result)
                    yield self._event(EventType.AGENT_FINISH, result=result)
                    return

                # 追加 assistant（含 tool_calls）到上下文
                raw_messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in response.tool_calls
                    ],
                })

                for tc in response.tool_calls:
                    yield self._event(
                        EventType.TOOL_CALL, name=tc.name, arguments=tc.arguments
                    )

                # 并发执行，按原顺序回灌结果
                results = await self._run_tools_concurrently(response.tool_calls)
                for tc, content, ok in results:
                    if ok:
                        yield self._event(
                            EventType.TOOL_RESULT, name=tc.name, content=content
                        )
                    else:
                        yield self._event(
                            EventType.TOOL_ERROR, name=tc.name, error=content
                        )
                    self._trace(
                        "tool_call", step=step + 1, name=tc.name,
                        ok=ok, summary=content[:200],
                    )
                    raw_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    })

                yield self._event(EventType.STEP_FINISH, step=step + 1)

            # 超出 max_steps → 兜底一次无工具调用
            raw_messages.append({
                "role": "user",
                "content": "已达到最大步数，请根据目前已获取的信息给出最终答案。",
            })
            final = await self._call_llm_async(lambda: self.llm.ainvoke(raw_messages))
            self._trace(
                "llm_call", step=current_step,
                model=getattr(final, "model", ""),
                latency_ms=getattr(final, "latency_ms", 0),
                usage=getattr(final, "usage", {}) or {},
                has_tool_calls=False, num_messages=len(raw_messages),
                fallback=True,
            )
            result = final.content or ""
            for piece in _chunk_text(result):
                yield self._event(EventType.LLM_CHUNK, text=piece)
            self.add_message(Message(role="assistant", content=result))
            self._record_edit_summary()
            self._trace(
                "agent_finish", step=current_step, result=result,
                max_steps_reached=True,
            )
            yield self._event(
                EventType.AGENT_FINISH, result=result, max_steps_reached=True
            )

        except Exception as exc:
            self._trace(
                "agent_error", step=current_step,
                error=str(exc), error_type=type(exc).__name__,
            )
            yield self._event(
                EventType.AGENT_ERROR,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    async def arun(self, input_text: str, **kwargs) -> str:
        """异步执行并返回最终答案（消费 arun_stream，原生异步 + 并发 + 超时）。"""
        result = ""
        async for event in self.arun_stream(input_text, **kwargs):
            if event.type == EventType.AGENT_FINISH:
                result = event.data.get("result", "")
        return result

    # ------------------------------------------------------------------
    # 会话持久化与恢复
    # ------------------------------------------------------------------

    def _get_agent_config(self) -> dict:
        """在基类配置基础上补充 max_steps / workspace_root，供一致性检查比对。"""
        config = super()._get_agent_config()
        config["max_steps"] = self.max_steps
        config["workspace_root"] = self.workspace_root
        return config

    def load_session(self, filepath: str) -> list[str]:
        """加载会话并恢复历史；先做环境一致性检查，返回（并打印）警告列表。

        与基类不同：加载前比对保存时的配置 / 工具 schema 与当前环境，
        若模型、最大步数或工具定义发生变化则给出警告，避免在不一致环境下盲目续跑。
        """
        if not self.session_store:
            raise ConfigException("会话持久化未启用：请用 Config(session_enabled=True)。")

        data = self.session_store.load(filepath)

        warnings: list[str] = []

        # 配置一致性（模型 / max_steps / provider）
        cfg_check = self.session_store.check_config_consistency(
            data.get("agent_config", {}), self._get_agent_config()
        )
        warnings.extend(cfg_check["warnings"])

        # 工具 schema 一致性
        tool_check = self.session_store.check_tool_schema_consistency(
            data.get("tool_schema_hash", ""), self._compute_tool_schema_hash()
        )
        if tool_check["changed"]:
            warnings.append(
                f"工具定义已变化（schema 不一致）：{tool_check['recommendation']}"
            )

        # 读缓存漂移：保存时读过的文件，若磁盘 size/mtime 已变，提示重新读取
        drifted = self._detect_read_drift(data.get("read_cache", {}) or {})
        if drifted:
            warnings.append(
                f"曾读取的 {len(drifted)} 个文件在磁盘上已变化，建议重新读取："
                + "、".join(drifted[:5])
                + ("…" if len(drifted) > 5 else "")
            )

        # 还原历史与 metadata
        self._messages = [
            Message.from_dict(m) for m in data.get("history", [])
        ]
        self._session_metadata = data.get("metadata", {})
        restored_cache = data.get("read_cache", {})
        if isinstance(restored_cache, dict):
            self._read_cache = restored_cache

        for w in warnings:
            print(f"⚠️  {w}", flush=True)
        if not warnings:
            print("✅ 环境一致，已恢复会话。", flush=True)
        return warnings
