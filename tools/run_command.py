"""RunCommandTool — 在工作区内安全执行 shell 命令"""

import subprocess
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult
from ._safety import validate_command
from core.exceptions import ToolException


class RunCommandTool(Tool):
    """在工作区根目录执行 shell 命令，带超时和危险命令黑名单保护"""

    name = "run_command"
    description = (
        "在工作区根目录执行 shell 命令。"
        "返回 stdout + stderr 合并输出及退出码。"
        "危险命令（强制删除、git reset --hard 等）会被拒绝。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认 30",
            },
        },
        "required": ["command"],
    }

    def __init__(
        self,
        workspace_root: str = ".",
        max_output_chars: int = 8192,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._max_output_chars = max_output_chars

    def run(self, **kwargs: Any) -> ToolResult:
        command: str = kwargs.get("command", "")
        timeout: int = int(kwargs.get("timeout", 30))

        if not command:
            return ToolResult.fail("参数 command 不能为空")

        try:
            validate_command(command)
        except ToolException as exc:
            return ToolResult.fail(str(exc))

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self._workspace_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return ToolResult.fail(
                f"命令超时（{timeout}s）: {command!r}",
                exit_code=-1,
            )
        except OSError as exc:
            return ToolResult.fail(f"命令执行失败: {exc}")

        parts: list[str] = []
        if proc.stdout:
            parts.append(proc.stdout)
        if proc.stderr:
            parts.append(f"[stderr]\n{proc.stderr}")
        output = "\n".join(parts) if parts else ""

        # 截断超长输出
        if len(output) > self._max_output_chars:
            output = output[: self._max_output_chars] + f"\n[已截断，超出 {self._max_output_chars} 字符]"

        if proc.returncode == 0:
            return ToolResult.succeed(content=output, exit_code=proc.returncode)
        else:
            return ToolResult.fail(error=output, exit_code=proc.returncode)
