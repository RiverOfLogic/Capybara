"""生产力工具 — TodoWrite（待办清单）与 DevLog（开发日志）

两者都是「Agent 主动维护的外部持久化记忆」：
- TodoWriteTool：把复杂任务拆成可跟踪的小步骤，覆盖式维护一张待办清单。
- DevLogTool：追加带时间戳的叙述性开发日志（区别于自动 trace 的结构化事件）。
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.base import Tool, ToolResult

_STATUS_ICON = {"pending": "☐", "in_progress": "◐", "completed": "☑"}
_VALID_STATUS = set(_STATUS_ICON)


class TodoWriteTool(Tool):
    """维护任务待办清单（覆盖式）。"""

    name = "todo_write"
    description = (
        "维护任务待办清单，把复杂任务拆成可跟踪的小步骤。传 items 覆盖整张清单"
        "（每项含 content 与 status）；不传 items 则返回当前清单。"
        "status 取 pending / in_progress / completed。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
    }

    def __init__(self, persistence_dir: str = "memory/todos", name: str = "default") -> None:
        self._dir = Path(persistence_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"todo-{name}.json"

    def _load(self) -> list:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save(self, items: list) -> None:
        tmp = str(self._path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def _format(self, items: list) -> str:
        if not items:
            return "（待办清单为空）"
        lines = []
        for i, it in enumerate(items, 1):
            icon = _STATUS_ICON.get(it.get("status"), "☐")
            lines.append(f"{i}. {icon} {it.get('content', '')}")
        done = sum(1 for it in items if it.get("status") == "completed")
        lines.append(f"（{done}/{len(items)} 已完成）")
        return "\n".join(lines)

    def run(self, **kwargs: Any) -> ToolResult:
        items = kwargs.get("items")
        if items is None:
            return ToolResult.succeed(self._format(self._load()))

        normalized = []
        for it in items:
            if not isinstance(it, dict) or "content" not in it:
                return ToolResult.fail("每个待办项需包含 content 与 status")
            status = it.get("status", "pending")
            if status not in _VALID_STATUS:
                return ToolResult.fail(
                    f"非法 status：{status}（应为 pending/in_progress/completed）"
                )
            normalized.append({"content": str(it["content"]), "status": status})
        self._save(normalized)
        return ToolResult.succeed(self._format(normalized), count=len(normalized))


class DevLogTool(Tool):
    """追加带时间戳的开发日志。"""

    name = "devlog"
    description = (
        "追加一条带时间戳的开发日志，记录关键决策、进展或遗留问题，便于回顾。"
        "与自动 trace 不同，这是你主动写下的叙述性记录。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "entry": {"type": "string", "description": "日志内容"},
            "category": {
                "type": "string",
                "description": "可选分类，如 decision / progress / risk",
            },
        },
        "required": ["entry"],
    }

    def __init__(self, persistence_dir: str = "memory/devlogs", name: str = "default") -> None:
        self._dir = Path(persistence_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"devlog-{name}.md"

    def run(self, **kwargs: Any) -> ToolResult:
        entry = kwargs.get("entry")
        if not entry:
            return ToolResult.fail("缺少 entry：请提供日志内容")
        category = kwargs.get("category")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = f"[{category}] " if category else ""
        line = f"- {timestamp} {prefix}{entry}\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
        return ToolResult.succeed(f"已记录开发日志：{prefix}{entry}")
