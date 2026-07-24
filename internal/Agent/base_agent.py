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
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=self.max_tokens,
                    tools=self.tools,
                )
            except Exception as exc:
                activity_log.llm_error(
                    request_id=request_id,
                    model=self.model,
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                    error=exc,
                )
                raise

            message = response.choices[0].message
            finish_reason = response.choices[0].finish_reason
            usage = getattr(response, "usage", None)
            if usage is not None:
                if hasattr(usage, "model_dump"):
                    usage = usage.model_dump(exclude_none=True)
                elif not isinstance(usage, dict):
                    usage = {"value": str(usage)}
            activity_log.llm_response(
                finish_reason=finish_reason,
                model=self.model,
                message_count=len(messages),
                has_tool_calls=bool(message.tool_calls),
                request_id=request_id,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                usage=usage,
            )

            assistant_msg = message.model_dump(exclude_none=True)
            messages.append(assistant_msg)

            if finish_reason == "stop":
                stop_result = self.hook_manager.run(
                    HookEvent.STOP,
                    {
                        "assistant_message": message.content or "",
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
                print("回复：", message.content)
                activity_log.agent_reply(message.content or "")
                return message.content or ""

            # Pre Hook 按原始顺序执行，获准的工具继续并行执行。
            tool_calls = message.tool_calls
            prepared_inputs = {}
            results = {}
            approved_calls = []
            hook_contexts: list[str] = []
            for block in tool_calls:
                tool_input = json.loads(block.function.arguments)
                pre_result = self.hook_manager.run(
                    HookEvent.PRE_TOOL_USE,
                    {
                        "tool_name": block.function.name,
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
                        block.function.name,
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
            for block in tool_calls:
                if block.id not in approved_ids:
                    continue
                post_result = self.hook_manager.run(
                    HookEvent.POST_TOOL_USE,
                    {
                        "tool_name": block.function.name,
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
            for block in tool_calls:
                output = results[block.id]
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.id,
                    "content": output,
                })
                if (
                    block.function.name == "compact"
                    and block.id in approved_ids
                ):
                    compact_called = True

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
