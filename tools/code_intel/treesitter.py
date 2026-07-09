"""Tree-sitter 后端 —— find_references / repo_map / get_file_relations。

设计见 docs/code-intelligence.md §3.3 / §3.4。用真 AST 做：
- find_references：精确标识符引用（排除注释/字符串里的同名词），带角色（def/call/import）。
- repo_map：抽取各文件 top 符号，压成全局导航文本喂 LLM。
- get_file_relations：抽取 import 关系，构文件级依赖图（imports / imported_by）。

Tree-sitter 为硬依赖：未安装时工具返回明确报错 + `pip install` 指引，不回退。
解析采用标准 py-tree-sitter API + tree_sitter_language_pack 提供的 grammar。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..base import Tool, ToolResult
from .._safety import validate_path
from core.exceptions import ToolException
from ._common import (
    iter_code_files, language_of, read_text, rel_to,
)

_INSTALL_HINT = (
    "未安装 Tree-sitter。请 `pip install tree-sitter tree-sitter-language-pack` "
    "后重试（本功能需精确 AST 解析）。"
)

# 每语言：定义节点类型 / import 节点类型。name 默认取 child_by_field_name("name")，
# 个别语言（C 函数、Go 类型）走 _node_name 的兜底descent。
LANG_SPEC: Dict[str, Dict[str, set]] = {
    "python": {
        "defs": {"function_definition", "class_definition"},
        "imports": {"import_statement", "import_from_statement"},
    },
    "c": {
        "defs": {"function_definition", "struct_specifier", "enum_specifier",
                 "union_specifier", "type_definition"},
        "imports": {"preproc_include"},
    },
    "cpp": {
        "defs": {"function_definition", "struct_specifier", "enum_specifier",
                 "union_specifier", "class_specifier", "namespace_definition"},
        "imports": {"preproc_include"},
    },
    "java": {
        "defs": {"class_declaration", "interface_declaration", "enum_declaration",
                 "method_declaration", "record_declaration", "constructor_declaration"},
        "imports": {"import_declaration"},
    },
    "rust": {
        "defs": {"function_item", "struct_item", "enum_item", "trait_item",
                 "mod_item", "type_item", "const_item", "macro_definition"},
        "imports": {"use_declaration"},
    },
    "go": {
        "defs": {"function_declaration", "method_declaration", "type_declaration"},
        "imports": {"import_declaration"},
    },
    "javascript": {
        "defs": {"function_declaration", "class_declaration", "method_definition"},
        "imports": {"import_statement"},
    },
    "typescript": {
        "defs": {"function_declaration", "class_declaration", "method_definition",
                 "interface_declaration", "type_alias_declaration", "enum_declaration"},
        "imports": {"import_statement"},
    },
    "tsx": {
        "defs": {"function_declaration", "class_declaration", "method_definition",
                 "interface_declaration", "type_alias_declaration", "enum_declaration"},
        "imports": {"import_statement"},
    },
}

_IDENT_TYPES = {"identifier", "type_identifier", "field_identifier",
                "property_identifier", "namespace_identifier"}

_parser_cache: Dict[str, Any] = {}


def treesitter_available() -> bool:
    try:
        import tree_sitter  # noqa
        import tree_sitter_language_pack  # noqa
        return True
    except Exception:
        return False


def _get_parser(lang: str):
    if lang not in _parser_cache:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language
        _parser_cache[lang] = Parser(get_language(lang))
    return _parser_cache[lang]


def _parse(text: str, lang: str):
    parser = _get_parser(lang)
    return parser.parse(text.encode("utf-8"))


def _node_text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _node_name(node) -> Optional[str]:
    """提取定义节点的名字。优先 name 字段，否则按类型 descent 兜底。"""
    nm = node.child_by_field_name("name")
    if nm is not None:
        return _node_text(nm)
    # C/C++ 函数：function_definition → declarator → function_declarator → identifier
    if node.type == "function_definition":
        decl = node.child_by_field_name("declarator")
        seen = 0
        stack = [decl] if decl is not None else list(node.children)
        while stack and seen < 200:
            seen += 1
            n = stack.pop()
            if n is None:
                continue
            if n.type in ("identifier", "field_identifier", "qualified_identifier"):
                return _node_text(n)
            stack.extend(n.children)
    # Go type_declaration → type_spec → name
    if node.type == "type_declaration":
        for c in node.children:
            if c.type == "type_spec":
                nm2 = c.child_by_field_name("name")
                if nm2 is not None:
                    return _node_text(nm2)
    # 兜底：第一个标识符子节点
    for c in node.children:
        if c.type in _IDENT_TYPES:
            return _node_text(c)
    return None


def _iter_definitions(root, lang: str):
    """产出 (name, kind, line) —— 该文件中的所有定义。"""
    spec = LANG_SPEC.get(lang)
    if not spec:
        return
    def_types = spec["defs"]
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in def_types:
            name = _node_name(node)
            if name:
                yield name, node.type, node.start_point[0] + 1
        stack.extend(node.children)


def _iter_imports(root, lang: str):
    """产出 import 语句节点。"""
    spec = LANG_SPEC.get(lang)
    if not spec:
        return
    imp_types = spec["imports"]
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in imp_types:
            yield node
        else:
            stack.extend(node.children)


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------


def _classify_role(node) -> str:
    """根据父节点上下文猜引用角色。"""
    parent = node.parent
    if parent is None:
        return "ref"
    pt = parent.type
    if pt in ("call", "call_expression", "function_call", "method_invocation"):
        return "call"
    if pt in LANG_SPEC.get("python", {}).get("defs", set()) or pt.endswith(
        ("_definition", "_declaration", "_item", "_specifier")
    ):
        # 名字字段位于定义节点下
        nm = parent.child_by_field_name("name")
        if nm is not None and nm.start_byte == node.start_byte:
            return "def"
    if "import" in pt or "include" in pt or pt == "use_declaration":
        return "import"
    if pt in ("attribute", "field_expression", "selector_expression",
              "member_expression"):
        return "attr"
    return "ref"


class FindReferencesTool(Tool):
    """按符号名查找引用（Tree-sitter 精确 AST，区分 def/call/import/attr）。"""

    name = "find_references"
    description = (
        "按符号名查找所有引用/使用点，基于 Tree-sitter 精确 AST（排除注释/字符串中的"
        "同名词，并标注角色 def/call/import/attr）。了解某函数/类被哪里用时使用。需安装 tree-sitter。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "要查找引用的符号名"},
            "path": {
                "type": "string",
                "description": "搜索起点目录或文件（相对工作区根），默认工作区根目录",
            },
        },
        "required": ["name"],
    }

    def __init__(
        self, workspace_root: str = ".", max_results: int = 200,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._max_results = max_results

    def run(self, **kwargs: Any) -> ToolResult:
        name: str = (kwargs.get("name") or "").strip()
        path: str = kwargs.get("path", ".")
        if not name:
            return ToolResult.fail("参数 name 不能为空")
        if not treesitter_available():
            return ToolResult.fail(_INSTALL_HINT)
        try:
            target = validate_path(path, self._workspace_root)
        except ToolException as exc:
            return ToolResult.fail(str(exc))
        if not target.exists():
            return ToolResult.fail(f"路径不存在: {path}")

        name_bytes = name.encode("utf-8")
        results: List[str] = []
        truncated = False
        for fp, lang in iter_code_files(target):
            if truncated:
                break
            if lang not in LANG_SPEC:
                continue
            text = read_text(fp)
            if text is None:
                continue
            try:
                tree = _parse(text, lang)
            except Exception:
                continue
            rel = rel_to(fp, self._workspace_root)
            stack = [tree.root_node]
            while stack:
                node = stack.pop()
                if node.type in _IDENT_TYPES and node.text == name_bytes:
                    role = _classify_role(node)
                    line = node.start_point[0] + 1
                    snippet = text.splitlines()[line - 1].strip() if line <= len(
                        text.splitlines()) else ""
                    results.append(f"{rel}:{line}: [{role}] {snippet}")
                    if len(results) >= self._max_results:
                        truncated = True
                        break
                stack.extend(node.children)

        if not results:
            return ToolResult.succeed(
                content=f"未找到符号引用: {name!r}", match_count=0,
            )
        content = "\n".join(results)
        if truncated:
            content += f"\n[已截断，超出最大结果数 {self._max_results}]"
        return ToolResult.succeed(content=content, match_count=len(results))


# ---------------------------------------------------------------------------
# repo_map
# ---------------------------------------------------------------------------


class RepoMapTool(Tool):
    """生成仓库地图：各文件 top 符号的压缩清单，供 LLM 建立全局结构认知。"""

    name = "repo_map"
    description = (
        "生成「仓库地图」——按文件列出其顶层符号（函数/类/类型），压缩成全局导航视图，"
        "供改动前建立整体认知。可选 path 聚焦子目录。需安装 tree-sitter。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "聚焦的目录或文件（相对工作区根），默认工作区根目录",
            },
            "max_chars": {
                "type": "integer",
                "description": "输出字符上限（预算），默认 6000",
            },
        },
    }

    def __init__(
        self, workspace_root: str = ".", default_budget: int = 6000,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._default_budget = default_budget

    def run(self, **kwargs: Any) -> ToolResult:
        path: str = kwargs.get("path", ".")
        budget: int = int(kwargs.get("max_chars", self._default_budget) or self._default_budget)
        if not treesitter_available():
            return ToolResult.fail(_INSTALL_HINT)
        try:
            target = validate_path(path, self._workspace_root)
        except ToolException as exc:
            return ToolResult.fail(str(exc))
        if not target.exists():
            return ToolResult.fail(f"路径不存在: {path}")

        per_file: List[Tuple[str, List[str]]] = []
        for fp, lang in iter_code_files(target):
            if lang not in LANG_SPEC:
                continue
            text = read_text(fp)
            if text is None:
                continue
            try:
                tree = _parse(text, lang)
            except Exception:
                continue
            defs = list(_iter_definitions(tree.root_node, lang))
            if not defs:
                continue
            rel = rel_to(fp, self._workspace_root)
            per_file.append((rel, [f"{k.split('_')[0]} {n} (L{ln})"
                                   for n, k, ln in defs]))

        # 按定义数量降序，符号多的文件更可能是核心
        per_file.sort(key=lambda x: len(x[1]), reverse=True)
        lines: List[str] = []
        used = 0
        total_files = len(per_file)
        shown = 0
        for rel, syms in per_file:
            block = rel + "\n" + "\n".join(f"  - {s}" for s in syms) + "\n"
            if used + len(block) > budget:
                break
            lines.append(block)
            used += len(block)
            shown += 1
        if not lines:
            return ToolResult.succeed(content="未发现可索引的代码符号", file_count=0)
        content = "".join(lines).rstrip()
        if shown < total_files:
            content += f"\n[预算受限，已显示 {shown}/{total_files} 个文件]"
        return ToolResult.succeed(content=content, file_count=shown)


# ---------------------------------------------------------------------------
# get_file_relations
# ---------------------------------------------------------------------------


def _module_key(rel_path: str) -> str:
    """文件路径 → 模块名 key（用于反向匹配 import 文本）。"""
    p = Path(rel_path)
    stem = p.stem
    return "__init__" if stem == "__init__" else stem


class GetFileRelationsTool(Tool):
    """查文件依赖关系：本文件 import 了谁、被谁 import（Tree-sitter 抽取 import）。"""

    name = "get_file_relations"
    description = (
        "查某文件的依赖关系：它 import 了哪些（imports）、被哪些文件 import（imported_by），"
        "基于 Tree-sitter 抽取 import 语句。改文件前评估波及面时用。需安装 tree-sitter。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "目标文件路径（相对工作区根）",
            },
        },
        "required": ["file"],
    }

    def __init__(self, workspace_root: str = ".") -> None:
        self._workspace_root = Path(workspace_root)

    def run(self, **kwargs: Any) -> ToolResult:
        file: str = (kwargs.get("file") or "").strip()
        if not file:
            return ToolResult.fail("参数 file 不能为空")
        if not treesitter_available():
            return ToolResult.fail(_INSTALL_HINT)
        try:
            target = validate_path(file, self._workspace_root)
        except ToolException as exc:
            return ToolResult.fail(str(exc))
        if not target.is_file():
            return ToolResult.fail(f"文件不存在: {file}")
        lang = language_of(target)
        if lang not in LANG_SPEC:
            return ToolResult.fail(f"不支持的语言/文件类型: {file}")

        rel_self = rel_to(target, self._workspace_root)
        self_key = _module_key(rel_self)

        # imports：解析目标文件
        text = read_text(target)
        imports: List[str] = []
        if text is not None:
            try:
                tree = _parse(text, lang)
                for node in _iter_imports(tree.root_node, lang):
                    line = node.start_point[0] + 1
                    raw = " ".join(_node_text(node).split())
                    imports.append(f"L{line}: {raw}")
            except Exception:
                pass

        # imported_by：扫描全库其它文件的 import 文本是否提及本文件模块名
        imported_by: List[str] = []
        for fp, lang2 in iter_code_files(self._workspace_root):
            rel = rel_to(fp, self._workspace_root)
            if rel == rel_self or lang2 not in LANG_SPEC:
                continue
            t2 = read_text(fp)
            if t2 is None:
                continue
            try:
                tree2 = _parse(t2, lang2)
            except Exception:
                continue
            for node in _iter_imports(tree2.root_node, lang2):
                txt = _node_text(node)
                # 词级匹配模块名，避免子串误命中
                if self_key in txt.replace("/", " ").replace(".", " ").replace(
                        "\\", " ").split():
                    imported_by.append(f"{rel}:{node.start_point[0] + 1}")
                    break

        lines = [f"文件: {rel_self}", "", "imports（本文件依赖）:"]
        lines += [f"  {x}" for x in imports] or ["  （无）"]
        lines += ["", "imported_by（依赖本文件）:"]
        lines += [f"  {x}" for x in imported_by] or ["  （无）"]
        return ToolResult.succeed(
            content="\n".join(lines),
            imports_count=len(imports), imported_by_count=len(imported_by),
        )
