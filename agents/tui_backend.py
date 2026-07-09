"""Capybara 交互式 TUI 的 Python 后端 —— headless，over-stdio JSON-lines 协议。

由 Ink 前端（`ui/index.js`）作为子进程拉起：前端把用户输入 / 确认应答以 JSON 行写到
本进程 stdin；本进程把 `CodingAgent.arun_stream` 的事件、确认请求、轮次结束等以 JSON
行写回 stdout。**stdout 只承载协议 JSON**——构造后把 `sys.stdout` 重定向到 stderr，
agent 的任何杂散输出都不会污染协议流。

协议（每行一个 JSON）：
    前端 → 后端： {"type":"user_message","text":...}
                 {"type":"confirm_response","id":...,"approved":bool}
                 {"type":"command","name":"compact|skills|diff|codeintel|trace"}
                 {"type":"reset"}          # 清空跨轮对话历史
                 {"type":"shutdown"}
    后端 → 前端： {"type":"ready","model":...,"workspace":...}
                 {"type":"agent_event","event": AgentEvent.to_dict()}
                 {"type":"confirm_request","id":...,"tool":...,"preview":...}
                 {"type":"notice","format":"plain|diff","title":...,"text":...}
                 {"type":"usage","used":...,"window":...,"percent":...}
                 {"type":"turn_done"}
                 {"type":"error","message":...}

跨轮记忆由 CodingAgent.self._messages 天然保留，因此这个后端进程活多久，对话上下文就
连续多久。stdin 用后台线程按行读（Windows 上 asyncio 直接读管道 stdin 不可靠），经
线程安全队列喂给事件循环。
"""

import argparse
import asyncio
import json
import os
import sys
import threading
from typing import Callable, Optional

from core.llm import AgentsLLM
from .coding_agent import CodingAgent
from .skills import SkillLoader
from .__main__ import _resolve_config
from tools.code_intel import resolve_ctags, treesitter_available, fts5_available


