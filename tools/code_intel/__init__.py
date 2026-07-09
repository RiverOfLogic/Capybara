"""代码智能子系统：find_definition / search_codebase / find_references /
repo_map / get_file_relations。

后端分工（详见 docs/code-intelligence.md）：
- ctags    → find_definition（定义检索，覆盖最广语言）
- FTS5     → search_codebase（自然语言/报错模糊搜索，零依赖）
- tree-sitter → find_references / repo_map / get_file_relations（AST 精确）
"""

from .ctags import FindDefinitionTool, CtagsIndex, resolve_ctags
from .fts import SearchCodebaseTool, FtsIndex, fts5_available
from .treesitter import (
    FindReferencesTool, RepoMapTool, GetFileRelationsTool, treesitter_available,
)

__all__ = [
    "FindDefinitionTool",
    "SearchCodebaseTool",
    "FindReferencesTool",
    "RepoMapTool",
    "GetFileRelationsTool",
    "CtagsIndex",
    "FtsIndex",
    "resolve_ctags",
    "fts5_available",
    "treesitter_available",
]
