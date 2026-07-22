"""Claude Code 风格的 Agent 生命周期命令 Hook。"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from internal.Agent.config import HooksConfig, get_config, get_config_dir


class HookEvent(str, Enum):
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"


_TOOL_EVENTS = {HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE}
_BLOCK_REASON = "Hook blocked this event"
_ERROR_LIMIT = 1000


@dataclass(frozen=True)
class CommandHook:
    command: str
    timeout: int
    on_error: Literal["allow", "block"]


@dataclass(frozen=True)
class HookGroup:
    matcher: re.Pattern[str] | None
    hooks: tuple[CommandHook, ...]


@dataclass(frozen=True)
class HookResult:
    blocked: bool = False
    reason: str = ""
    updated_prompt: str | None = None
    updated_input: dict[str, Any] | None = None
    updated_output: str | None = None
    additional_context: tuple[str, ...] = ()


class HookManager:
    """加载、匹配并串行执行命令 Hook。"""

    def __init__(
        self,
        groups: dict[HookEvent, tuple[HookGroup, ...]] | None = None,
        *,
        workdir: Path,
        stop_max_continuations: int = 5,
        logger=None,
    ):
        self._groups = groups or {}
        self.workdir = workdir.resolve()
        self.stop_max_continuations = stop_max_continuations
        self._logger = logger

    @property
    def enabled(self) -> bool:
        return any(self._groups.values())

    @classmethod
    def from_config(
        cls,
        config: HooksConfig,
        config_dir: Path,
        *,
        workdir: Path,
        logger=None,
    ) -> "HookManager":
        if not config.config_path.strip():
            return cls(
                workdir=workdir,
                stop_max_continuations=config.stop_max_continuations,
                logger=logger,
            )

        configured_path = Path(config.config_path).expanduser()
        path = (
            configured_path
            if configured_path.is_absolute()
            else config_dir.resolve() / configured_path
        ).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Hook 配置文件不存在: {path}")

        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Hook 配置不是有效 JSON: {path}: {exc}") from exc
        groups = cls._parse_document(document, config.default_timeout)
        return cls(
            groups,
            workdir=workdir,
            stop_max_continuations=config.stop_max_continuations,
            logger=logger,
        )

    @staticmethod
    def _parse_document(
        document: Any, default_timeout: int
    ) -> dict[HookEvent, tuple[HookGroup, ...]]:
        if not isinstance(document, dict) or not isinstance(document.get("hooks"), dict):
            raise ValueError("Hook 配置根对象必须包含对象字段 hooks")

        raw_events = document["hooks"]
        unknown = set(raw_events) - {event.value for event in HookEvent}
        if unknown:
            raise ValueError(f"未知 Hook 事件: {', '.join(sorted(unknown))}")

        parsed: dict[HookEvent, tuple[HookGroup, ...]] = {}
        for event_name, raw_groups in raw_events.items():
            event = HookEvent(event_name)
            if not isinstance(raw_groups, list):
                raise ValueError(f"hooks.{event_name} 必须是数组")
            groups: list[HookGroup] = []
            for group_index, raw_group in enumerate(raw_groups):
                location = f"hooks.{event_name}[{group_index}]"
                if not isinstance(raw_group, dict):
                    raise ValueError(f"{location} 必须是对象")
                matcher_text = raw_group.get("matcher", "")
                if not isinstance(matcher_text, str):
                    raise ValueError(f"{location}.matcher 必须是字符串")
                try:
                    matcher = re.compile(matcher_text) if matcher_text else None
                except re.error as exc:
                    raise ValueError(f"{location}.matcher 不是有效正则: {exc}") from exc

                raw_hooks = raw_group.get("hooks")
                if not isinstance(raw_hooks, list):
                    raise ValueError(f"{location}.hooks 必须是数组")
                hooks: list[CommandHook] = []
                for hook_index, raw_hook in enumerate(raw_hooks):
                    hook_location = f"{location}.hooks[{hook_index}]"
                    if not isinstance(raw_hook, dict):
                        raise ValueError(f"{hook_location} 必须是对象")
                    if raw_hook.get("type") != "command":
                        raise ValueError(f"{hook_location}.type 仅支持 command")
                    command = raw_hook.get("command")
                    if not isinstance(command, str) or not command.strip():
                        raise ValueError(f"{hook_location}.command 必须是非空字符串")
                    timeout = raw_hook.get("timeout", default_timeout)
                    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
                        raise ValueError(f"{hook_location}.timeout 必须是正整数")
                    on_error = raw_hook.get("on_error", "allow")
                    if on_error not in {"allow", "block"}:
                        raise ValueError(f"{hook_location}.on_error 必须是 allow 或 block")
                    hooks.append(CommandHook(command, timeout, on_error))
                groups.append(HookGroup(matcher, tuple(hooks)))
            parsed[event] = tuple(groups)
        return parsed

    def matching(
        self, event: HookEvent, tool_name: str | None = None
    ) -> tuple[CommandHook, ...]:
        matched: list[CommandHook] = []
        for group in self._groups.get(event, ()):
            if event in _TOOL_EVENTS:
                if group.matcher is None or (
                    tool_name is not None and group.matcher.fullmatch(tool_name)
                ):
                    matched.extend(group.hooks)
            elif group.matcher is None:
                matched.extend(group.hooks)
        return tuple(matched)

    def run(self, event: HookEvent, payload: dict[str, Any]) -> HookResult:
        current = dict(payload)
        current["hook_event_name"] = event.value
        current["cwd"] = str(self.workdir)
        contexts: list[str] = []
        updated_prompt: str | None = None
        updated_input: dict[str, Any] | None = None
        updated_output: str | None = None

        for hook in self.matching(event, current.get("tool_name")):
            partial = self._execute(hook, event, current)
            contexts.extend(partial.additional_context)
            if partial.updated_prompt is not None:
                updated_prompt = partial.updated_prompt
                current["prompt"] = partial.updated_prompt
            if partial.updated_input is not None:
                updated_input = partial.updated_input
                current["tool_input"] = partial.updated_input
            if partial.updated_output is not None:
                updated_output = partial.updated_output
                current["tool_output"] = partial.updated_output
            if partial.blocked:
                return HookResult(
                    blocked=True,
                    reason=partial.reason,
                    updated_prompt=updated_prompt,
                    updated_input=updated_input,
                    updated_output=updated_output,
                    additional_context=tuple(contexts),
                )

        return HookResult(
            updated_prompt=updated_prompt,
            updated_input=updated_input,
            updated_output=updated_output,
            additional_context=tuple(contexts),
        )

    def _execute(
        self, hook: CommandHook, event: HookEvent, payload: dict[str, Any]
    ) -> HookResult:
        self._log("hook_started", event=event.value, command=hook.command)
        started_at = time.perf_counter()
        try:
            completed = subprocess.run(
                hook.command,
                shell=True,
                cwd=self.workdir,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=hook.timeout,
            )
        except subprocess.TimeoutExpired:
            return self._runtime_error(
                hook, event, started_at, f"Hook timed out after {hook.timeout}s"
            )
        except OSError as exc:
            return self._runtime_error(
                hook, event, started_at, f"Hook process failed: {exc}"
            )

        duration_ms = (time.perf_counter() - started_at) * 1000
        if completed.returncode == 2:
            reason = completed.stderr.strip() or _BLOCK_REASON
            self._log(
                "hook_completed", event=event.value, command=hook.command,
                duration_ms=duration_ms, exit_code=2, decision="block",
            )
            self._log("hook_blocked", event=event.value, reason=reason[:_ERROR_LIMIT])
            return HookResult(blocked=True, reason=reason)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            return self._runtime_error(hook, event, started_at, detail)
        if not completed.stdout.strip():
            self._log(
                "hook_completed", event=event.value, command=hook.command,
                duration_ms=duration_ms, exit_code=0, decision="allow",
            )
            return HookResult()

        try:
            result = self._parse_output(event, completed.stdout)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._runtime_error(hook, event, started_at, f"Invalid hook output: {exc}")

        decision = "block" if result.blocked else "allow"
        self._log(
            "hook_completed", event=event.value, command=hook.command,
            duration_ms=duration_ms, exit_code=0, decision=decision,
        )
        if result.blocked:
            self._log(
                "hook_blocked", event=event.value,
                reason=(result.reason or _BLOCK_REASON)[:_ERROR_LIMIT],
            )
        return result

    def _runtime_error(
        self,
        hook: CommandHook,
        event: HookEvent,
        started_at: float,
        detail: str,
    ) -> HookResult:
        bounded = detail[:_ERROR_LIMIT]
        self._log(
            "hook_failed", event=event.value, command=hook.command,
            duration_ms=(time.perf_counter() - started_at) * 1000,
            error=bounded,
        )
        if hook.on_error == "block":
            self._log("hook_blocked", event=event.value, reason=bounded)
            return HookResult(blocked=True, reason=bounded)
        return HookResult()

    @staticmethod
    def _parse_output(event: HookEvent, stdout: str) -> HookResult:
        document = json.loads(stdout)
        if not isinstance(document, dict):
            raise ValueError("stdout JSON 必须是对象")
        system_message = document.get("systemMessage")
        if system_message is not None and not isinstance(system_message, str):
            raise TypeError("systemMessage 必须是字符串")
        specific = document.get("hookSpecificOutput")
        if not isinstance(specific, dict):
            raise ValueError("缺少对象字段 hookSpecificOutput")
        if specific.get("hookEventName") != event.value:
            raise ValueError("hookEventName 与当前事件不一致")

        context = specific.get("additionalContext")
        if context is not None and not isinstance(context, str):
            raise TypeError("additionalContext 必须是字符串")
        contexts = (context,) if context else ()

        if event is HookEvent.USER_PROMPT_SUBMIT:
            decision = specific.get("decision", "allow")
            HookManager._require_choice("decision", decision, {"allow", "block"})
            reason = HookManager._optional_string(specific, "reason")
            prompt = HookManager._optional_string(specific, "updatedPrompt")
            return HookResult(
                blocked=decision == "block", reason=reason,
                updated_prompt=prompt, additional_context=contexts,
            )
        if event is HookEvent.PRE_TOOL_USE:
            decision = specific.get("permissionDecision", "allow")
            HookManager._require_choice(
                "permissionDecision", decision, {"allow", "deny"}
            )
            reason = HookManager._optional_string(
                specific, "permissionDecisionReason"
            )
            updated = specific.get("updatedInput")
            if updated is not None and not isinstance(updated, dict):
                raise TypeError("updatedInput 必须是对象")
            return HookResult(
                blocked=decision == "deny", reason=reason,
                updated_input=updated, additional_context=contexts,
            )
        if event is HookEvent.POST_TOOL_USE:
            decision = specific.get("decision", "allow")
            HookManager._require_choice("decision", decision, {"allow", "block"})
            reason = HookManager._optional_string(specific, "reason")
            output = HookManager._optional_string(specific, "updatedOutput")
            return HookResult(
                blocked=decision == "block", reason=reason,
                updated_output=output, additional_context=contexts,
            )

        decision = specific.get("decision", "allow")
        HookManager._require_choice("decision", decision, {"allow", "block"})
        reason = HookManager._optional_string(specific, "reason")
        return HookResult(
            blocked=decision == "block", reason=reason,
            additional_context=contexts,
        )

    @staticmethod
    def _optional_string(data: dict[str, Any], key: str) -> str | None:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{key} 必须是字符串")
        return value

    @staticmethod
    def _require_choice(key: str, value: Any, choices: set[str]) -> None:
        if value not in choices:
            raise ValueError(f"{key} 必须是 {' 或 '.join(sorted(choices))}")

    def _log(self, method: str, **details: Any) -> None:
        if self._logger is not None and hasattr(self._logger, method):
            getattr(self._logger, method)(**details)


_hook_manager: HookManager | None = None
_hook_manager_lock = threading.Lock()


def get_hook_manager() -> HookManager:
    """获取进程级 HookManager，首次使用时加载并校验 JSON 配置。"""
    global _hook_manager
    with _hook_manager_lock:
        if _hook_manager is None:
            from internal.Agent.tools.base import get_workdir
            from internal.conversation_log import get_logger

            cfg = get_config()
            _hook_manager = HookManager.from_config(
                cfg.hooks,
                get_config_dir(),
                workdir=get_workdir(),
                logger=get_logger(),
            )
        return _hook_manager


def reset_hook_manager() -> None:
    """清除进程级实例；用于配置重新加载后的测试或显式重启。"""
    global _hook_manager
    with _hook_manager_lock:
        _hook_manager = None
