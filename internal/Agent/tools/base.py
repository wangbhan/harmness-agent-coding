"""
工具抽象基类与共享辅助函数
"""
import re
import locale
import platform
import inspect
from abc import ABC, abstractmethod
from pathlib import Path

WORKDIR = Path.cwd().parent


# ============================================================
# 共享辅助函数
# ============================================================

def _get_file_encoding() -> str:
    """根据系统环境返回文件读写编码"""
    if platform.system() == "Windows":
        return "utf-8"
    return locale.getpreferredencoding(False)


def safe_path(p: str) -> Path:
    """
    确认安全path
    :param p: 相对或绝对路径
    :return: 解析后的安全路径
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError("路径超出工作区范围")
    return path


# ============================================================
# Schema 生成辅助函数
# ============================================================

def _python_type_to_json(py_type) -> str:
    """将 Python 类型注解映射为 JSON Schema 类型"""
    mapping = {str: "string", int: "integer", float: "number", bool: "boolean"}
    origin = getattr(py_type, "__origin__", None)
    if origin is not None:
        args = getattr(py_type, "__args__", ())
        if args:
            return _python_type_to_json(args[0])
    return mapping.get(py_type, "string")


def _parse_docstring_summary(doc: str | None, fallback: str) -> str:
    """提取 docstring 首行作为描述"""
    if not doc:
        return fallback
    for line in doc.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.rstrip("，。,.")
    return fallback


def _parse_param_descriptions(doc: str | None) -> dict[str, str]:
    """从 docstring 中解析 :param name: description 格式的参数描述"""
    if not doc:
        return {}
    result = {}
    for line in doc.splitlines():
        m = re.match(r"\s*:param\s+(\w+)\s*:\s*(.+)", line)
        if m:
            result[m.group(1)] = m.group(2).strip()
    return result


# ============================================================
# BaseTool 抽象基类
# ============================================================

class BaseTool(ABC):
    """
    所有工具的抽象基类。

    子类必须实现：
      - name: str 类属性，工具名称
      - execute(): 工具执行逻辑（使用具体参数签名）

    子类可选覆盖：
      - description: str 类属性，默认从 execute 的 docstring 提取
      - param_descriptions: dict，默认从 execute 的 docstring :param 提取
      - schema_override: dict，自定义 OpenAI schema，覆盖自动生成
    """

    name: str = ""
    description: str = ""
    param_descriptions: dict[str, str] = {}
    schema_override: dict | None = None

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """工具执行入口，子类应使用具体参数签名"""
        ...

    def to_openai_schema(self) -> dict:
        """从 execute 方法签名自动生成 OpenAI tool definition"""
        if self.schema_override:
            return self.schema_override

        sig = inspect.signature(self.execute)
        properties = {}
        required = []

        doc = self.execute.__doc__
        resolved_desc = self.description or _parse_docstring_summary(doc, fallback=self.name)
        resolved_param_descs = self.param_descriptions or _parse_param_descriptions(doc)

        for pname, param in sig.parameters.items():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                continue
            annotation = param.annotation if param.annotation is not inspect.Parameter.empty else str
            json_type = _python_type_to_json(annotation)
            prop = {"type": json_type}
            desc = resolved_param_descs.get(pname)
            if desc:
                prop["description"] = desc
            properties[pname] = prop

            if param.default is inspect.Parameter.empty:
                required.append(pname)

        schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": resolved_desc,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                },
            },
        }
        if required:
            schema["function"]["parameters"]["required"] = required
        return schema
