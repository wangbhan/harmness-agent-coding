"""
工具注册表
"""
import json
import time
import uuid

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

    def call(self, name: str, arguments_json: str, call_id: str | None = None) -> str:
        """调用工具，并实时记录开始、完成、失败及耗时。"""
        # 懒导入可避免 conversation_log -> tools.base -> registry 的导入环。
        from internal.conversation_log import get_logger

        resolved_call_id = call_id or f"call_{uuid.uuid4().hex}"
        activity_log = get_logger()
        activity_log.tool_call(name, arguments_json, resolved_call_id)
        started_at = time.perf_counter()

        tool_instance = self._tools.get(name)
        if tool_instance is None:
            error = LookupError(f"未知工具: {name}")
            activity_log.tool_error(
                name,
                resolved_call_id,
                error,
                duration_ms=(time.perf_counter() - started_at) * 1000,
            )
            return str(error)

        try:
            kwargs = json.loads(arguments_json)
            result = tool_instance.execute(**kwargs)
        except Exception as exc:
            activity_log.tool_error(
                name,
                resolved_call_id,
                exc,
                duration_ms=(time.perf_counter() - started_at) * 1000,
            )
            raise

        # BaseTool 约定返回字符串；统一转换可保证日志截断及消息回传安全。
        result = str(result)
        activity_log.tool_result(
            name,
            resolved_call_id,
            result,
            duration_ms=(time.perf_counter() - started_at) * 1000,
        )
        return result


# 模块级默认注册表
default_registry = ToolRegistry()
