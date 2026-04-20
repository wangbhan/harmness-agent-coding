"""
文件写入工具
将内容写入指定路径的文件，自动创建不存在的父目录。
"""
from internal.Agent.tools.base import BaseTool, safe_path, _get_file_encoding


class WriteTool(BaseTool):
    name = "write"
    description = "写入文件内容"
    param_descriptions = {"file_path": "文件路径", "content": "写入内容"}

    def execute(self, file_path: str, content: str) -> str:
        """写入文件内容"""
        try:
            fp = safe_path(file_path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding=_get_file_encoding())
            return "写入成功"
        except Exception as e:
            return f"写入文件错误：{str(e)}"