class TUIBackend:
    """把一个 CodingAgent 包装成 stdio JSON 协议服务；send 可注入以便测试。"""

    def __init__(self, agent: CodingAgent, send: Callable[[dict], None]) -> None:
        self.agent = agent
        self._send = send
        self._pending: dict[str, asyncio.Future] = {}
        self._turn_task: Optional[asyncio.Task] = None
        self._confirm_seq = 0
        # 挂上异步确认钩子：写文件 / 跑命令前经协议向前端征询
        agent._async_confirm = self._confirm

    # -- 出站 -------------------------------------------------------------

    def send_ready(self, model: str, workspace: str) -> None:
        self._send({"type": "ready", "model": model, "workspace": workspace})

    def _send_notice(self, text: str, fmt: str = "plain", title: Optional[str] = None) -> None:
        """把一条告示（命令结果 / 本轮 diff）发回前端。format=diff → 前端彩色渲染。"""
        msg = {"type": "notice", "format": fmt, "text": text}
        if title:
            msg["title"] = title
        self._send(msg)

    def send_usage(self) -> None:
        """把当前上下文 token 用量（估算）发回前端，用于百分比显示。

        估算 = system prompt + 跨轮历史的 token，与 compact_history 的预算口径一致
        （都走 estimate_tokens 的 chars/4 启发式）；窗口取 config.context_window。
        """
        from core.context_manager import estimate_tokens
        msgs = []
        if self.agent.system_prompt:
            msgs.append({"role": "system", "content": self.agent.system_prompt})
        msgs.extend({"role": m.role, "content": m.content} for m in self.agent._messages)
        window = self.agent.config.context_window or 0
        used = estimate_tokens(msgs)
        percent = round(used / window * 100, 1) if window else 0.0
        self._send({"type": "usage", "used": used, "window": window, "percent": percent})

    def _preview(self, tool_name: str, args: dict) -> str:
        """复用 CodingAgent 的 diff / 命令预览，生成确认展示文本。"""
        if tool_name == "run_command":
            return str(args.get("command", ""))
        if tool_name in self.agent._EDIT_TOOLS:
            try:
                return self.agent._edit_preview(tool_name, args)
            except Exception:
                pass
        return json.dumps(args, ensure_ascii=False)

    # -- 确认往返 ---------------------------------------------------------

    async def _confirm(self, tool_name: str, args: dict) -> bool:
        """CodingAgent._async_confirm 钩子：发 confirm_request、在 Future 上等应答。"""
        self._confirm_seq += 1
        cid = f"confirm-{self._confirm_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        self._send({
            "type": "confirm_request",
            "id": cid,
            "tool": tool_name,
            "preview": self._preview(tool_name, args),
        })
        try:
            return bool(await fut)
        finally:
            self._pending.pop(cid, None)

    def resolve_confirm(self, cid: str, approved: bool) -> None:
        fut = self._pending.get(cid)
        if fut is not None and not fut.done():
            fut.set_result(approved)

    # -- 斜杠命令 ---------------------------------------------------------

    async def _run_command(self, name: str) -> None:
        """处理前端的后端类斜杠命令，把结果作为 notice 发回。

        命令仅在无 turn 运行时才会被前端提交（输入框只在空闲挂载），这里再加一道守卫：
        turn 进行中直接拒绝，避免 /compact 之类改到正在读的历史。
        """
        if self._turn_task is not None and not self._turn_task.done():
            self._send_notice("有任务正在进行，请稍候再试。")
            return
        try:
            if name == "compact":
                before, after = await asyncio.to_thread(self.agent.compact_now)
                self._send_notice(
                    f"已压缩对话历史：{before} 条 → {after} 条。"
                    if after < before
                    else f"历史较短，无需压缩（当前 {before} 条）。"
                )
            elif name == "skills":
                self._send_notice(self._list_skills())
            elif name == "diff":
                self._send_notice(
                    self.agent.get_last_diff_summary(), fmt="diff", title="diff_manual"
                )
            elif name == "codeintel":
                self._send_notice(self._codeintel_status())
            elif name == "trace":
                self._send_notice(self._export_trace())
            else:
                self._send_notice(f"未知命令：/{name}")
        except Exception as exc:  # 命令失败不拖垮会话
            self._send_notice(f"命令执行失败：{type(exc).__name__}: {exc}")
        self.send_usage()  # 刷新上下文用量（/compact 后尤其会变）

    def _list_skills(self) -> str:
        skills = SkillLoader(self.agent.config.skills_dir).list_skills()
        if not skills:
            return "技能库为空（skills_dir 下无 *.md，或 skills_enabled 未开启）。"
        lines = ["可用技能："]
        for s in skills:
            desc = f"：{s['description']}" if s.get("description") else ""
            lines.append(f"  · {s['name']}{desc}")
        return "\n".join(lines)

    def _codeintel_status(self) -> str:
        cfg = self.agent.config
        ctags = resolve_ctags(cfg.ctags_path)
        ts, fts = treesitter_available(), fts5_available()
        return "\n".join([
            "代码智能后端：",
            f"  {'✓' if ctags else '×'} ctags        "
            + (str(ctags) if ctags else "未找到 → find_definition 不可用"),
            f"  {'✓' if ts else '×'} tree-sitter  "
            + ("已就绪" if ts else "未安装 → find_references / repo_map / get_file_relations 不可用"),
            f"  {'✓' if fts else '×'} sqlite FTS5  "
            + ("已就绪" if fts else "当前 sqlite3 未编译 FTS5 → search_codebase 不可用"),
        ])

    def _export_trace(self) -> str:
        report = self.agent.export_trace_html()
        return f"trace HTML 报告已导出：{report}" if report else "trace 未启用。"

    # -- 单轮执行 ---------------------------------------------------------

    async def _run_turn(self, text: str) -> None:
        """跑一轮 arun_stream，逐事件转协议行；异常转 error，最后必发 turn_done。"""
        try:
            async for ev in self.agent.arun_stream(text):
                self._send({"type": "agent_event", "event": ev.to_dict()})
            self._maybe_send_turn_diff()  # 本轮若改了文件 → 末尾发一条红绿 diff
        except Exception as exc:  # 单轮失败不拖垮整个会话
            self._send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self.send_usage()  # 每轮结束刷新上下文用量百分比（turn_done 仍作最后信号）
            self._send({"type": "turn_done"})

    def _maybe_send_turn_diff(self) -> None:
        """本轮有文件改动就发一条 diff notice；无改动 / 未启用追踪则静默。"""
        tracker = getattr(self.agent, "_edit_tracker", None)
        if tracker is None:
            return
        try:
            if tracker.summary():  # {rel: unified_diff}，空 dict → 本轮无改动
                self._send_notice(tracker.format_summary(), fmt="diff", title="turn_diff")
        except Exception:
            pass

    # -- 命令分发 ---------------------------------------------------------

    async def handle(self, msg: dict) -> bool:
        """处理一条已解析的前端命令。返回 False 表示应退出主循环。

        user_message 会把一轮作为独立 task 跑起来（不阻塞本分发循环），这样即便某轮
        正 await 确认，confirm_response 仍能被及时处理并解开那个 Future。
        """
        mtype = msg.get("type")
        if mtype == "user_message":
            if self._turn_task is None or self._turn_task.done():
                self._turn_task = asyncio.create_task(
                    self._run_turn(str(msg.get("text", "")))
                )
        elif mtype == "confirm_response":
            self.resolve_confirm(str(msg.get("id", "")), bool(msg.get("approved")))
        elif mtype == "command":
            await self._run_command(str(msg.get("name", "")))
        elif mtype == "reset":
            self.agent.clear_history()
            self.send_usage()  # 历史清空 → 用量归零
        elif mtype == "shutdown":
            return False
        return True

    async def run(self, lines: asyncio.Queue) -> None:
        """主循环：从队列取 JSON 行、dispatch，直到 shutdown 或 stdin EOF（None 哨兵）。"""
        while True:
            line = await lines.get()
            if line is None:  # stdin EOF
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not await self.handle(msg):
                break
        # 收尾：取消未完成的轮次，把挂起的确认一律按拒绝解开，避免协程悬挂
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(False)


