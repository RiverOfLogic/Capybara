"""search_codebase —— 基于 SQLite FTS5 的全文模糊搜索。

设计见 docs/code-intelligence.md §3.2。把代码按行窗口分块写入 FTS5 索引，
LLM 用自然语言 / 关键词 / 报错片段去 MATCH，bm25 排名返回 file:line 片段。

零依赖：仅用 stdlib sqlite3（FTS5 已编译在内）。
"""

import re
import sqlite3
from pathlib import Path
from typing import Any, List

from ..base import Tool, ToolResult
from ._common import (
    DEFAULT_INDEX_DIR, index_dir_path, iter_code_files, read_text, rel_to,
)

_CHUNK_LINES = 40           # 每个索引块的行数
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def fts5_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE _t USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


class FtsIndex:
    """管理 FTS5 索引库：建表、增量更新、查询。"""

    def __init__(
        self, workspace_root: str = ".", index_dir: str = DEFAULT_INDEX_DIR,
        chunk_lines: int = _CHUNK_LINES,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.index_dir = index_dir
        self.chunk_lines = chunk_lines

    @property
    def db_file(self) -> Path:
        return index_dir_path(self.workspace_root, self.index_dir) / "index.db"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_file))
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS code "
            "USING fts5(path UNINDEXED, start_line UNINDEXED, body, "
            "tokenize='unicode61')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL)"
        )
        return conn

    def _chunks(self, text: str):
        lines = text.splitlines()
        for i in range(0, len(lines), self.chunk_lines):
            yield i + 1, "\n".join(lines[i:i + self.chunk_lines])

    def ensure(self) -> None:
        """增量同步索引：新增/改动文件重建其块，删除文件清其块。"""
        conn = self._connect()
        try:
            seen = set()
            known = dict(conn.execute("SELECT path, mtime FROM files").fetchall())
            for fp, _lang in iter_code_files(self.workspace_root):
                rel = rel_to(fp, self.workspace_root)
                seen.add(rel)
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                if known.get(rel) == mtime:
                    continue  # 未变
                text = read_text(fp)
                if text is None:
                    continue
                conn.execute("DELETE FROM code WHERE path = ?", (rel,))
                conn.executemany(
                    "INSERT INTO code(path, start_line, body) VALUES (?, ?, ?)",
                    [(rel, sl, body) for sl, body in self._chunks(text)],
                )
                conn.execute(
                    "INSERT OR REPLACE INTO files(path, mtime) VALUES (?, ?)",
                    (rel, mtime),
                )
            # 清理已删除文件
            for rel in set(known) - seen:
                conn.execute("DELETE FROM code WHERE path = ?", (rel,))
                conn.execute("DELETE FROM files WHERE path = ?", (rel,))
            conn.commit()
        finally:
            conn.close()

    def search(self, query_text: str, limit: int = 20) -> List[str]:
        tokens = _TOKEN_RE.findall(query_text or "")
        if not tokens:
            return []
        # 引号包裹每个 token，OR 连接以提召回，bm25 排名surface最相关
        match = " OR ".join(f'"{t}"' for t in dict.fromkeys(tokens))
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT path, start_line, "
                "snippet(code, 2, '«', '»', ' … ', 12) "
                "FROM code WHERE code MATCH ? ORDER BY bm25(code) LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()
        out = []
        for path, start_line, snip in rows:
            snip = " ".join(snip.split())  # 折叠多行/空白
            out.append(f"{path}:{start_line}: {snip}")
        return out


class SearchCodebaseTool(Tool):
    """用自然语言 / 关键词 / 报错片段模糊搜索代码（SQLite FTS5，零依赖）。"""

    name = "search_codebase"
    description = (
        "用自然语言、关键词或报错信息模糊搜索代码库（FTS5 全文检索 + bm25 排名）。"
        "不知道符号确切名字、或想按语义/报错定位代码时用它。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query_text": {
                "type": "string",
                "description": "自然语言描述、关键词或报错片段",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 20",
            },
        },
        "required": ["query_text"],
    }

    def __init__(
        self, workspace_root: str = ".", index_dir: str = DEFAULT_INDEX_DIR,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._index = FtsIndex(workspace_root, index_dir)

    def run(self, **kwargs: Any) -> ToolResult:
        query: str = (kwargs.get("query_text") or "").strip()
        limit: int = int(kwargs.get("limit", 20) or 20)
        if not query:
            return ToolResult.fail("参数 query_text 不能为空")
        if not fts5_available():
            return ToolResult.fail(
                "当前 Python 的 sqlite3 未编译 FTS5，无法使用 search_codebase。"
            )
        try:
            self._index.ensure()
        except sqlite3.Error as exc:
            return ToolResult.fail(f"索引构建失败: {exc}")
        results = self._index.search(query, limit)
        if not results:
            return ToolResult.succeed(
                content=f"未搜到相关代码: {query!r}", match_count=0,
            )
        return ToolResult.succeed(
            content="\n".join(results), match_count=len(results),
        )
