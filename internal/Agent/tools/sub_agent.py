"""
子代理委派工具
将子任务委派给独立的子代理执行，子代理拥有完整的工具集，
完成指定任务后返回结果摘要。支持延迟绑定子 Agent 实例。
"""
from internal.Agent.tools.base import BaseTool

DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": "将子任务委派给子代理执行。子代理拥有独立的工具集，完成指定任务后返回结果摘要。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要委派给子代理的任务描述，应包含充分的上下文和预期目标",
                },
            },
            "required": ["task"],
        },
    },
}


class DelegateTool(BaseTool):
    name = "delegate"
    description = "将子任务委派给子代理执行"
    schema_override = DELEGATE_SCHEMA

    def __init__(self):
        self._sub_agent = None

    def bind(self, sub_agent) -> None:
        """绑定子 Agent 实例（延迟注入）"""
        self._sub_agent = sub_agent

    def execute(self, task: str) -> str:
        """委派任务给子代理"""
        if self._sub_agent is None:
            return "错误：子代理未初始化"
        try:
            from internal.Agent.system import subagent_system
            sub_messages = [
                {"role": "system", "content": subagent_system},
                {"role": "user", "content": task},
            ]
            self._sub_agent.run(sub_messages)
            last = sub_messages[-1]
            content = last.get("content", "")
            if not content:
                return "子代理未返回文本内容"
            return content[:8000]
        except Exception as e:
            return f"子代理执行失败：{e}"


# 模块级实例，供 __init__.py 注册
delegate_tool = DelegateTool()