# ---------------------------------------------------------------------------
# 进程入口
# ---------------------------------------------------------------------------


def build_agent(
    workspace: str, config_path: Optional[str], max_steps: int, auto_approve: bool
) -> tuple[CodingAgent, str]:
    """构造交互式后端用的 CodingAgent，返回 (agent, 模型名)。

    verbose=False：任何过程输出都走事件流，不打印到 stdout。
    require_confirm=not auto_approve：配合异步确认钩子决定是否弹确认。
    """
    cfg = _resolve_config(workspace, config_path or None)
    # 交互式会话默认开 trace + 改动追踪：让 /trace、/diff 及每轮结束的红绿 diff 有数据。
    # trace 仅内存记录、导出时才写 HTML，开销低。
    cfg.trace_enabled = True
    llm = AgentsLLM()
    agent = CodingAgent(
        name="Capybara",
        llm=llm,
        workspace_root=workspace,
        config=cfg,
        verbose=False,
        require_confirm=not auto_approve,
        max_steps=max_steps,
        track_edits=True,
    )
    return agent, llm.model


def _stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """后台线程：阻塞按行读 stdin，经 call_soon_threadsafe 喂进事件循环队列。"""
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if line:
                loop.call_soon_threadsafe(queue.put_nowait, line)
    except Exception:
        pass
    loop.call_soon_threadsafe(queue.put_nowait, None)  # EOF 哨兵


async def _amain(args: argparse.Namespace) -> int:
    # 协议只走真正的 stdout；把 sys.stdout 换成 stderr，隔离 agent 的杂散输出
    protocol_out = sys.stdout
    for stream in (protocol_out, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    sys.stdout = sys.stderr

    def send(obj: dict) -> None:
        protocol_out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        protocol_out.flush()

    try:
        agent, model = build_agent(
            args.workspace, args.config, args.max_steps, args.auto_approve
        )
    except Exception as exc:  # 缺 .env / 凭据等启动失败
        send({"type": "error", "message": f"启动失败：{type(exc).__name__}: {exc}"})
        return 1

    backend = TUIBackend(agent, send)
    backend.send_ready(model, args.workspace)
    backend.send_usage()  # 初始上下文用量（仅 system prompt）

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    threading.Thread(target=_stdin_reader, args=(loop, queue), daemon=True).start()
    try:
        await backend.run(queue)
    finally:
        try:
            agent.close()
        except Exception:
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capybara-backend",
        description="Capybara 交互式 TUI 后端（stdio JSON-lines 协议，由 Ink 前端拉起）",
    )
    p.add_argument("--workspace", "-w", default=".", help="工作区根目录")
    p.add_argument("--config", "-c", default=None, help="项目级配置文件路径")
    p.add_argument("--max-steps", type=int, default=30, help="ReAct 循环最大步数")
    p.add_argument("--auto-approve", action="store_true", help="跳过写文件/命令确认")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    args.workspace = os.path.abspath(args.workspace)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
