"""CLI 入口 —— 把一条任务交给 CodingAgent 执行。

两种调用方式：
    python -m agents "任务描述" [选项]
    capybara "任务描述" [选项]           # pip install -e . 后可用（见 pyproject.toml）

项目级配置：默认探测 <workspace>/.capybara.toml（TOML，字段名对应 Config 字段名），
可用 --config 显式指定。未找到配置文件则用 Config() 默认值。需要真实 LLM
凭据（.env 的 LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL）。
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from core.config import Config
from core.llm import AgentsLLM
from .coding_agent import CodingAgent
from tools.code_intel.ctags import resolve_ctags
from tools.code_intel.treesitter import treesitter_available

DEFAULT_CONFIG_FILENAME = ".capybara.toml"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_logo() -> Path:
    """启动横幅用的 Sixel logo：取第一个存在的候选（`logo.six` 或 `logo.sixel`）。

    两个扩展名都是 Sixel 的常见写法，容忍任一命名，换 logo 时不必纠结后缀。都不存在
    时回退到 `logo.six`（`_print_banner` / 前端遇不存在会静默跳过）。
    """
    for name in ("logo.six", "logo.sixel"):
        candidate = _REPO_ROOT / name
        if candidate.is_file():
            return candidate
    return _REPO_ROOT / "logo.six"


_LOGO_PATH = _resolve_logo()
_UI_INDEX = _REPO_ROOT / "ui" / "index.js"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capybara",
        description="CodingAgent 命令行入口：把一条任务描述交给 Agent 执行。",
    )
    parser.add_argument(
        "task", nargs="?", default=None,
        help="要执行的任务描述；省略则进入交互式终端 UI（Ink 前端）",
    )
    parser.add_argument(
        "--workspace", "-w", default=".",
        help="工作区根目录（默认当前目录）",
    )
    parser.add_argument(
        "--config", "-c", default=None,
        help=f"项目级配置文件路径（默认探测工作区下的 {DEFAULT_CONFIG_FILENAME}，不存在则用默认配置）",
    )
    parser.add_argument(
        "--production", action="store_true",
        help="使用生产预设 CodingAgent.production()（trace/会话自动保存/项目自举/改动追踪/子 Agent 全开）",
    )
    parser.add_argument(
        "--auto-approve", action="store_true",
        help="跳过写文件/跑命令前的确认，直接执行",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="打印执行过程（--production 模式下默认已开启）",
    )
    parser.add_argument(
        "--verify", default=None, metavar="COMMAND",
        help="验证命令（如 'python -m unittest'），最终答案前自动跑验证，失败则回灌修复",
    )
    parser.add_argument(
        "--max-steps", type=int, default=20,
        help="ReAct 循环最大步数（默认 20）",
    )
    return parser


def _print_banner(model: str, logo_path: Path = _LOGO_PATH, stream=None) -> None:
    """终端启动时打印 Sixel logo 并显示当前模型。

    logo 是原始 Sixel 转义序列，直接写字节到终端即可渲染，不支持 Sixel 的终端会
    显示为一段乱码——不影响后续文字输出，因此 logo 文件缺失/写入失败都静默跳过。
    """
    stream = stream if stream is not None else sys.stdout.buffer
    try:
        if logo_path.is_file():
            stream.write(logo_path.read_bytes())
            stream.flush()
    except Exception:
        pass
    print(f"模型：{model}\n")


def _resolve_config(workspace: str, config_path: Optional[str]) -> Config:
    """显式 --config 优先；否则探测 <workspace>/.capybara.toml；都没有则默认 Config。"""
    path = config_path or os.path.join(workspace, DEFAULT_CONFIG_FILENAME)
    if os.path.isfile(path):
        return Config.from_file(path)
    return Config()


def _reconfigure_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 旧 cmd（GBK/cp936）下中文乱码。

    交互式后端 tui_backend 已各自 reconfigure；这里补上一次性 CLI 路径。老 Python /
    已被重定向为不可重配的流一律静默跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _probe_code_intel(cfg: Config, stream=None) -> None:
    """启动期探测代码智能可用度：ctags / tree-sitter 任一缺失时向 stderr 打中文横幅。

    横幅逐条列出缺什么、影响哪些工具、修复命令；全可用则完全静默。走 stderr，不污染
    stdout / TUI 协议。基础读写/搜索/运行不受影响，仅高级检索降级。
    """
    stream = stream if stream is not None else sys.stderr
    problems: List[str] = []
    if not resolve_ctags(cfg.ctags_path):
        problems.append(
            "  · ctags 未找到 → find_definition 不可用。\n"
            "      装 Universal Ctags：Windows `choco install universal-ctags` /"
            " `scoop install universal-ctags`；\n"
            "      或在 .capybara.toml 设 ctags_path 指向 ctags 可执行文件。"
        )
    if not treesitter_available():
        problems.append(
            "  · tree-sitter 未安装 → find_references / repo_map / get_file_relations 不可用。\n"
            "      修复：`pipx runpip capybara install tree-sitter tree-sitter-language-pack`\n"
            "      （或在当前环境 `pip install tree-sitter tree-sitter-language-pack`）。"
        )
    if not problems:
        return
    banner = (
        "⚠ 代码智能可用度不足（不影响基础读写/搜索/运行，仅以下高级检索降级）：\n"
        + "\n".join(problems)
    )
    print(banner, file=stream)


def _launch_tui(workspace: str, config_path: Optional[str]) -> int:
    """无 task 时进入交互式 UI：定位 node，把 Ink 前端作为子进程接管终端。

    把自己的 `sys.executable` 传给前端，让它用**同一个解释器**拉起
    `agents.tui_backend`——保证后端跑在装了依赖的当前环境，而不是 PATH 上随便一个
    python。缺 node 或缺前端入口时给出清晰指引而非崩溃。返回前端进程的退出码。
    """
    node = shutil.which("node")
    if node is None:
        print(
            "交互式 UI 需要 Node.js（未在 PATH 找到 node）。\n"
            "  · 装 Node ≥18 后先 `cd ui && npm install`，再运行 `capybara`；\n"
            '  · 或用一次性模式：`capybara "你的任务" --workspace ./proj`。',
            file=sys.stderr,
        )
        return 1
    if not _UI_INDEX.is_file():
        print(
            f"未找到前端入口 {_UI_INDEX}。\n"
            "  首次使用请先 `cd ui && npm install`。",
            file=sys.stderr,
        )
        return 1
    cmd = [
        node, str(_UI_INDEX),
        "--python", sys.executable,
        "--workspace", workspace,
        "--config", config_path or "",
        "--logo", str(_LOGO_PATH),
    ]
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    _reconfigure_stdio()
    args = build_parser().parse_args(argv)
    workspace = os.path.abspath(args.workspace)

    # 两条路径分叉前统一解析配置 + 探测代码智能（跑在启动器进程、真终端，两种模式都可见）
    cfg = _resolve_config(workspace, args.config)
    _probe_code_intel(cfg)

    # 无 task → 交互式终端 UI（Ink 前端 + agents.tui_backend 后端）
    if args.task is None:
        return _launch_tui(workspace, args.config)

    # 有 task → 一次性模式（纯 Python，不需要 Node）
    llm = AgentsLLM()
    _print_banner(llm.model)

    if args.production:
        agent = CodingAgent.production(
            name="Capybara", llm=llm, workspace_root=workspace, config=cfg,
            require_confirm=not args.auto_approve,
            max_steps=args.max_steps,
            verify_command=args.verify,
        )
    else:
        agent = CodingAgent(
            name="Capybara", llm=llm, workspace_root=workspace, config=cfg,
            require_confirm=not args.auto_approve,
            verbose=args.verbose,
            max_steps=args.max_steps,
            verify_command=args.verify,
        )

    try:
        result = agent.run(args.task)
    except Exception as exc:
        print(f"[出错] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
