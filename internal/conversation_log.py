"""
项目全局结构化日志。

日志使用 loguru 的同步 sink：每个事件产生后会立即写入 JSONL 文件，并可同时
输出到终端。get_logger() 会按需初始化，因此 Agent、工具注册表或其他入口都不
需要手动传递 logger 实例。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from internal.Agent.config import get_config


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _DurableFileSink:
    """逐条 flush，并可通过 fsync 强制同步到磁盘的 Loguru sink。"""

    def __init__(self, path: Path, *, fsync: bool):
        self.path = path
        self._fsync = fsync
        self._stream = path.open("a", encoding="utf-8", buffering=1)
        self._closed = False

    def write(self, message: str) -> None:
        if not self._closed:
            self._stream.write(message)

    def flush(self) -> None:
        if self._closed:
            return
        self._stream.flush()
        if self._fsync:
            os.fsync(self._stream.fileno())

    def stop(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._stream.close()
            self._closed = True


def _resolve_log_dir(explicit_log_dir: str | Path | None, cfg) -> Path:
    """解析日志目录；默认固定在项目根目录，避免落到意外的 cwd 父目录。"""
    if explicit_log_dir is not None:
        return Path(explicit_log_dir).expanduser().resolve()

    configured_dir = Path(cfg.paths.logs_dir).expanduser()
    if configured_dir.is_absolute():
        return configured_dir.resolve()

    base_dir = (
        Path(cfg.paths.workdir).expanduser().resolve()
        if cfg.paths.workdir
        else _PROJECT_ROOT
    )
    return (base_dir / configured_dir).resolve()


def _json_formatter(record: dict) -> str:
    """将 loguru record 转换成一行稳定的 JSON。"""
    entry = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "session_id": record["extra"].get("session_id", ""),
        "event": record["extra"].get("event", "log"),
        "process_id": record["process"].id,
        "thread_id": record["thread"].id,
        "data": record["extra"].get("data", {}),
    }
    record["extra"]["_json_line"] = json.dumps(
        entry, ensure_ascii=False, default=str
    )
    return "{extra[_json_line]}\n"


def _console_formatter(record: dict) -> str:
    """终端只展示单行摘要，完整内容保留在 JSONL 文件中。"""
    data = record["extra"].get("data", {})
    summary = json.dumps(data, ensure_ascii=False, default=str)
    if len(summary) > 800:
        summary = summary[:800] + "..."
    record["extra"]["_console_data"] = summary
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{extra[event]}</cyan> | {extra[_console_data]}\n"
    )


_global_logger: SessionLogger | None = None
_logger_lock = threading.RLock()


def init_logger(
    level: str | None = None,
    *,
    log_dir: str | Path | None = None,
    console: bool | None = None,
    fsync: bool | None = None,
) -> "SessionLogger":
    """
    初始化进程级全局日志。

    重复调用是幂等的；若需要开启新会话，请先调用 close_logger()。log_dir、
    console 和 fsync 参数主要用于非 CLI 入口及测试，省略时使用项目配置。
    """
    global _global_logger
    with _logger_lock:
        if _global_logger is None:
            _global_logger = SessionLogger(
                level=level,
                log_dir=log_dir,
                console=console,
                fsync=fsync,
            )
        return _global_logger


def get_logger() -> "SessionLogger":
    """获取全局日志；首次使用时自动初始化，任何入口都不会静默丢日志。"""
    if _global_logger is None:
        return init_logger()
    return _global_logger


def close_logger() -> None:
    """立即刷盘并关闭当前全局日志。"""
    global _global_logger
    with _logger_lock:
        if _global_logger is not None:
            _global_logger.close()
            _global_logger = None


class SessionLogger:
    """线程安全的 Agent 会话日志，文件 sink 与终端 sink 共用同一 session_id。"""

    def __init__(
        self,
        level: str | None = None,
        *,
        log_dir: str | Path | None = None,
        console: bool | None = None,
        fsync: bool | None = None,
    ):
        cfg = get_config()
        self.level = (level or cfg.log.level).upper()
        self._log_cfg = cfg.log
        self._closed = False

        resolved_log_dir = _resolve_log_dir(log_dir, cfg)
        resolved_log_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        self.session_id = (
            now.strftime("session_%Y-%m-%dT%H-%M-%S-")
            + f"{now.microsecond // 1000:03d}-{os.getpid()}"
        )
        self.log_path = resolved_log_dir / f"{self.session_id}.jsonl"
        self._disk_sink = _DurableFileSink(
            self.log_path,
            fsync=(
                fsync
                if fsync is not None
                else getattr(cfg.log, "fsync", True)
            ),
        )

        # 这是应用级全局 logger。移除 loguru 自带的 stderr sink，避免每条事件重复。
        logger.remove()
        self._sink_ids = [
            logger.add(
                self._disk_sink,
                level=self.level,
                format=_json_formatter,
                enqueue=False,
                catch=True,
            )
        ]

        console_enabled = (
            console
            if console is not None
            else getattr(cfg.log, "console", True)
        )
        if console_enabled:
            self._sink_ids.append(
                logger.add(
                    sys.stderr,
                    level=self.level,
                    format=_console_formatter,
                    colorize=True,
                    enqueue=False,
                    catch=True,
                )
            )

        self._log = logger.bind(session_id=self.session_id)
        self._emit("INFO", "logger_initialized", {"log_path": str(self.log_path)})

    def close(self) -> None:
        """同步 sink 在每条消息后已刷新；此处负责最终刷盘并移除本会话 sink。"""
        if self._closed:
            return
        self._emit("INFO", "logger_closed", {})
        logger.complete()
        for sink_id in self._sink_ids:
            try:
                logger.remove(sink_id)
            except ValueError:
                pass
        self._closed = True

    def _emit(self, level: str, event: str, data: dict[str, Any]) -> None:
        if self._closed:
            return
        self._log.bind(event=event, data=data).log(level, event)

    def debug(self, event: str, details: dict[str, Any] | None = None) -> None:
        self._emit("DEBUG", event, details or {})

    def info(self, event: str, details: dict[str, Any] | None = None) -> None:
        self._emit("INFO", event, details or {})

    def warning(self, event: str, details: dict[str, Any] | None = None) -> None:
        self._emit("WARNING", event, details or {})

    def error(self, event: str, details: dict[str, Any] | None = None) -> None:
        self._emit("ERROR", event, details or {})

    def session_start(self, system_prompt_summary: str) -> None:
        self._emit("INFO", "session_started", {
            "system_prompt_length": len(system_prompt_summary),
            "system_prompt_preview": system_prompt_summary[:200],
        })

    def session_end(self, turn_count: int) -> None:
        self._emit("INFO", "session_completed", {"turn_count": turn_count})

    def user_input(self, content: str) -> None:
        self._emit("INFO", "user_input", {"content": content})

    def llm_request(
        self,
        *,
        request_id: str,
        model: str,
        message_count: int,
        tool_count: int,
    ) -> None:
        self._emit("INFO", "llm_call_started", {
            "request_id": request_id,
            "model": model,
            "message_count": message_count,
            "tool_count": tool_count,
        })

    def llm_response(
        self,
        stop_reason: str,
        model: str,
        message_count: int,
        has_tool_uses: bool,
        *,
        request_id: str = "",
        duration_ms: float | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self._emit("INFO", "llm_call_completed", {
            "request_id": request_id,
            "stop_reason": stop_reason,
            "model": model,
            "message_count": message_count,
            "has_tool_uses": has_tool_uses,
            "duration_ms": duration_ms,
            "usage": usage or {},
        })

    def llm_error(
        self,
        *,
        request_id: str,
        model: str,
        duration_ms: float,
        error: BaseException,
    ) -> None:
        self._emit("ERROR", "llm_call_failed", {
            "request_id": request_id,
            "model": model,
            "duration_ms": duration_ms,
            "error_type": type(error).__name__,
            "error": str(error),
        })

    def agent_reply(self, content: str) -> None:
        self._emit("INFO", "agent_reply", {
            "content": content[:self._log_cfg.max_reply_len],
            "content_length": len(content),
        })

    def hook_started(self, *, event: str, command: str) -> None:
        self._emit("INFO", "hook_started", {
            "hook_event": event,
            "command": command,
        })

    def hook_completed(
        self,
        *,
        event: str,
        command: str,
        duration_ms: float,
        exit_code: int,
        decision: str,
        system_message: str = "",
    ) -> None:
        self._emit("INFO", "hook_completed", {
            "hook_event": event,
            "command": command,
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "decision": decision,
            "system_message": system_message[:1000],
        })

    def hook_failed(
        self,
        *,
        event: str,
        command: str,
        duration_ms: float,
        error: str,
        exit_code: int | None,
        timed_out: bool,
    ) -> None:
        self._emit("ERROR", "hook_failed", {
            "hook_event": event,
            "command": command,
            "duration_ms": duration_ms,
            "error": error[:1000],
            "exit_code": exit_code,
            "timed_out": timed_out,
        })

    def hook_blocked(self, *, event: str, reason: str) -> None:
        self._emit("WARNING", "hook_blocked", {
            "hook_event": event,
            "reason": reason[:1000],
        })

    def tool_call(self, tool_name: str, arguments: str, call_id: str) -> None:
        common = {"tool_name": tool_name, "call_id": call_id}
        self._emit("INFO", "tool_call_started", {
            **common,
            "arguments_summary": arguments[:self._log_cfg.max_args_info],
        })
        self._emit("DEBUG", "tool_call_arguments", {
            **common,
            "arguments": arguments[:self._log_cfg.max_args_debug],
        })

    def tool_result(
        self,
        tool_name: str,
        call_id: str,
        result: str,
        *,
        duration_ms: float | None = None,
    ) -> None:
        common = {"tool_name": tool_name, "call_id": call_id}
        self._emit("INFO", "tool_call_completed", {
            **common,
            "duration_ms": duration_ms,
            "result_summary": result[:self._log_cfg.max_result_info],
            "result_length": len(result),
        })
        self._emit("DEBUG", "tool_call_result", {
            **common,
            "result": result[:self._log_cfg.max_result_debug],
        })

    def tool_error(
        self,
        tool_name: str,
        call_id: str,
        error: BaseException,
        *,
        duration_ms: float,
    ) -> None:
        self._emit("ERROR", "tool_call_failed", {
            "tool_name": tool_name,
            "call_id": call_id,
            "duration_ms": duration_ms,
            "error_type": type(error).__name__,
            "error": str(error),
        })
