from internal.Agent.tools.base import BaseTool, safe_path, _get_file_encoding


class WriteTool(BaseTool):
    name = "write"

    def execute(self, file_path: str, content: str) -> str:
        """
        写入文件，需要在当前目录下完成，保持文件原有格式（分行），
        :param file_path: 文件路径
        :param content: 写入内容
        :return: 操作结果
        """
        try:
            fp = safe_path(file_path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding=_get_file_encoding())
            return "写入成功"
        except Exception as e:
            return f"写入文件错误：{str(e)}"
