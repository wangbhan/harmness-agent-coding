"""
工具类函数与注册类
    此部分提取工具类函数的信息后转化为相应厂商对应的工具类注册后放在api接口中使用
"""
import re
import json
import locale
import platform
import inspect
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable

WORKDIR = Path.cwd().parent

# ============================================================
# 工具注册框架
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


def _parse_docstring_summary(fn: Callable) -> str:
    """提取 docstring 首行作为工具描述"""
    doc = fn.__doc__
    if not doc:
        return fn.__name__
    for line in doc.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.rstrip("，。,.")
    return fn.__name__


def _parse_param_descriptions(fn: Callable) -> dict[str, str]:
    """从 docstring 中解析 :param name: description 格式的参数描述"""
    doc = fn.__doc__
    if not doc:
        return {}
    result = {}
    for line in doc.splitlines():
        m = re.match(r"\s*:param\s+(\w+)\s*:\s*(.+)", line)
        if m:
            result[m.group(1)] = m.group(2).strip()
    return result


@dataclass
class ToolDescriptor:
    """工具描述符，持有 handler 及元数据，可自动生成 OpenAI 格式 schema"""
    handler: Callable # 工具处理函数
    name: str
    description: str
    param_descriptions: dict[str, str] = field(default_factory=dict)
    schema_override: dict | None = None  # 自定义 schema，覆盖自动生成

    def to_openai_schema(self) -> dict:
        """从 handler 函数签名自动生成 OpenAI tool definition，如有 schema_override 则直接使用"""
        # 优先使用自定义 schema
        if self.schema_override:
            return self.schema_override
        # 获取函数签名
        sig = inspect.signature(self.handler)
        properties = {}
        required = []
        # 递归获取函数参数，以下示例
        # sig:  (command: str) -> str
        # pname:  command
        # param:  command: str
        # param.annotation:  <class 'str'>
        for pname, param in sig.parameters.items():
            json_type = _python_type_to_json(param.annotation)
            prop = {"type": json_type}
            desc = self.param_descriptions.get(pname)
            if desc:
                prop["description"] = desc
            properties[pname] = prop

            if param.default is inspect.Parameter.empty:
                required.append(pname)

        schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                },
            },
        }
        if required:
            schema["function"]["parameters"]["required"] = required
        return schema


class ToolRegistry:
    """工具注册表，管理工具注册、schema 生成和调用分发"""

    def __init__(self):
        self._tools: dict[str, ToolDescriptor] = {}

    def register(self, descriptor: ToolDescriptor) -> None:
        self._tools[descriptor.name] = descriptor

    def get_openai_tools(self, exclude: set[str] | None = None) -> list[dict]:
        """返回所有已注册工具的 OpenAI 格式 schema 列表, 并且可以选择排除相应的工具"""
        exclude = exclude or set()
        return [t.to_openai_schema() for name, t in self._tools.items() if name not in exclude]

    def call(self, name: str, arguments_json: str) -> str:
        """根据工具名和 JSON 参数字符串调用对应 handler"""
        descriptor = self._tools.get(name)
        if descriptor is None:
            return f"未知工具: {name}"
        kwargs = json.loads(arguments_json)
        # 写入参数并执行工具
        return descriptor.handler(**kwargs)


# 模块级默认注册表
default_registry = ToolRegistry()

def tool(
    handler: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    schema: dict | None = None,
) -> Callable:
    """装饰器：将函数注册为可用工具

    用法:
        @tool
        def run_bash(command: str) -> str:
            '''运行bash命令
            :param command: bash命令
            '''
            ...

        # 或指定自定义名称和描述
        @tool(name="shell", description="执行shell命令")
        def run_bash(command: str) -> str:
            ...

        # 复杂参数类型可提供自定义 schema
        @tool(schema=CUSTOM_SCHEMA)
        def run_complex(items: list):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        tool_name = name or fn.__name__.removeprefix("run_")
        tool_desc = description or _parse_docstring_summary(fn)
        param_descs = _parse_param_descriptions(fn)

        # 对工具进行封装
        descriptor = ToolDescriptor(
            handler=fn,
            name=tool_name,
            description=tool_desc,
            param_descriptions=param_descs,
            schema_override=schema,
        )
        # 注册工具
        default_registry.register(descriptor)
        return fn

    if handler is not None:
        return decorator(handler)
    return decorator

# ============================================================
# 安全路径工具
# ============================================================

def _get_file_encoding() -> str:
    """根据系统环境返回文件读写编码"""
    # 针对与Windows系统中出现乱码的情况进行单独处理
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