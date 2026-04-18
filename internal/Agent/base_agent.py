from concurrent.futures import ThreadPoolExecutor, as_completed


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