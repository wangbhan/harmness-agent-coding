from internal.Agent.tools.base_tools import tool, safe_path, _get_file_encoding


@tool
def run_read(file_path: str, limit: int = None):
    """
    读取文件，需要在当前目录下完成，保持文件原有格式（分行），
    :param file_path: 文件路径
    :param limit: 限制读取行数
    :return: 文件内容
    """
    try:
        text = safe_path(file_path).read_text(encoding=_get_file_encoding())
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"...{(len(lines) - limit)} more lines"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"读取文件错误：{str(e)}"