import io
import json
import shlex
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from internal.Agent.base_agent import Agent
from internal.Agent.config import HooksConfig
from internal.Agent.hooks import HookEvent, HookManager
from internal.conversation_log import (
    _PROJECT_ROOT,
    _resolve_log_dir,
    close_logger,
    init_logger,
)
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
            fsync=True,
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

    def test_log_file_remains_readable_after_logger_is_closed(self):
        log_path = self.activity_log.log_path
        self.activity_log.info("persistence_test", {"persisted": True})

        close_logger()

        self.assertTrue(log_path.is_file())
        events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        event_names = [entry["event"] for entry in events]
        self.assertIn("persistence_test", event_names)
        self.assertIn("logger_closed", event_names)

    def test_each_event_can_be_fsynced(self):
        with patch("internal.conversation_log.os.fsync") as fsync_mock:
            self.activity_log.info("fsync_test", {})

        fsync_mock.assert_called_once()

    def test_default_relative_log_dir_uses_project_root(self):
        cfg = SimpleNamespace(
            paths=SimpleNamespace(workdir="", logs_dir="logs/sessions")
        )

        resolved = _resolve_log_dir(None, cfg)

        self.assertEqual(resolved, _PROJECT_ROOT / "logs/sessions")

    def test_hook_lifecycle_is_logged_without_sensitive_payloads(self):
        root = Path(self.temp_dir.name)

        def manager_for(name, source, *, on_error="allow"):
            script = root / f"{name}.py"
            script.write_text(source, encoding="utf-8")
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
            config_path = root / f"{name}.json"
            config_path.write_text(json.dumps({"hooks": {
                "UserPromptSubmit": [{"hooks": [{
                    "type": "command",
                    "command": command,
                    "on_error": on_error,
                }]}]
            }}), encoding="utf-8")
            return HookManager.from_config(
                HooksConfig(config_path=config_path.name),
                root,
                workdir=root,
                logger=self.activity_log,
            )

        success = manager_for(
            "success",
            """import json
print(json.dumps({
    "systemMessage": "operator diagnostic",
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "updatedPrompt": "STDOUT_SECRET"
    }
}))
""",
        )
        blocked = manager_for(
            "blocked",
            "import sys\nsys.stderr.write('policy denied')\nsys.exit(2)\n",
        )
        failed = manager_for("failed", "print('invalid-json')\n")

        success.run(HookEvent.USER_PROMPT_SUBMIT, {"prompt": "PROMPT_SECRET"})
        blocked.run(HookEvent.USER_PROMPT_SUBMIT, {"prompt": "PROMPT_SECRET"})
        failed.run(HookEvent.USER_PROMPT_SUBMIT, {"prompt": "PROMPT_SECRET"})

        hook_events = [
            entry for entry in self._events_on_disk()
            if entry["event"].startswith("hook_")
        ]
        event_names = [entry["event"] for entry in hook_events]
        self.assertIn("hook_started", event_names)
        self.assertIn("hook_completed", event_names)
        self.assertIn("hook_blocked", event_names)
        self.assertIn("hook_failed", event_names)
        completed = next(
            entry for entry in hook_events
            if entry["event"] == "hook_completed"
            and entry["data"]["decision"] == "allow"
        )
        self.assertEqual(completed["data"]["system_message"], "operator diagnostic")

        serialized = json.dumps(hook_events, ensure_ascii=False)
        self.assertNotIn("PROMPT_SECRET", serialized)
        self.assertNotIn("STDOUT_SECRET", serialized)


if __name__ == "__main__":
    unittest.main()
