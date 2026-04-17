import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI

from internal.Agent.tools import ToolDescriptor, default_registry, WORKDIR

key = os.environ["ZAI_API_KEY"]

client = OpenAI(
    base_url="https://api.z.ai/api/coding/paas/v4",
    api_key=key
)

# 时间采用utc时间
now_utc = datetime.now(timezone.utc)

system = (f"现在时间是：{now_utc}\n"
          f"你是一个在{WORKDIR}下的coding agent助手，使用任务工具来委派探索性任务或子任务。")

subagent_system = (f"现在时间是：{now_utc}\n"
                   f"你是一个在{WORKDIR}下的coding agent助手，完成给定任务，然后总结你的发现。")


# ============================================================
# Agent 类
# ============================================================

class Agent:
    """LLM Agent，封装客户端、工具集和对话循环"""

    def __init__(self, client, registry, tools, model="glm-5.1", max_tokens=8000):
        self.client = client
        self.registry = registry
        self.tools = tools
        self.model = model
        self.max_tokens = max_tokens

    def run(self, messages: list[dict]) -> None:
        """执行 agent 循环，直接修改 messages 列表。同一轮中的多个工具调用并行执行。"""
        while True:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
                tools=self.tools,
            )
            message = response.choices[0].message
            finish_reason = response.choices[0].finish_reason
            print("response:", response)

            assistant_msg = message.model_dump(exclude_none=True)
            messages.append(assistant_msg)

            if finish_reason == "stop":
                print("回复：", message.content)
                return

            # 并行执行所有工具调用 - 一次请求中存在多个工具调用的情况
            tool_calls = message.tool_calls
            with ThreadPoolExecutor() as executor:
                future_to_id = {
                    executor.submit(
                        self.registry.call, block.function.name, block.function.arguments
                    ): block.id
                    for block in tool_calls
                }
                results = {}
                for future in as_completed(future_to_id):
                    call_id = future_to_id[future]
                    results[call_id] = future.result()

            # 按原始顺序追加结果
            for block in tool_calls:
                output = results[block.id]
                print(f"工具调用 [{block.function.name}]：", block.function.arguments)
                print(f"执行结果:", output[:200])
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.id,
                    "content": output,
                })


# ============================================================
# 子代理委派工具
# ============================================================

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

# 2. 构建父 agent（基础工具 + delegate）
parent_agent = Agent(
    client=client,
    registry=default_registry,
    tools=default_registry.get_openai_tools(),
)


# ============================================================
# CLI 入口
# ============================================================

if __name__ == '__main__':
    history = [
        {"role": "system", "content": system},
    ]
    while True:
        try:
            query = input("请输入问题：")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        parent_agent.run(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
