from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import time
import uuid

from internal.Agent.config import get_config
from internal.Agent.hooks import HookEvent, get_hook_manager
from internal.conversation_log import get_logger
from internal.Agent.tools.background import _get_bg_manager
from internal.Agent.tools.compact import micro_compact, auto_compact, snip_compact, tool_result_budget


# ============================================================
# Agent 类
# ============================================================

class Agent:
    """LLM Agent，封装客户端、工具集和对话循环"""

    def __init__(
        self,
        client,
        registry,
        tools,
        model=None,
        max_tokens=None,
        hook_manager=None,
        process_user_prompts=True,
    ):
        cfg = get_config().llm
        self.client = client
        self.registry = registry
        self.tools = tools
        self.model = model or cfg.default_model
        self.max_tokens = max_tokens if max_tokens is not None else cfg.default_max_tokens
        self.stream = cfg.stream
        self.hook_manager = hook_manager if hook_manager is not None else get_hook_manager()
        self.process_user_prompts = process_user_prompts

    @staticmethod
    def _extract_system_message(messages: list[dict]) -> dict | None:
        for msg in messages:
            if msg.get("role") == "system":
                return msg
        return None

    @staticmethod
    def _context_message(contexts: list[str] | tuple[str, ...]) -> dict | None:
        filtered = [context for context in contexts if context]
        if not filtered:
            return None
        return {"role": "system", "content": "\n\n".join(filtered)}

    def _process_user_prompt(self, messages: list[dict]) -> str | None:
        if (
            not self.process_user_prompts
            or not messages
            or messages[-1].get("role") != "user"
            or not isinstance(messages[-1].get("content"), str)
        ):
            return None

        result = self.hook_manager.run(
            HookEvent.USER_PROMPT_SUBMIT,
            {"prompt": messages[-1]["content"]},
        )
        if result.blocked:
            reason = result.reason or "用户输入被 Hook 拒绝"
            messages.pop()
            print("Hook 拒绝：", reason)
            return reason

        if result.updated_prompt is not None:
            messages[-1]["content"] = result.updated_prompt
        context_message = self._context_message(result.additional_context)
        if context_message is not None:
            messages.insert(len(messages) - 1, context_message)
        return None

    def run(self, messages: list[dict]) -> str | None:
        """执行 agent 循环，直接修改 messages 列表。同一轮中的多个工具调用并行执行。"""
        prompt_block_reason = self._process_user_prompt(messages)
        if prompt_block_reason is not None:
            return prompt_block_reason

        stop_continuation_count = 0
        while True:
            # 清除BG中完成的任务并且做为消息注入
            notifs = _get_bg_manager().drain_notifications()
            if notifs:
                lines = ["以下后台任务已完成："]
                for n in notifs:
                    lines.append(f"任务 {n['task_id']}：\n  状态：{n['status']}\n  命令：{n['command']}")
                messages.append({"role": "system", "content": "\n".join(lines)})
            # 被动压缩旧 tool_result
            messages[:] = tool_result_budget(messages)
            messages[:] = snip_compact(messages)
            messages[:] = micro_compact(messages)

            request_id = f"req_{uuid.uuid4().hex}"
            activity_log = get_logger()
            activity_log.llm_request(
                request_id=request_id,
                model=self.model,
                message_count=len(messages),
                tool_count=len(self.tools),
            )
            started_at = time.perf_counter()
            try:
                system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
                dialog = [m for m in messages if m.get("role") != "system"]
                kwargs = {
                    "model": self.model,
                    "messages": dialog,
                    "max_tokens": self.max_tokens,
                }
                if self.tools:
                    kwargs["tools"] = self.tools
                if system_parts:
                    kwargs["system"] = "\n\n".join(system_parts)
                if self.stream:
                    printed_text = False
                    with self.client.messages.stream(**kwargs) as stream:
                        for chunk in stream.text_stream:
                            print(chunk, end="", flush=True)
                            printed_text = True
                        response = stream.get_final_message()
                    if printed_text:
                        print()
                else:
                    response = self.client.messages.create(**kwargs)
            except Exception as exc:
                activity_log.llm_error(
                    request_id=request_id,
                    model=self.model,
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                    error=exc,
                )
                raise

            stop_reason = response.stop_reason
            usage = getattr(response, "usage", None)
            if usage is not None and hasattr(usage, "model_dump"):
                usage = usage.model_dump()
            assistant_content = []
            for b in response.content:
                if b.type == "text":
                    assistant_content.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    assistant_content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            assistant_msg = {"role": "assistant", "content": assistant_content}
            messages.append(assistant_msg)
            text = "".join(b.text for b in response.content if b.type == "text")
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            activity_log.llm_response(
                stop_reason=stop_reason,
                model=self.model,
                message_count=len(messages),
                has_tool_uses=bool(tool_uses),
                request_id=request_id,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                usage=usage,
            )

            if stop_reason == "end_turn":
                stop_result = self.hook_manager.run(
                    HookEvent.STOP,
                    {
                        "assistant_message": text,
                        "continuation_count": stop_continuation_count,
                    },
                )
                if stop_result.blocked and (
                    stop_continuation_count
                    < self.hook_manager.stop_max_continuations
                ):
                    continuation_parts = [
                        stop_result.reason or "Stop Hook 要求继续执行",
                        *stop_result.additional_context,
                    ]
                    context_message = self._context_message(continuation_parts)
                    if context_message is not None:
                        messages.append(context_message)
                    stop_continuation_count += 1
                    continue
                if stop_result.blocked:
                    activity_log.warning(
                        "stop_hook_continuation_limit",
                        {
                            "limit": self.hook_manager.stop_max_continuations,
                            "continuation_count": stop_continuation_count,
                        },
                    )
                if not self.stream:
                    print("回复：", text)
                activity_log.agent_reply(text)
                return text

            # Pre Hook 按原始顺序执行，获准的工具继续并行执行。
            prepared_inputs = {}
            results = {}
            approved_calls = []
            hook_contexts: list[str] = []
            for block in tool_uses:
                tool_input = block.input
                pre_result = self.hook_manager.run(
                    HookEvent.PRE_TOOL_USE,
                    {
                        "tool_name": block.name,
                        "tool_input": tool_input,
                        "tool_use_id": block.id,
                    },
                )
                hook_contexts.extend(pre_result.additional_context)
                if pre_result.blocked:
                    results[block.id] = pre_result.reason or "工具调用被 Hook 拒绝"
                    continue
                prepared_inputs[block.id] = (
                    pre_result.updated_input
                    if pre_result.updated_input is not None
                    else tool_input
                )
                approved_calls.append(block)

            with ThreadPoolExecutor() as executor:
                future_to_id = {
                    executor.submit(
                        self.registry.call,
                        block.name,
                        json.dumps(prepared_inputs[block.id], ensure_ascii=False),
                        block.id,
                    ): block.id
                    for block in approved_calls
                }
                for future in as_completed(future_to_id):
                    call_id = future_to_id[future]
                    results[call_id] = future.result()

            # Post Hook 也按原始顺序执行，保证副作用确定。
            approved_ids = {block.id for block in approved_calls}
            for block in tool_uses:
                if block.id not in approved_ids:
                    continue
                post_result = self.hook_manager.run(
                    HookEvent.POST_TOOL_USE,
                    {
                        "tool_name": block.name,
                        "tool_input": prepared_inputs[block.id],
                        "tool_use_id": block.id,
                        "tool_output": results[block.id],
                    },
                )
                hook_contexts.extend(post_result.additional_context)
                if post_result.blocked:
                    results[block.id] = post_result.reason or "工具结果被 Hook 拒绝"
                elif post_result.updated_output is not None:
                    results[block.id] = post_result.updated_output

            # 按原始顺序追加结果，最后再注入上下文，保持协议要求的消息顺序。
            compact_called = False
            tool_result_blocks = []
            for block in tool_uses:
                output = results[block.id]
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
                if (
                    block.name == "compact"
                    and block.id in approved_ids
                ):
                    compact_called = True
            messages.append({"role": "user", "content": tool_result_blocks})

            context_message = self._context_message(hook_contexts)
            if context_message is not None:
                messages.append(context_message)

            # compact 工具调用后，压缩全部消息（此时所有并行工具结果已追加）
            if compact_called:
                system_msg = self._extract_system_message(messages)
                compact_content = auto_compact(messages, client=self.client, model=self.model)
                new = []
                if system_msg:
                    new.append(system_msg)
                new.append({"role": "user", "content": compact_content})
                messages[:] = new
