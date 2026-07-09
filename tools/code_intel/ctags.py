"""find_definition —— 基于 Universal Ctags 的定义检索。

设计见 docs/code-intelligence.md §3.1。启动时 `ctags -R` 生成 tags 文件，
查询时只读文本 tags（解析进内存 dict），不加载任何 parser，语言覆盖最广。

ctags 为硬依赖：未检测到时工具返回明确报错 + 安装指引，不回退。
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..base import Tool, ToolResult
from ._common import (
    SKIP_DIRS, DEFAULT_INDEX_DIR, index_dir_path, iter_code_files, language_of,
)

_INSTALL_HINT = (
    "未检测到 Universal Ctags。请安装后重试："
    "Windows `choco install universal-ctags` / `scoop install universal-ctags`；"
    "Linux `apt install universal-ctags`；macOS `brew install universal-ctags`。"
    "或在 .capybara.toml 的 ctags_path 指定 ctags 可执行文件路径。"
)


class _TagEntry:
    __slots__ = ("name", "path", "line", "kind", "scope")

    def __init__(self, name, path, line, kind, scope):
        self.name = name
        self.path = path
        self.line = line
        self.kind = kind
        self.scope = scope


def resolve_ctags(ctags_path: str = "") -> Optional[str]:
    """定位 ctags 可执行文件。

    优先级：显式 ctags_path（.capybara.toml 的单一配置来源）> 系统 PATH 上的 ctags。
    找不到返回 None。
    """
    if ctags_path:
        p = Path(ctags_path)
        if p.is_file():
            return str(p)
        found = shutil.which(ctags_path)
        if found:
            return found
        # 候选无效则继续退到 PATH 查找
    return shutil.which("ctags")


def _parse_tags_file(tags_path: Path) -> Dict[str, List[_TagEntry]]:
    """解析 u-ctags 格式 tags 文件 → {name: [entry, ...]}。"""
    index: Dict[str, List[_TagEntry]] = {}
    try:
        text = tags_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return index
    for raw in text.splitlines():
        if not raw or raw.startswith("!_TAG_"):
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        name, path = parts[0], parts[1]
        kind = ""
        line = 0
        scope = ""
        # parts[2] 是地址模式（以 ;" 结尾）；其后是 kind / line:/ scope 等字段
        for field in parts[3:]:
            if field.startswith("line:"):
                try:
                    line = int(field[5:])
                except ValueError:
                    pass
            elif ":" in field:
                # scope 字段，如 class:Foo / struct:Bar / namespace:N
                scope = field
            elif field:
                kind = field
        index.setdefault(name, []).append(
            _TagEntry(name, path, line, kind, scope)
        )
    return index


class CtagsIndex:
    """管理 tags 文件的生成、过期判定与查询缓存。"""

    def __init__(
        self, workspace_root: str = ".", ctags_path: str = "",
        index_dir: str = DEFAULT_INDEX_DIR,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.ctags_path = ctags_path
        self.index_dir = index_dir
        self._cache: Optional[Dict[str, List[_TagEntry]]] = None
        self._cache_mtime: float = -1.0

    @property
    def tags_file(self) -> Path:
        return index_dir_path(self.workspace_root, self.index_dir) / "tags"

    def _newest_source_mtime(self) -> float:
        newest = 0.0
        for fp, _lang in iter_code_files(self.workspace_root):
            try:
                m = fp.stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest = m
        return newest

    def is_stale(self) -> bool:
        tf = self.tags_file
        if not tf.exists():
            return True
        try:
            tags_mtime = tf.stat().st_mtime
        except OSError:
            return True
        return tags_mtime < self._newest_source_mtime()

    def build(self) -> ToolResult:
        """运行 ctags 生成 tags 文件。"""
        exe = resolve_ctags(self.ctags_path)
        if not exe:
            return ToolResult.fail(_INSTALL_HINT)
        tags = self.tags_file
        cmd = [
            exe, "-R", "--fields=+nKsS", "--extras=+q",
            "-f", str(tags),
        ]
        for d in SKIP_DIRS:
            cmd.append(f"--exclude={d}")
        cmd.append(".")
        try:
            proc = subprocess.run(
                cmd, cwd=str(self.workspace_root),
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ToolResult.fail(f"ctags 执行失败: {exc}")
        if proc.returncode != 0:
            return ToolResult.fail(
                f"ctags 退出码 {proc.returncode}: {(proc.stderr or '').strip()[:500]}"
            )
        # 失效缓存
        self._cache = None
        self._cache_mtime = -1.0
        return ToolResult.succeed(content="ok")

    def ensure(self) -> Optional[ToolResult]:
        """确保 tags 最新；失败返回 ToolResult，成功返回 None。"""
        if self.is_stale():
            res = self.build()
            if not res.ok:
                return res
        return None

    def load(self) -> Dict[str, List[_TagEntry]]:
        tf = self.tags_file
        try:
            mtime = tf.stat().st_mtime
        except OSError:
            return {}
        if self._cache is None or mtime != self._cache_mtime:
            self._cache = _parse_tags_file(tf)
            self._cache_mtime = mtime
        return self._cache

    def find(self, name: str, lang: Optional[str] = None) -> List[_TagEntry]:
        entries = self.load().get(name, [])
        if lang:
            entries = [e for e in entries if language_of(e.path) == lang]
        return entries


class FindDefinitionTool(Tool):
    """按符号名查找定义（Universal Ctags 后端，覆盖 150+ 语言）。"""

    name = "find_definition"
    description = (
        "按符号名查找定义位置（函数/类/结构体/类型/方法等），基于 Universal Ctags，"
        "语言覆盖最广。大仓库里优先用它定位代码。需安装 ctags。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "要查找定义的符号名"},
            "lang": {
                "type": "string",
                "description": "可选，按语言过滤（python/c/cpp/java/rust/go/javascript/typescript）",
            },
        },
        "required": ["name"],
    }

    def __init__(
        self, workspace_root: str = ".", ctags_path: str = "",
        index_dir: str = DEFAULT_INDEX_DIR, max_results: int = 100,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._index = CtagsIndex(workspace_root, ctags_path, index_dir)
        self._max_results = max_results

    def run(self, **kwargs: Any) -> ToolResult:
        name: str = (kwargs.get("name") or "").strip()
        lang: Optional[str] = kwargs.get("lang")
        if not name:
            return ToolResult.fail("参数 name 不能为空")

        err = self._index.ensure()
        if err is not None:
            return err

        entries = self._index.find(name, lang)
        if not entries:
            return ToolResult.succeed(
                content=f"未找到符号定义: {name!r}", match_count=0,
            )
        lines: List[str] = []
        for e in entries[: self._max_results]:
            scope = f" ({e.scope})" if e.scope else ""
            lines.append(f"{e.path}:{e.line} [{e.kind}]{scope} {e.name}")
        content = "\n".join(lines)
        if len(entries) > self._max_results:
            content += f"\n[已截断，共 {len(entries)} 处，仅显示前 {self._max_results}]"
        return ToolResult.succeed(content=content, match_count=len(entries))
