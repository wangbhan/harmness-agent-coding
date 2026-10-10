"""
1.在每次工具调用完（只要不是tool_use）之后需要将memory存入到对应的地方
2.调用agent时需要将记忆做注入，同时将之前对话和记忆相关的部分也通过模型判断后进行注入
"""
import json
import re
import time

from internal.Agent.tools.base import get_workdir

MEMORY_DIR = ".memory"
WORK_DIR = get_workdir()

_MEMORY_EXTRA: "MemoryExtractor | None" = None

def _get_memory_extractor(client, model, max_tokens) -> "MemoryExtractor":
    """延迟初始化 MemoryExtractor，确保配置和 WORKDIR 已就绪"""
    global _MEMORY_EXTRA
    if _MEMORY_EXTRA is None:
        work_dir = get_workdir()
        _MEMORY_EXTRA = MemoryExtractor(client, model, max_tokens, work_dir)
    return _MEMORY_EXTRA

class MemoryExtractor:

    def __init__(self, client, model, max_tokens, work_dir):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.MEMORY_DIR = work_dir / MEMORY_DIR
        self.MEMORY_INDEX = self.MEMORY_DIR / "MEMORY.md"

    def extract_memories(self, messages: list):
        """从最近对话中提取新记忆，并将其转化为记忆文件"""
        # 提取最近对话
        messages_part = []
        for message in messages[-10:]:
            role = message.get("role")
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(
                    str(getattr(b, "text", "")) for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str) and content.strip():
                messages_part.append(f"{role}: {content}")
        dialogue = "\n".join(messages_part)

        if not dialogue.strip():
            return

        # 列出以往记忆文件信息
        existing = self.list_memory_files()
        existing_desc = "\n".join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"

        prompt = (
            "请从下面的对话中提取用户偏好、约束条件或项目相关事实。\n"
            "返回一个 JSON 数组，每个元素格式如下：{name, type, description, body}。\n"
            "- name：简短的 kebab-case（短横线命名）标识符，例如 'user-preference-tabs'\n"
            "- type：以下四种类型之一：\n"
            "  - 'user'：用户偏好\n"
            "  - 'feedback'：用户提供的指导、要求或反馈\n"
            "  - 'project'：项目相关事实\n"
            "  - 'reference'：外部引用或参考信息\n"
            "- description：用于索引检索的一行摘要\n"
            "- body：完整内容，使用 Markdown 格式编写\n"
            "如果没有发现新的信息，或者信息已经包含在已有记忆中，请返回 []。\n\n"
            f"已有记忆：\n{existing_desc}\n\n"
            f"对话内容：\n{dialogue[:4000]}"
        )

        try:
            response = self.client.messages.create(
                model=self.model, messages=[{"role": "user", "content": prompt}], max_tokens=800
            )
            text = self.extract_text(response.content).strip()
            # Extract JSON array from response
            match = re.search(r'\[.*\]', text, re.DOTALL)
            if not match:
                return
            items = json.loads(match.group())
            if not items:
                return
            count = 0
            for mem in items:
                name = mem.get("name", f"memory_{int(time.time())}")
                mem_type = mem.get("type", "user")
                desc = mem.get("description", "")
                body = mem.get("body", "")
                if desc and body:
                    self.write_memory_file(name, mem_type, desc, body)
                    count += 1
            if count:
                print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
        except Exception:
            pass

    def extract_text(self, content: str) -> str:
        """提取内容中的文本信息"""
        if not isinstance(content, list): return str(content)
        return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")

    def write_memory_file(self, name: str, mem_type: str, desc: str, body: str) -> None:
        """将记忆写到yaml文件中"""
        slug = name.lower().replace(" ", "-").replace("/", "-")
        filename = f"{slug}.md"
        filepath = self.MEMORY_DIR / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(f"---\nname: {name}\ndescription: {desc}\ntype: {mem_type}\n---\n{body}")
        self._rebuild_index()
        return filepath

    def _rebuild_index(self):
        """重建记忆文件索引"""
        lines = []
        for f in sorted(self.MEMORY_DIR.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            raw = f.read_text()
            meta, body = self._parse_frontmatter(raw)
            name = meta.get("name", f.stem)
            desc = meta.get("description", body.split("\n")[0][:80])
            lines.append(f"- [{name}]({f.name}) - {desc}")
        self.MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")

    @staticmethod
    def _parse_frontmatter(text: str):
        """解析 YAML 前置元数据"""
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        meta = {}
        for line in parts[1].strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
        return meta, parts[2].strip()

    def list_memory_files(self):
        """列出所有记忆文件信息"""
        result = []
        for f in sorted(self.MEMORY_DIR.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            raw = f.read_text()
            meta, body = self._parse_frontmatter(raw)
            result.append({
                "filename": f.name,
                "name": meta.get("name", f.stem),
                "description": meta.get("description", ""),
                "type": meta.get("type", "user"),
                "body": body,
            })
        return result
