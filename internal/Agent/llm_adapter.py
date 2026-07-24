"""
LLM 适配层：统一 OpenAI 和 Anthropic 原生 API 的请求/响应格式。

内部 messages 始终保持 OpenAI 格式；调用 Anthropic API 时在边界做格式转换。
"""
import json
from dataclasses import dataclass, field


# ============================================================
# 统一响应数据类（对外接口与 OpenAI SDK 响应对象兼容）
# ============================================================

@dataclass
class NormalizedFunction:
    name: str
    arguments: str  # JSON 字符串


@dataclass
class NormalizedToolCall:
    id: str
    function: NormalizedFunction


@dataclass
class NormalizedMessage:
    content: str | None
    tool_calls: list[NormalizedToolCall] | None

    def model_dump(self, exclude_none: bool = True) -> dict:
        """返回 OpenAI 格式 dict，供追加到 messages 历史。"""
        result: dict = {"role": "assistant"}
        if self.content is not None:
            result["content"] = self.content
        elif not exclude_none:
            result["content"] = None
        if self.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        return result


@dataclass
class NormalizedChoice:
    message: NormalizedMessage
    finish_reason: str


@dataclass
class NormalizedResponse:
    choices: list[NormalizedChoice]
    usage: dict | None = field(default=None)

    @classmethod
    def from_openai(cls, response) -> "NormalizedResponse":
        choice = response.choices[0]
        msg = choice.message
        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                NormalizedToolCall(
                    id=tc.id,
                    function=NormalizedFunction(
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    ),
                )
                for tc in msg.tool_calls
            ]
        usage = None
        if response.usage is not None:
            if hasattr(response.usage, "model_dump"):
                usage = response.usage.model_dump(exclude_none=True)
            elif isinstance(response.usage, dict):
                usage = response.usage
            else:
                usage = {"value": str(response.usage)}
        return cls(
            choices=[
                NormalizedChoice(
                    message=NormalizedMessage(content=msg.content, tool_calls=tool_calls),
                    finish_reason=choice.finish_reason,
                )
            ],
            usage=usage,
        )

    @classmethod
    def from_anthropic(cls, response) -> "NormalizedResponse":
        content_text: str | None = None
        tool_calls: list[NormalizedToolCall] = []
        for block in response.content:
            if block.type == "text":
                content_text = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    NormalizedToolCall(
                        id=block.id,
                        function=NormalizedFunction(
                            name=block.name,
                            arguments=json.dumps(block.input, ensure_ascii=False),
                        ),
                    )
                )
        stop_reason = response.stop_reason
        if stop_reason == "tool_use":
            finish_reason = "tool_calls"
        elif stop_reason == "end_turn":
            finish_reason = "stop"
        else:
            finish_reason = stop_reason or "stop"
        usage = None
        if response.usage is not None:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
        return cls(
            choices=[
                NormalizedChoice(
                    message=NormalizedMessage(
                        content=content_text,
                        tool_calls=tool_calls if tool_calls else None,
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=usage,
        )


# ============================================================
# OpenAI → Anthropic 格式转换
# ============================================================

def _openai_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """将 OpenAI function calling schema 转为 Anthropic tool schema。"""
    result = []
    for t in tools:
        fn = t.get("function", {})
        result.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


def _openai_messages_to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """
    将 OpenAI 格式 messages 转为 Anthropic 格式。
    返回 (system_prompt, anthropic_messages)。

    转换规则：
    - role:"system" → 合并为顶层 system 参数
    - role:"tool"   → user 消息中的 type:"tool_result" 块（连续的合并为一条）
    - role:"assistant" 含 tool_calls → content 列表含 type:"tool_use" 块
    - role:"user"   → 不变
    """
    system_parts: list[str] = []
    result: list[dict] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if content:
                system_parts.append(content)
            continue

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content or "",
            }
            # 连续 tool 消息合并进同一条 user 消息
            if result and result[-1]["role"] == "user" and isinstance(result[-1].get("content"), list):
                result[-1]["content"].append(block)
            else:
                result.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                anthropic_content: list[dict] = []
                if content:
                    anthropic_content.append({"type": "text", "text": content})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    arguments = fn.get("arguments", "{}")
                    try:
                        input_data = json.loads(arguments)
                    except json.JSONDecodeError:
                        input_data = {}
                    anthropic_content.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": input_data,
                    })
                result.append({"role": "assistant", "content": anthropic_content})
            else:
                result.append({"role": "assistant", "content": content or ""})
            continue

        if role == "user":
            result.append({"role": "user", "content": content or ""})
            continue

    return "\n\n".join(system_parts), result


# ============================================================
# 统一适配器
# ============================================================

class LLMAdapter:
    """统一封装 OpenAI 和 Anthropic 客户端，对外暴露相同的 create() 接口。"""

    def __init__(self, client, provider: str):
        self.client = client
        self.provider = provider

    def create(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
    ) -> NormalizedResponse:
        if self.provider == "anthropic":
            return self._anthropic_create(model, messages, tools, max_tokens)
        return self._openai_create(model, messages, tools, max_tokens)

    def _openai_create(self, model, messages, tools, max_tokens) -> NormalizedResponse:
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        response = self.client.chat.completions.create(**kwargs)
        return NormalizedResponse.from_openai(response)

    def _anthropic_create(self, model, messages, tools, max_tokens) -> NormalizedResponse:
        system, anthropic_messages = _openai_messages_to_anthropic(messages)
        kwargs: dict = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _openai_tools_to_anthropic(tools)
        response = self.client.messages.create(**kwargs)
        return NormalizedResponse.from_anthropic(response)
