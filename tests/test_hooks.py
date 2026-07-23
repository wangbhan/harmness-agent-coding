import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from internal.Agent.config import (
    _CONFIG_DIR,
    HooksConfig,
    get_config,
    get_config_dir,
    init_config,
)
from internal.Agent.hooks import (
    HookEvent,
    HookManager,
    get_hook_manager,
    reset_hook_manager,
)


class HooksConfigTest(unittest.TestCase):
    def tearDown(self):
        reset_hook_manager()
        init_config(_CONFIG_DIR)

    def test_defaults_and_active_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "config.yaml").write_text("{}\n", encoding="utf-8")

            init_config(config_dir)

            self.assertEqual(get_config().hooks.config_path, "")
            self.assertEqual(get_config().hooks.default_timeout, 10)
            self.assertEqual(get_config().hooks.stop_max_continuations, 5)
            self.assertEqual(get_config_dir(), config_dir.resolve())

    def test_explicit_relative_hook_path_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "config.yaml").write_text(
                "hooks:\n  config_path: configs/hooks.json\n",
                encoding="utf-8",
            )

            init_config(config_dir)

            self.assertEqual(get_config().hooks.config_path, "configs/hooks.json")

    def test_timeout_must_be_positive(self):
        with self.assertRaises(ValidationError):
            HooksConfig(default_timeout=0)

    def test_stop_continuation_limit_cannot_be_negative(self):
        with self.assertRaises(ValidationError):
            HooksConfig(stop_max_continuations=-1)

    def test_disabled_hooks_do_not_eagerly_initialize_logger(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            init_config(config_dir)
            reset_hook_manager()

            with patch("internal.conversation_log.get_logger") as logger_mock:
                manager = get_hook_manager()

            self.assertFalse(manager.enabled)
            logger_mock.assert_not_called()


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_hooks(self, hooks: dict) -> HooksConfig:
        path = self.root / "hooks.json"
        path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        return HooksConfig(config_path="hooks.json", default_timeout=3)

    def manager(self, hooks: dict) -> HookManager:
        return HookManager.from_config(
            self.write_hooks(hooks),
            self.root,
            workdir=self.root,
        )

    def write_script(self, name: str, source: str) -> str:
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


class HookLoadingTest(HookTestCase):
    def test_empty_path_disables_hooks_without_reading_a_file(self):
        manager = HookManager.from_config(
            HooksConfig(), self.root, workdir=self.root
        )

        self.assertFalse(manager.enabled)
        self.assertEqual(manager.matching(HookEvent.PRE_TOOL_USE, "bash"), ())

    def test_relative_path_loads_ordered_full_match_groups(self):
        manager = self.manager({
            "PreToolUse": [
                {
                    "matcher": "bash|write",
                    "hooks": [
                        {"type": "command", "command": "first"},
                        {"type": "command", "command": "second", "timeout": 2},
                    ],
                },
                {
                    "hooks": [
                        {"type": "command", "command": "third"}
                    ]
                },
            ]
        })

        self.assertTrue(manager.enabled)
        self.assertEqual(
            [hook.command for hook in manager.matching(HookEvent.PRE_TOOL_USE, "bash")],
            ["first", "second", "third"],
        )
        self.assertEqual(
            [hook.command for hook in manager.matching(HookEvent.PRE_TOOL_USE, "bash_extra")],
            ["third"],
        )

    def test_non_tool_events_only_use_groups_without_matcher(self):
        manager = self.manager({
            "Stop": [
                {"matcher": "bash", "hooks": [{"type": "command", "command": "skip"}]},
                {"hooks": [{"type": "command", "command": "run"}]},
            ]
        })

        self.assertEqual(
            [hook.command for hook in manager.matching(HookEvent.STOP)],
            ["run"],
        )

    def test_missing_configured_file_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            HookManager.from_config(
                HooksConfig(config_path="missing.json"),
                self.root,
                workdir=self.root,
            )

    def test_invalid_json_file_is_an_error(self):
        (self.root / "hooks.json").write_text("{invalid", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "有效 JSON"):
            HookManager.from_config(
                HooksConfig(config_path="hooks.json"),
                self.root,
                workdir=self.root,
            )

    def test_absolute_config_path_is_supported(self):
        config = self.write_hooks({"Stop": []})
        absolute_config = HooksConfig(
            config_path=str((self.root / config.config_path).resolve())
        )

        manager = HookManager.from_config(
            absolute_config, self.root / "unused", workdir=self.root
        )

        self.assertFalse(manager.enabled)

    def test_invalid_hook_documents_are_rejected(self):
        invalid_documents = [
            [],
            {},
            {"hooks": []},
            {"hooks": {"Unknown": []}},
            {"hooks": {"PreToolUse": [{"matcher": "[", "hooks": []}]}},
            {"hooks": {"PreToolUse": [{"hooks": [{"type": "python", "command": "x"}]}]}},
            {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": ""}]}]}},
            {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x", "timeout": 0}]}]}},
            {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x", "on_error": "ignore"}]}]}},
        ]
        path = self.root / "hooks.json"
        for document in invalid_documents:
            with self.subTest(document=document):
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises((ValueError, TypeError)):
                    HookManager.from_config(
                        HooksConfig(config_path="hooks.json"),
                        self.root,
                        workdir=self.root,
                    )


class HookProtocolTest(HookTestCase):
    def test_pre_tool_hook_receives_payload_and_updates_input(self):
        command = self.write_script(
            "pre.py",
            """import json, sys
data = json.load(sys.stdin)
assert data["hook_event_name"] == "PreToolUse"
assert data["tool_name"] == "bash"
assert data["tool_use_id"] == "call-1"
assert data["tool_input"] == {"command": "pwd"}
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "updatedInput": {"command": "git status"},
    "additionalContext": "checked"
}}))
""",
        )
        manager = self.manager({
            "PreToolUse": [{"matcher": "bash", "hooks": [{"type": "command", "command": command}]}]
        })

        result = manager.run(HookEvent.PRE_TOOL_USE, {
            "tool_name": "bash",
            "tool_input": {"command": "pwd"},
            "tool_use_id": "call-1",
        })

        self.assertFalse(result.blocked)
        self.assertEqual(result.updated_input, {"command": "git status"})
        self.assertEqual(result.additional_context, ("checked",))

    def test_prompt_changes_are_chained_in_configuration_order(self):
        first = self.write_script(
            "first.py",
            """import json, sys
data = json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "updatedPrompt": data["prompt"] + "-first",
    "additionalContext": "one"
}}))
""",
        )
        second = self.write_script(
            "second.py",
            """import json, sys
data = json.load(sys.stdin)
assert data["prompt"].endswith("-first")
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "updatedPrompt": data["prompt"] + "-second",
    "additionalContext": "two"
}}))
""",
        )
        manager = self.manager({
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": first},
                {"type": "command", "command": second},
            ]}]
        })

        result = manager.run(HookEvent.USER_PROMPT_SUBMIT, {"prompt": "hello"})

        self.assertEqual(result.updated_prompt, "hello-first-second")
        self.assertEqual(result.additional_context, ("one", "two"))

    def test_post_tool_hook_updates_output(self):
        command = self.write_script(
            "post.py",
            """import json, sys
data = json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "updatedOutput": data["tool_output"].upper()
}}))
""",
        )
        manager = self.manager({
            "PostToolUse": [{"matcher": "read", "hooks": [{"type": "command", "command": command}]}]
        })

        result = manager.run(HookEvent.POST_TOOL_USE, {
            "tool_name": "read", "tool_input": {"path": "x"},
            "tool_use_id": "2", "tool_output": "content",
        })

        self.assertEqual(result.updated_output, "CONTENT")

    def test_block_stops_remaining_hooks(self):
        marker = self.root / "should-not-exist"
        blocker = self.write_script(
            "block.py",
            """import json
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "Stop", "decision": "block", "reason": "keep going"
}}))
""",
        )
        later = self.write_script(
            "later.py",
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        )
        manager = self.manager({
            "Stop": [{"hooks": [
                {"type": "command", "command": blocker},
                {"type": "command", "command": later},
            ]}]
        })

        result = manager.run(HookEvent.STOP, {
            "assistant_message": "done", "continuation_count": 0,
        })

        self.assertTrue(result.blocked)
        self.assertEqual(result.reason, "keep going")
        self.assertFalse(marker.exists())

    def test_exit_code_two_blocks_with_stderr_reason(self):
        command = self.write_script(
            "deny.py", "import sys\nsys.stderr.write('denied')\nsys.exit(2)\n"
        )
        manager = self.manager({
            "PreToolUse": [{"hooks": [{"type": "command", "command": command}]}]
        })

        result = manager.run(HookEvent.PRE_TOOL_USE, {
            "tool_name": "bash", "tool_input": {}, "tool_use_id": "3",
        })

        self.assertTrue(result.blocked)
        self.assertEqual(result.reason, "denied")

    def test_exit_code_two_without_stderr_uses_fallback_reason(self):
        command = self.write_script("deny-empty.py", "import sys\nsys.exit(2)\n")
        manager = self.manager({
            "Stop": [{"hooks": [{"type": "command", "command": command}]}]
        })

        result = manager.run(HookEvent.STOP, {
            "assistant_message": "done", "continuation_count": 0,
        })

        self.assertTrue(result.blocked)
        self.assertTrue(result.reason)

    def test_exit_code_two_blocks_even_with_invalid_utf8_output(self):
        sources = [
            "import sys\nsys.stderr.buffer.write(b'\\xff')\nsys.exit(2)\n",
            "import sys\nsys.stdout.buffer.write(b'\\xff')\nsys.exit(2)\n",
        ]
        for index, source in enumerate(sources):
            command = self.write_script(f"deny-bytes-{index}.py", source)
            manager = self.manager({
                "PreToolUse": [{"hooks": [{
                    "type": "command", "command": command,
                    "on_error": "allow",
                }]}]
            })

            result = manager.run(HookEvent.PRE_TOOL_USE, {
                "tool_name": "bash", "tool_input": {}, "tool_use_id": "bytes",
            })

            with self.subTest(index=index):
                self.assertTrue(result.blocked)
                self.assertTrue(result.reason)

    def test_process_launch_error_honors_block_policy(self):
        config = self.write_hooks({
            "Stop": [{"hooks": [{
                "type": "command", "command": "ignored", "on_error": "block"
            }]}]
        })
        missing_workdir = self.root / "removed-workdir"
        missing_workdir.mkdir()
        manager = HookManager.from_config(
            config, self.root, workdir=missing_workdir
        )
        missing_workdir.rmdir()

        result = manager.run(HookEvent.STOP, {
            "assistant_message": "done", "continuation_count": 0,
        })

        self.assertTrue(result.blocked)
        self.assertIn("process failed", result.reason.lower())

    def test_event_specific_json_block_decisions_are_honored(self):
        cases = [
            (
                HookEvent.USER_PROMPT_SUBMIT,
                {"prompt": "hello"},
                {"decision": "block", "reason": "prompt denied"},
            ),
            (
                HookEvent.PRE_TOOL_USE,
                {"tool_name": "bash", "tool_input": {}, "tool_use_id": "1"},
                {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "tool denied",
                },
            ),
            (
                HookEvent.POST_TOOL_USE,
                {
                    "tool_name": "bash", "tool_input": {},
                    "tool_use_id": "1", "tool_output": "unsafe",
                },
                {"decision": "block", "reason": "output denied"},
            ),
        ]
        for index, (event, payload, specific) in enumerate(cases):
            output = {
                "hookSpecificOutput": {
                    "hookEventName": event.value,
                    **specific,
                }
            }
            command = self.write_script(
                f"decision-{index}.py",
                f"import json\nprint(json.dumps({output!r}))\n",
            )
            manager = self.manager({
                event.value: [{"hooks": [{"type": "command", "command": command}]}]
            })

            with self.subTest(event=event.value):
                self.assertTrue(manager.run(event, payload).blocked)

    def test_nonzero_exit_uses_fail_open_by_default(self):
        command = self.write_script(
            "failure.py", "import sys\nsys.stderr.write('failed')\nsys.exit(1)\n"
        )
        manager = self.manager({
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]
        })

        result = manager.run(HookEvent.USER_PROMPT_SUBMIT, {"prompt": "hello"})

        self.assertFalse(result.blocked)

    def test_runtime_errors_follow_on_error_policy(self):
        invalid_json = self.write_script("invalid.py", "print('not-json')\n")
        for policy, blocked in (("allow", False), ("block", True)):
            with self.subTest(policy=policy):
                manager = self.manager({
                    "UserPromptSubmit": [{"hooks": [{
                        "type": "command", "command": invalid_json,
                        "on_error": policy,
                    }]}]
                })
                result = manager.run(HookEvent.USER_PROMPT_SUBMIT, {"prompt": "hi"})
                self.assertEqual(result.blocked, blocked)

    def test_invalid_utf8_output_follows_on_error_policy(self):
        invalid_utf8 = self.write_script(
            "invalid-utf8.py",
            "import sys\nsys.stdout.buffer.write(b'\\xff')\n",
        )
        for policy, blocked in (("allow", False), ("block", True)):
            with self.subTest(policy=policy):
                manager = self.manager({
                    "UserPromptSubmit": [{"hooks": [{
                        "type": "command", "command": invalid_utf8,
                        "on_error": policy,
                    }]}]
                })

                result = manager.run(
                    HookEvent.USER_PROMPT_SUBMIT, {"prompt": "你好"}
                )

                self.assertEqual(result.blocked, blocked)

    def test_command_protocol_uses_utf8_for_unicode_input_and_output(self):
        command = self.write_script(
            "unicode.py",
            """import json, sys
data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
assert data["prompt"] == "你好，世界"
output = {"hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "updatedPrompt": data["prompt"] + "！"
}}
sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))
""",
        )
        manager = self.manager({
            "UserPromptSubmit": [{"hooks": [{
                "type": "command", "command": command, "on_error": "block"
            }]}]
        })

        result = manager.run(
            HookEvent.USER_PROMPT_SUBMIT, {"prompt": "你好，世界"}
        )

        self.assertFalse(result.blocked)
        self.assertEqual(result.updated_prompt, "你好，世界！")

    def test_timeout_follows_block_policy(self):
        sleeper = self.write_script("slow.py", "import time\ntime.sleep(2)\n")
        path = self.root / "hooks.json"
        path.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{
                "type": "command", "command": sleeper,
                "timeout": 1, "on_error": "block",
            }]}]
        }}), encoding="utf-8")
        manager = HookManager.from_config(
            HooksConfig(config_path="hooks.json"), self.root, workdir=self.root
        )

        result = manager.run(HookEvent.STOP, {
            "assistant_message": "done", "continuation_count": 0,
        })

        self.assertTrue(result.blocked)
        self.assertIn("timed out", result.reason.lower())

    @unittest.skipIf(os.name == "nt", "POSIX process-group semantics")
    def test_timeout_terminates_hook_child_processes(self):
        marker = self.root / "child-finished"
        child = self.root / "child.py"
        child.write_text(
            """import sys, time
from pathlib import Path
time.sleep(1.2)
Path(sys.argv[1]).write_text("finished", encoding="utf-8")
""",
            encoding="utf-8",
        )
        parent = self.root / "parent.py"
        parent.write_text(
            """import subprocess, sys, time
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
time.sleep(10)
""",
            encoding="utf-8",
        )
        command = " ".join([
            shlex.quote(sys.executable),
            shlex.quote(str(parent)),
            shlex.quote(str(child)),
            shlex.quote(str(marker)),
        ])
        path = self.root / "hooks.json"
        path.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{
                "type": "command", "command": command,
                "timeout": 1, "on_error": "block",
            }]}]
        }}), encoding="utf-8")
        manager = HookManager.from_config(
            HooksConfig(config_path="hooks.json"), self.root, workdir=self.root
        )

        result = manager.run(HookEvent.STOP, {
            "assistant_message": "done", "continuation_count": 0,
        })
        time.sleep(0.5)

        self.assertTrue(result.blocked)
        self.assertFalse(marker.exists())

    def test_mismatched_event_and_wrong_known_field_types_are_errors(self):
        bad_outputs = [
            {"hookSpecificOutput": {"hookEventName": "Stop"}},
            {"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "updatedPrompt": 3
            }},
        ]
        for index, output in enumerate(bad_outputs):
            command = self.write_script(
                f"bad-{index}.py", f"import json\nprint(json.dumps({output!r}))\n"
            )
            manager = self.manager({
                "UserPromptSubmit": [{"hooks": [{
                    "type": "command", "command": command, "on_error": "block"
                }]}]
            })
            with self.subTest(output=output):
                self.assertTrue(
                    manager.run(HookEvent.USER_PROMPT_SUBMIT, {"prompt": "x"}).blocked
                )


if __name__ == "__main__":
    unittest.main()
