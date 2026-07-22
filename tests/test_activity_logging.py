import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from internal.Agent.base_agent import Agent
from internal.conversation_log import close_logger, init_logger
from internal.Agent.tools.base import BaseTool
from internal.Agent.tools.registry import ToolRegistry


class _EchoTool(BaseTool):
    name = "echo_for_test"

    def execute(self, text: str) -> str:
        """返回输入文本。"""
        time.sleep(0.01)
        return text


class _FakeMessage:
    content = "done"
    tool_calls = None

    def model_dump(self, **_kwargs):
        return {"role": "assistant", "content": self.content}


class _FakeCompletions:
    def create(self, **_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=_FakeMessage(), finish_reason="stop")],
            usage=SimpleNamespace(
                model_dump=lambda **_kwargs: {
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                }
            ),
        )


class ActivityLoggingTest(unittest.TestCase):
    def setUp(self):
        close_logger()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.activity_log = init_logger(
            level="DEBUG",
            log_dir=self.temp_dir.name,
            console=False,
        )

    def tearDown(self):
        close_logger()
        self.temp_dir.cleanup()

    def _events_on_disk(self) -> list[dict]:
        # 特意在 close_logger() 之前读取，验证每条日志都是实时刷盘的。
        return [
            json.loads(line)
            for line in self.activity_log.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_tool_call_is_logged_immediately_with_duration(self):
        registry = ToolRegistry()
        registry.register(_EchoTool())

        result = registry.call(
            "echo_for_test",
            '{"text": "hello"}',
            call_id="call_test",
        )

        self.assertEqual(result, "hello")
        events = self._events_on_disk()
        event_names = [entry["event"] for entry in events]
        self.assertIn("tool_call_started", event_names)
        self.assertIn("tool_call_completed", event_names)
        completed = next(
            entry for entry in events if entry["event"] == "tool_call_completed"
        )
        self.assertEqual(completed["data"]["call_id"], "call_test")
        self.assertGreaterEqual(completed["data"]["duration_ms"], 10)

    def test_tool_failure_is_logged_before_exception_is_raised(self):
        registry = ToolRegistry()
        registry.register(_EchoTool())

        with self.assertRaises(json.JSONDecodeError):
            registry.call("echo_for_test", "not-json", call_id="call_bad_json")

        failed = [
            entry
            for entry in self._events_on_disk()
            if entry["event"] == "tool_call_failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["data"]["call_id"], "call_bad_json")
        self.assertEqual(failed[0]["data"]["error_type"], "JSONDecodeError")

    def test_llm_call_lifecycle_is_logged(self):
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeCompletions())
        )
        agent = Agent(
            client=client,
            registry=ToolRegistry(),
            tools=[],
            model="fake-model",
        )
        messages = [{"role": "user", "content": "hello"}]

        with redirect_stdout(io.StringIO()):
            agent.run(messages)

        events = self._events_on_disk()
        started = next(entry for entry in events if entry["event"] == "llm_call_started")
        completed = next(
            entry for entry in events if entry["event"] == "llm_call_completed"
        )
        self.assertEqual(
            started["data"]["request_id"], completed["data"]["request_id"]
        )
        self.assertEqual(completed["data"]["model"], "fake-model")
        self.assertEqual(completed["data"]["usage"]["completion_tokens"], 1)


if __name__ == "__main__":
    unittest.main()
