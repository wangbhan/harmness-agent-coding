import copy
import io
import json
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from internal.Agent.base_agent import Agent
from internal.Agent.hooks import HookEvent, HookResult
from internal.Agent.tools.base import BaseTool
from internal.Agent.tools.registry import ToolRegistry


class FakeMessage:
    def __init__(self, content="done", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, **_kwargs):
        result = {"role": "assistant", "content": self.content}
        if self.tool_calls is not None:
            result["tool_calls"] = self.tool_calls
        return result


def tool_call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        message, finish_reason = self.responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
            usage=None,
        )


class FakeHookManager:
    def __init__(self, handler=None, stop_max_continuations=5):
        self.handler = handler or (lambda _event, _payload: HookResult())
        self.stop_max_continuations = stop_max_continuations
        self.calls = []

    def run(self, event, payload):
        self.calls.append((event, copy.deepcopy(payload)))
        return self.handler(event, payload)


def make_agent(responses, hook_manager, registry=None, **kwargs):
    completions = FakeCompletions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = Agent(
        client=client,
        registry=registry or ToolRegistry(),
        tools=[],
        model="test-model",
        hook_manager=hook_manager,
        **kwargs,
    )
    return agent, completions


class UserPromptHookTest(unittest.TestCase):
    def test_prompt_is_rewritten_and_context_is_injected_before_llm(self):
        manager = FakeHookManager(
            lambda event, _payload: HookResult(
                updated_prompt="rewritten",
                additional_context=("policy context",),
            ) if event is HookEvent.USER_PROMPT_SUBMIT else HookResult()
        )
        agent, completions = make_agent(
            [(FakeMessage("done"), "stop")], manager
        )
        messages = [
            {"role": "system", "content": "base"},
            {"role": "user", "content": "original"},
        ]

        with redirect_stdout(io.StringIO()):
            agent.run(messages)

        sent = completions.calls[0]["messages"]
        self.assertEqual(sent[1], {"role": "system", "content": "policy context"})
        self.assertEqual(sent[2], {"role": "user", "content": "rewritten"})
        self.assertEqual(manager.calls[0][1]["prompt"], "original")

    def test_blocked_prompt_skips_llm_and_is_removed_from_history(self):
        manager = FakeHookManager(
            lambda event, _payload: HookResult(blocked=True, reason="rejected")
            if event is HookEvent.USER_PROMPT_SUBMIT else HookResult()
        )
        agent, completions = make_agent([], manager)
        messages = [
            {"role": "system", "content": "base"},
            {"role": "user", "content": "secret rejected text"},
        ]
        output = io.StringIO()

        with redirect_stdout(output):
            result = agent.run(messages)

        self.assertEqual(result, "rejected")
        self.assertEqual(completions.calls, [])
        self.assertEqual(messages, [{"role": "system", "content": "base"}])
        self.assertIn("rejected", output.getvalue())

    def test_internal_subagent_task_skips_user_prompt_event(self):
        manager = FakeHookManager()
        agent, completions = make_agent(
            [(FakeMessage("done"), "stop")],
            manager,
            process_user_prompts=False,
        )

        with redirect_stdout(io.StringIO()):
            agent.run([{"role": "user", "content": "delegated task"}])

        self.assertEqual(len(completions.calls), 1)
        self.assertNotIn(
            HookEvent.USER_PROMPT_SUBMIT,
            [event for event, _payload in manager.calls],
        )


class StopHookTest(unittest.TestCase):
    def test_stop_block_injects_reason_and_forces_another_llm_call(self):
        stop_count = 0

        def handler(event, _payload):
            nonlocal stop_count
            if event is not HookEvent.STOP:
                return HookResult()
            stop_count += 1
            if stop_count == 1:
                return HookResult(
                    blocked=True,
                    reason="continue working",
                    additional_context=("check tests",),
                )
            return HookResult()

        manager = FakeHookManager(handler)
        agent, completions = make_agent(
            [(FakeMessage("first"), "stop"), (FakeMessage("final"), "stop")],
            manager,
        )
        messages = [{"role": "user", "content": "work"}]

        with redirect_stdout(io.StringIO()):
            agent.run(messages)

        self.assertEqual(len(completions.calls), 2)
        injected = completions.calls[1]["messages"][-1]
        self.assertEqual(injected["role"], "system")
        self.assertIn("continue working", injected["content"])
        self.assertIn("check tests", injected["content"])

    def test_stop_cannot_force_more_than_five_continuations(self):
        def handler(event, _payload):
            if event is HookEvent.STOP:
                return HookResult(blocked=True, reason="again")
            return HookResult()

        manager = FakeHookManager(handler, stop_max_continuations=5)
        responses = [(FakeMessage(f"answer-{index}"), "stop") for index in range(6)]
        agent, completions = make_agent(responses, manager)

        with redirect_stdout(io.StringIO()):
            agent.run([{"role": "user", "content": "work"}])

        self.assertEqual(len(completions.calls), 6)
        stop_payloads = [
            payload for event, payload in manager.calls if event is HookEvent.STOP
        ]
        self.assertEqual(
            [payload["continuation_count"] for payload in stop_payloads],
            [0, 1, 2, 3, 4, 5],
        )


