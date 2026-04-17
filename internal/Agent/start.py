import os
from datetime import datetime, timezone

from openai import OpenAI

from internal.Agent.tools import default_registry, WORKDIR

key = os.environ["ZAI_API_KEY"]

client = OpenAI(
    base_url="https://api.z.ai/api/coding/paas/v4",
    api_key=key
)
# 时间采用utc时间
now_utc = datetime.now(timezone.utc)
system = (f"现在时间是：{now_utc}\n  你是一个在{WORKDIR}下的coding agent助手，优先使用todo tool规划多步骤任务后再执行，"
          f"开始前标记为“进行中”，完成后标记为“已完成”，优先使用工具而非文字描述，"
          f"并且注意当前环境是Windows环境，编写的代码注释和回答必须是用中文回答")

# 从注册表自动生成所有已注册工具的 OpenAI schema
tools = default_registry.get_openai_tools()


def agent_loop(messages: list[dict]):
    """
    执行agent循环
    :param messages:
    :return:
    """
    while True:
        response = client.chat.completions.create(
            model="glm-5.1",
            messages=messages,
            max_tokens=8000,
            tools=tools,
        )
        message = response.choices[0].message
        finish_reason = response.choices[0].finish_reason
        print("response:", response)

        # 构建完整的 assistant message - 此处需要添加model_dump来保存使用过的工具和对话信息而非单纯保存对话信息
        # assistant_msg = {"role": "assistant", "content": message.content}
        assistant_msg = message.model_dump(exclude_none=True)
        messages.append(assistant_msg)

        # 不使用工具，直接返回
        if finish_reason == "stop":
            print("回复：", message.content)
            return

        # 通过注册表分发工具调用
        for block in message.tool_calls:
            print(f"工具调用 [{block.function.name}]：", block.function.arguments)
            output = default_registry.call(block.function.name, block.function.arguments)
            print(f"执行结果:", output[:200])
            messages.append({
                "role": "tool",
                "tool_call_id": block.id,
                "content": output,
            })

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
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
