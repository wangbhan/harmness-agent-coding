"""
文件读取工具
读取指定路径的文件内容，支持行数限制。
"""
from internal.Agent.tools.base import BaseTool, safe_path, _get_file_encoding


class ReadTool(BaseTool):
    name = "read"
    description = "读取文件内容"
    param_descriptions = {"file_path": "文件路径", "limit": "限制读取行数"}

    def execute(self, file_path: str, limit: int = None) -> str:
        """读取文件内容"""
        try:
            text = safe_path(file_path).read_text(encoding=_get_file_encoding())
            lines = text.splitlines()
            if limit and limit < len(lines):
                lines = lines[:limit] + [f"...{(len(lines) - limit)} more lines"]
            return "\n".join(lines)[:50000]
        except Exception as e:
            return f"读取文件错误：{str(e)}"
