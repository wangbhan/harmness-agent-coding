from concurrent.futures import ThreadPoolExecutor, as_completed

from internal.Agent.config import get_config
from internal.Agent.tools.compact import micro_compact, auto_compact


# ============================================================
# Agent 类
# ============================================================

class Agent:
    """LLM Agent，封装客户端、工具集和对话循环"""

    def __init__(self, client, registry, tools, model=None, max_tokens=None, session_log=None):
        cfg = get_config().llm
        self.client = client
        self.registry = registry
        self.tools = tools
        self.model = model or cfg.default_model
        self.max_tokens = max_tokens if max_tokens is not None else cfg.default_max_tokens
        self.log = session_log

    @staticmethod
    def _extract_system_message(messages: list[dict]) -> dict | None:
        for msg in messages:
            if msg.get("role") == "system":
                return msg
        return None

    def run(self, messages: list[dict]) -> None:
        """执行 agent 循环，直接修改 messages 列表。同一轮中的多个工具调用并行执行。"""
        while True:
            # 被动压缩旧 tool_result
            messages = micro_compact(messages)

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
                tools=self.tools,
            )
            message = response.choices[0].message
            finish_reason = response.choices[0].finish_reason
            print("response:", response)
            if self.log:
                self.log.llm_response(
                    finish_reason=finish_reason,
                    model=self.model,
                    message_count=len(messages),
                    has_tool_calls=bool(message.tool_calls),
                )

            assistant_msg = message.model_dump(exclude_none=True)
            messages.append(assistant_msg)

            if finish_reason == "stop":
                print("回复：", message.content)
                if self.log:
                    self.log.agent_reply(message.content or "")
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
            compact_called = False
            for block in tool_calls:
                output = results[block.id]
                print(f"工具调用 [{block.function.name}]：", block.function.arguments)
                print(f"执行结果:", output[:200])
                if self.log:
                    self.log.tool_call(
                        tool_name=block.function.name,
                        arguments=block.function.arguments,
                        call_id=block.id,
                    )
                    self.log.tool_result(
                        tool_name=block.function.name,
                        call_id=block.id,
                        result=output,
                    )
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.id,
                    "content": output,
                })
                if block.function.name == "compact":
                    compact_called = True

            # compact 工具调用后，压缩全部消息（此时所有并行工具结果已追加）
            if compact_called:
                system_msg = self._extract_system_message(messages)
                compact_content = auto_compact(messages, client=self.client, model=self.model)
                new = []
                if system_msg:
                    new.append(system_msg)
                new.append({"role": "user", "content": compact_content})
                messages[:] = new
