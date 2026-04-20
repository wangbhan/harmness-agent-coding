"""
Skill 加载器 + SkillTool

1. 扫描 skills 目录下的 SKILL.md 文件
2. 解析 YAML frontmatter 和正文
3. 提供描述列表（注入 system prompt）和内容获取（工具调用）
"""
import re
from pathlib import Path

import yaml

from internal.Agent.tools.base import BaseTool, _get_file_encoding

SKILL_DIR = Path(__file__).parent / "skills"


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()

    def _load_all(self):
        if not self.skills_dir.exists():
            return
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text(encoding=_get_file_encoding())
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}

    def _parse_frontmatter(self, text: str) -> tuple:
        """格式化加载skill"""
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        """获取所有skill的简要描述"""
        if not self.skills:
            return "(no skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        """获取整个skill的内容"""
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"


skill_loader = SkillLoader(SKILL_DIR)


class SkillTool(BaseTool):
    name = "skill"
    description = "获取skill内容"
    param_descriptions = {"skill_name": "skill名称"}

    def execute(self, skill_name: str) -> str:
        """获取skill内容"""
        return skill_loader.get_content(skill_name)
