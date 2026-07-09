"""改动追踪 + diff 总结 — 让 CodingAgent 改完文件后能说清「改了什么」

在写文件类工具（write_file / apply_patch）执行「前」抓取文件原内容快照，
任务结束后对比当前内容，用 difflib 生成统一 diff，供 Agent 总结改动。
"""

import difflib
from pathlib import Path
from typing import Dict, List, Optional

_DIFF_MAX_LINES = 200


class EditTracker:
    """记录一次任务中被修改文件的「改动前」内容，结束后生成 diff 总结。"""

    def __init__(self, workspace_root: str) -> None:
        self._root = Path(workspace_root)
        # abs_path -> {"before": str | None, "rel": str}
        #   before == ""   → 文件原本不存在（新建）
        #   before is None → 快照读取失败，跳过 diff
        self._snapshots: Dict[str, dict] = {}

    def reset(self) -> None:
        self._snapshots = {}

    def snapshot(self, rel_path: str) -> None:
        """在修改「前」抓取文件原内容；同一文件只抓第一次（保留最原始版本）。"""
        try:
            abs_path = (self._root / rel_path).resolve()
        except Exception:
            return
        key = str(abs_path)
        if key in self._snapshots:
            return
        if abs_path.exists():
            try:
                before = abs_path.read_text(encoding="utf-8")
            except Exception:
                before = None
        else:
            before = ""  # 新建文件
        self._snapshots[key] = {"before": before, "rel": rel_path}

    def _read_after(self, abs_path: Path) -> Optional[str]:
        if not abs_path.exists():
            return ""  # 被删除
        try:
            return abs_path.read_text(encoding="utf-8")
        except Exception:
            return None

    def summary(self) -> Dict[str, str]:
        """返回 {rel_path: unified_diff}，仅包含真正发生变化的文件。"""
        changes: Dict[str, str] = {}
        for key, snap in self._snapshots.items():
            before = snap["before"]
            if before is None:
                continue
            after = self._read_after(Path(key))
            if after is None or before == after:
                continue
            rel = snap["rel"]
            diff_lines = list(difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            ))
            if len(diff_lines) > _DIFF_MAX_LINES:
                diff_lines = diff_lines[:_DIFF_MAX_LINES] + ["...(diff 已截断)\n"]
            changes[rel] = "".join(diff_lines)
        return changes

    def changed_files(self) -> List[str]:
        return list(self.summary().keys())

    def format_summary(self) -> str:
        """人读的改动总结：变更文件列表 + 每个文件的 diff。"""
        changes = self.summary()
        if not changes:
            return "本次任务未修改任何文件。"
        parts = [f"本次共修改 {len(changes)} 个文件："]
        for rel in changes:
            parts.append(f"  - {rel}")
        parts.append("")
        for rel, diff in changes.items():
            parts.append(f"=== {rel} ===")
            parts.append(diff.rstrip("\n"))
            parts.append("")
        return "\n".join(parts)
