"""
基于 loguru 的对话日志系统。
每次 REPL 会话生成独立的 JSONL 文件，记录完整交互链路。
"""
import json
import os
from datetime import datetime, timezone

from loguru import logger

from internal.Agent.tools.base import WORKDIR

LOG_DIR = WORKDIR / ".logs" / "sessions"

MAX_ARGS_INFO = 500
MAX_ARGS_DEBUG = 10_000
MAX_RESULT_INFO = 500
MAX_RESULT_DEBUG = 10_000
MAX_REPLY_LEN = 2000


def _json_formatter(record):
    """loguru format 函数：将 extra 中的结构化数据序列化为 JSON 行。"""
    entry = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "session_id": record["extra"].get("session_id", ""),
        "event": record["extra"].get("event", ""),
        "data": record["extra"].get("data", {}),
    }
    record["extra"]["_json_line"] = json.dumps(entry, ensure_ascii=False, default=str)
    return "{extra[_json_line]}\n"


class SessionLogger:
    """每次 REPL 会话对应一个实例，拥有独立的日志文件。"""

    def __init__(self, level: str = "DEBUG"):
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        self.session_id = (
            now.strftime("session_%Y-%m-%dT%H-%M-%S-")
            + f"{now.microsecond // 1000:03d}"
        )

        logger.remove()
        logger.add(
            LOG_DIR / f"{self.session_id}.jsonl",
            level=level.upper(),
            format=_json_formatter,
            encoding="utf-8",
        )
        self._log = logger.bind(session_id=self.session_id)

    def close(self):
        """刷盘并移除所有 sink，确保日志写入磁盘。"""
        logger.complete()
        logger.remove()

    def _emit(self, level: str, event: str, data: dict):
        self._log.bind(event=event, data=data).log(level, "")

    def session_start(self, system_prompt_summary: str):
        self._emit("INFO", "session_start", {
            "system_prompt_length": len(system_prompt_summary),
            "system_prompt_preview": system_prompt_summary[:200],
        })

    def session_end(self, turn_count: int):
        self._emit("INFO", "session_end", {"turn_count": turn_count})

    def user_input(self, content: str):
        self._emit("INFO", "user_input", {"content": content})

    def llm_response(self, finish_reason: str, model: str,
                     message_count: int, has_tool_calls: bool):
        self._emit("DEBUG", "llm_response", {
            "finish_reason": finish_reason,
            "model": model,
            "message_count": message_count,
            "has_tool_calls": has_tool_calls,
        })

    def agent_reply(self, content: str):
        self._emit("INFO", "agent_reply", {
            "content": content[:MAX_REPLY_LEN],
        })

    def tool_call(self, tool_name: str, arguments: str, call_id: str):
        self._emit("INFO", "tool_call", {
            "tool_name": tool_name,
            "call_id": call_id,
            "arguments_summary": arguments[:MAX_ARGS_INFO],
        })
        self._emit("DEBUG", "tool_call_full", {
            "tool_name": tool_name,
            "call_id": call_id,
            "arguments": arguments[:MAX_ARGS_DEBUG],
        })

    def tool_result(self, tool_name: str, call_id: str, result: str):
        self._emit("INFO", "tool_result", {
            "tool_name": tool_name,
            "call_id": call_id,
            "result_summary": result[:MAX_RESULT_INFO],
            "result_length": len(result),
        })
        self._emit("DEBUG", "tool_result_full", {
            "tool_name": tool_name,
            "call_id": call_id,
            "result": result[:MAX_RESULT_DEBUG],
        })

    def error(self, message: str, details: dict | None = None):
        data = {"message": message}
        if details:
            data.update(details)
        self._emit("ERROR", "error", data)

    def warning(self, message: str, details: dict | None = None):
        data = {"message": message}
        if details:
            data.update(details)
        self._emit("WARNING", "warning", data)


def get_log_level_from_env() -> str:
    """读取环境变量 AGENT_LOG_LEVEL，默认 DEBUG。"""
    return os.environ.get("AGENT_LOG_LEVEL", "DEBUG")
