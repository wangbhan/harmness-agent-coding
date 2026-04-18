# ============================================================
# 子代理委派工具
# ============================================================
from internal.Agent.base_agent import Agent
from internal.Agent.llm_config import client
from internal.Agent.system import subagent_system
from internal.Agent.tools.base_tools import default_registry, ToolDescriptor

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


def make_delegate_handler(sub_agent: Agent):
    """工厂函数：创建 delegate handler，闭包捕获子 agent 实例"""
    def run_delegate(task: str) -> str:
        """将任务委派给子代理执行"""
        try:
            sub_messages = [
                {"role": "system", "content": subagent_system},
                {"role": "user", "content": task},
            ]
            # 此处执行子agent的agent_loop
            sub_agent.run(sub_messages)
            # 取出最后总计欸的一段
            last = sub_messages[-1]
            content = last.get("content", "")
            if not content:
                return "子代理未返回文本内容"
            return content[:8000]
        except Exception as e:
            return f"子代理执行失败：{e}"
    return run_delegate


# ============================================================
# 组装父子 Agent
# ============================================================

# 1. 注册 delegate 工具到 default_registry
sub_agent = Agent(
    client=client,
    registry=default_registry,
    tools=default_registry.get_openai_tools(exclude={"delegate"}),
)

default_registry.register(ToolDescriptor(
    handler=make_delegate_handler(sub_agent),
    name="delegate",
    description="将子任务委派给子代理执行",
    schema_override=DELEGATE_SCHEMA,
))