"""
工具注册表
"""
import json

from internal.Agent.tools.base import BaseTool


class ToolRegistry:
    """工具注册表，管理工具注册、schema 生成和调用分发"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool_instance: BaseTool) -> None:
        """注册一个工具实例"""
        self._tools[tool_instance.name] = tool_instance

    def get_openai_tools(self, exclude: set[str] | None = None) -> list[dict]:
        """返回所有已注册工具的 OpenAI 格式 schema 列表"""
        exclude = exclude or set()
        return [
            tool.to_openai_schema()
            for name, tool in self._tools.items()
            if name not in exclude
        ]

    def call(self, name: str, arguments_json: str) -> str:
        """根据工具名和 JSON 参数字符串调用对应工具的 execute 方法"""
        tool_instance = self._tools.get(name)
        if tool_instance is None:
            return f"未知工具: {name}"
        kwargs = json.loads(arguments_json)
        return tool_instance.execute(**kwargs)


# 模块级默认注册表
default_registry = ToolRegistry()