class RecordingTool(BaseTool):
    def __init__(self, name, records, delay=0):
        self.name = name
        self.records = records
        self.delay = delay

    def execute(self, text: str) -> str:
        self.records.append((self.name, text, "start", time.perf_counter()))
        time.sleep(self.delay)
        self.records.append((self.name, text, "end", time.perf_counter()))
        return f"{self.name}:{text}"


class ToolHookTest(unittest.TestCase):
    def test_pre_and_post_hooks_transform_and_block_in_original_order(self):
        hook_order = []

        def handler(event, payload):
            if event is HookEvent.PRE_TOOL_USE:
                hook_order.append(("pre", payload["tool_use_id"]))
                if payload["tool_use_id"] == "a":
                    return HookResult(
                        updated_input={"text": "changed"},
                        additional_context=("pre-context",),
                    )
                return HookResult(blocked=True, reason="tool denied")
            if event is HookEvent.POST_TOOL_USE:
                hook_order.append(("post", payload["tool_use_id"]))
                return HookResult(
                    updated_output=payload["tool_output"].upper(),
                    additional_context=("post-context",),
                )
            return HookResult()

        records = []
        registry = ToolRegistry()
        registry.register(RecordingTool("alpha", records))
        registry.register(RecordingTool("beta", records))
        manager = FakeHookManager(handler)
        calls = [
            tool_call("a", "alpha", {"text": "original"}),
            tool_call("b", "beta", {"text": "blocked"}),
        ]
        agent, _completions = make_agent(
            [(FakeMessage(None, calls), "tool_calls"), (FakeMessage("done"), "stop")],
            manager,
            registry,
        )
        messages = [{"role": "user", "content": "run"}]

        with redirect_stdout(io.StringIO()):
            agent.run(messages)

        self.assertEqual(hook_order, [("pre", "a"), ("pre", "b"), ("post", "a")])
        self.assertEqual(
            [(name, text) for name, text, phase, _at in records if phase == "start"],
            [("alpha", "changed")],
        )
        tool_messages = [message for message in messages if message["role"] == "tool"]
        self.assertEqual(tool_messages[0]["content"], "ALPHA:CHANGED")
        self.assertEqual(tool_messages[1]["content"], "tool denied")
        context_message = next(
            message for message in messages
            if message["role"] == "system" and "pre-context" in message["content"]
        )
        self.assertIn("post-context", context_message["content"])
        self.assertGreater(messages.index(context_message), messages.index(tool_messages[-1]))

    def test_approved_tools_remain_parallel(self):
        records = []
        registry = ToolRegistry()
        registry.register(RecordingTool("slow_a", records, delay=0.15))
        registry.register(RecordingTool("slow_b", records, delay=0.15))
        manager = FakeHookManager()
        calls = [
            tool_call("a", "slow_a", {"text": "one"}),
            tool_call("b", "slow_b", {"text": "two"}),
        ]
        agent, _completions = make_agent(
            [(FakeMessage(None, calls), "tool_calls"), (FakeMessage("done"), "stop")],
            manager,
            registry,
        )

        started = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            agent.run([{"role": "user", "content": "run"}])
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.27)
        self.assertEqual(
            [event for event, _payload in manager.calls].count(HookEvent.PRE_TOOL_USE),
            2,
        )
        self.assertEqual(
            [event for event, _payload in manager.calls].count(HookEvent.POST_TOOL_USE),
            2,
        )

    def test_denied_compact_call_does_not_trigger_compaction(self):
        def handler(event, payload):
            if (
                event is HookEvent.PRE_TOOL_USE
                and payload["tool_name"] == "compact"
            ):
                return HookResult(blocked=True, reason="compact denied")
            return HookResult()

        manager = FakeHookManager(handler)
        calls = [tool_call("compact-1", "compact", {})]
        agent, _completions = make_agent(
            [(FakeMessage(None, calls), "tool_calls"), (FakeMessage("done"), "stop")],
            manager,
        )

        with patch("internal.Agent.base_agent.auto_compact") as compact_mock:
            with redirect_stdout(io.StringIO()):
                agent.run([{"role": "user", "content": "run"}])

        compact_mock.assert_not_called()

    def test_blocked_post_hook_replaces_tool_output(self):
        def handler(event, _payload):
            if event is HookEvent.POST_TOOL_USE:
                return HookResult(blocked=True, reason="unsafe output")
            return HookResult()

        records = []
        registry = ToolRegistry()
        registry.register(RecordingTool("echo", records))
        manager = FakeHookManager(handler)
        calls = [tool_call("echo-1", "echo", {"text": "secret"})]
        agent, _completions = make_agent(
            [(FakeMessage(None, calls), "tool_calls"), (FakeMessage("done"), "stop")],
            manager,
            registry,
        )
        messages = [{"role": "user", "content": "run"}]

        with redirect_stdout(io.StringIO()):
            agent.run(messages)

        tool_message = next(message for message in messages if message["role"] == "tool")
        self.assertEqual(tool_message["content"], "unsafe output")


if __name__ == "__main__":
    unittest.main()
