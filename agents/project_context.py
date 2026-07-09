"""项目上下文检测 — 让 CodingAgent 了解它所在的项目

检测主要语言、建议的测试命令、关键配置文件和 README 摘要，
拼成简洁的「项目背景」文本注入 system prompt，使 Agent 无需用户告知即可
知道「这是个什么项目、该怎么跑测试」。
"""

import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

# 与 tools/workspace_info.py 保持一致的检测口径
_EXT_TO_LANG = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".cpp": "C++",
    ".c": "C",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".sh": "Shell",
}

_SCAN_DIRS = [".", "core", "src", "lib", "app", "agents", "tools"]

_KEY_FILES = [
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "Makefile",
    "README.md",
    "README.rst",
]

_README_MAX = 400


def _detect_language(root: Path) -> Optional[str]:
    """扫描若干目录的文件扩展名，返回出现最多的已知语言。"""
    counter: Counter = Counter()
    for scan_dir in _SCAN_DIRS:
        scan_path = root / scan_dir
        if not scan_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(scan_path):
            dirnames[:] = [
                d for d in dirnames
                if d not in {"__pycache__", ".git", "node_modules", ".venv"}
            ]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext in _EXT_TO_LANG:
                    counter[ext] += 1
    if not counter:
        return None
    return _EXT_TO_LANG[counter.most_common(1)[0][0]]


def _has_test_files(tests_dir: Path) -> bool:
    if not tests_dir.is_dir():
        return False
    for fname in os.listdir(tests_dir):
        if fname.startswith("test_") and fname.endswith(".py"):
            return True
        if fname.endswith("_test.py"):
            return True
    return False


def _detect_test_command(root: Path) -> Optional[str]:
    """启发式猜测项目的测试命令。"""
    if (root / "package.json").exists():
        return "npm test"

    tests_dir = root / "tests"
    has_pytest_hint = False
    for cfg in ("pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini"):
        p = root / cfg
        if p.exists():
            try:
                if "pytest" in p.read_text(encoding="utf-8", errors="ignore"):
                    has_pytest_hint = True
            except Exception:
                pass
    if (root / "pytest.ini").exists():
        has_pytest_hint = True

    if has_pytest_hint:
        return "python -m pytest"
    if _has_test_files(tests_dir):
        return "python -m unittest discover -s tests"
    return None


def _read_readme_excerpt(root: Path) -> Optional[str]:
    for name in ("README.md", "README.rst", "README.txt"):
        p = root / name
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                return None
            if len(text) > _README_MAX:
                text = text[:_README_MAX] + "..."
            return text
    return None


def detect_project_context(workspace_root: str) -> Dict[str, Any]:
    """检测项目上下文，返回 language / test_command / key_files / readme_excerpt。"""
    root = Path(workspace_root).resolve()
    key_files = [f for f in _KEY_FILES if (root / f).exists()]
    return {
        "language": _detect_language(root),
        "test_command": _detect_test_command(root),
        "key_files": key_files,
        "readme_excerpt": _read_readme_excerpt(root),
    }


def format_project_context(ctx: Dict[str, Any]) -> str:
    """把检测结果渲染成「项目背景」文本块，供拼进 system prompt（英文，供 LLM 使用）。"""
    lines = ["## Project Context (auto-detected)"]

    language = ctx.get("language")
    lines.append(f"- Primary language: {language or 'not detected'}")

    test_command = ctx.get("test_command")
    if test_command:
        lines.append(f"- Suggested test command: `{test_command}` (prefer this when verifying changes)")
    else:
        lines.append("- Suggested test command: not detected, choose one based on the actual project")

    key_files = ctx.get("key_files") or []
    if key_files:
        lines.append(f"- Key files: {', '.join(key_files)}")

    readme = ctx.get("readme_excerpt")
    if readme:
        lines.append("- README excerpt:")
        for ln in readme.splitlines():
            lines.append(f"    {ln}")

    return "\n".join(lines)
