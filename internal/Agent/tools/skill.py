"""
# 1.找到skill的路径，对其进行遍历

# 2.对每个skill里面的skill.md进行re正则匹配解析，得到基础摘要和总体内容

# 3.对所有的skill进行描述拼接方便放入提示词中

# 4.
"""

import re
from pathlib import Path

import yaml

from internal.Agent.tools.base_tools import tool, _get_file_encoding

# 基于当前文件来获取Skill的绝对路径
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

@tool
def run_skill(skill_name: str):
    return skill_loader.get_content(skill_name)