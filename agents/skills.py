"""Skills 知识外化 — SkillLoader + SkillTool

把可复用的操作指南 / 项目约定写成 `skills_dir/*.md` 文件，Agent 通过 use_skill
工具按需「列出 / 加载」技能内容并遵循。每个技能文件可选带 frontmatter：
    name: 技能名
    description: 一句话说明
缺省则用文件名作技能名。
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.base import Tool, ToolResult

_NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


class SkillLoader:
    """从 skills_dir 读取 *.md 技能文件。"""

    def __init__(self, skills_dir: str = "skills") -> None:
        self._dir = Path(skills_dir)

    def _meta(self, path: Path) -> tuple:
        name = path.stem
        desc = ""
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return name, desc
        m = _NAME_RE.search(text)
        if m:
            name = m.group(1).strip()
        d = _DESC_RE.search(text)
        if d:
            desc = d.group(1).strip()
        return name, desc

    def list_skills(self) -> List[Dict[str, str]]:
        skills: List[Dict[str, str]] = []
        if not self._dir.is_dir():
            return skills
        for p in sorted(self._dir.glob("*.md")):
            name, desc = self._meta(p)
            skills.append({"name": name, "description": desc, "file": p.name})
        return skills

    def get_skill(self, name: str) -> Optional[str]:
        if not self._dir.is_dir():
            return None
        for p in self._dir.glob("*.md"):
            skill_name, _ = self._meta(p)
            if skill_name == name or p.stem == name:
                try:
                    return p.read_text(encoding="utf-8")
                except Exception:
                    return None
        return None


class SkillTool(Tool):
    """列出 / 加载技能库中的可复用指南。"""

    name = "use_skill"
    description = (
        "技能库：存放可复用的操作指南与项目约定。不传 name → 列出全部可用技能；"
        "传 name → 返回该技能的完整内容供你遵循。遇到不确定的项目约定时优先查阅。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名；省略则列出全部"}
        },
    }

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    def run(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name")
        if not name:
            skills = self._loader.list_skills()
            if not skills:
                return ToolResult.succeed("（技能库为空）")
            lines = ["可用技能："] + [
                f"  - {s['name']}: {s['description']}" for s in skills
            ]
            return ToolResult.succeed("\n".join(lines), count=len(skills))
        content = self._loader.get_skill(name)
        if content is None:
            return ToolResult.fail(f"未找到技能：{name}")
        return ToolResult.succeed(content)
