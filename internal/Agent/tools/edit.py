from internal.Agent.tools.base import BaseTool, safe_path, _get_file_encoding


class EditTool(BaseTool):
    name = "edit"

    def execute(self, file_path: str, old_text: str, new_text: str) -> str:
        """
        替换文件内容，需要在当前目录下完成，保持文件原有格式（分行），
        :param file_path: 文件路径
        :param old_text: 原文本
        :param new_text: 新文本
        :return: 操作结果
        """
        try:
            fp = safe_path(file_path)
            content = fp.read_text(encoding=_get_file_encoding())
            if old_text not in content:
                return "未找到旧文本"
            fp.write_text(content.replace(old_text, new_text, 1), encoding=_get_file_encoding())
            return "替换成功"
        except Exception as e:
            return f"替换文件错误：{str(e)}"
