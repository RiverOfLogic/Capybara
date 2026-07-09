"""工具层共享安全校验函数

validate_path  — 文件路径工作区越界检查
validate_command — 危险命令黑名单检查
"""

import re
from pathlib import Path

from core.exceptions import ToolException

# 危险命令黑名单（大小写不敏感匹配）
_COMMAND_BLACKLIST: list[str] = [
    r"del\s+/f",          # Windows 强制删除
    r"rmdir\s+/s",        # Windows 递归删除目录
    r"rd\s+/s",           # rmdir 别名
    r"\bformat\b",        # 磁盘格式化
    r"rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r",  # rm -rf / rm -fr
    r"rm\s+-r\b",         # rm -r
    r"git\s+reset\s+--hard",
    r"git\s+push\s+(-f|--force)",
    r"git\s+checkout\s+--",  # 覆盖工作区文件
    r":\(\)\{:\|:&\};:",  # fork bomb
]

_BLACKLIST_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _COMMAND_BLACKLIST]


def validate_path(path: str, workspace_root: Path) -> Path:
    """校验路径在工作区内，返回解析后的绝对路径。

    越界时抛 ToolException（信息含尝试路径与工作区根）。
    """
    root = workspace_root.resolve()
    resolved = (root / path).resolve()

    if not resolved.is_relative_to(root):
        raise ToolException(
            f"路径越界：{resolved} 不在工作区 {root} 内"
        )
    return resolved


def validate_command(command: str) -> None:
    """检查命令是否命中黑名单，命中时抛 ToolException。"""
    for pattern in _BLACKLIST_PATTERNS:
        if pattern.search(command):
            raise ToolException(
                f"命令包含危险操作，已拒绝执行: {command!r}"
            )
