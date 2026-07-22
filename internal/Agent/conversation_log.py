"""
基于 loguru 的对话日志系统。
每次 REPL 会话生成独立的 JSONL 文件，记录完整交互链路。
通过 init_logger() / get_logger() 实现全局单例，任意模块可随时打印日志。
"""
import json
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from internal.Agent.config import get_config
from internal.Agent.tools.base import get_workdir


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


class _NullLogger:
    """未初始化时的空操作 Logger，避免调用方需要守卫。"""
    def session_start(self, *a, **kw): pass
    def session_end(self, *a, **kw): pass
    def user_input(self, *a, **kw): pass
    def llm_response(self, *a, **kw): pass
    def agent_reply(self, *a, **kw): pass
    def tool_call(self, *a, **kw): pass
    def tool_result(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def warning(self, *a, **kw): pass


_null_logger = _NullLogger()
_global_logger: Optional["SessionLogger"] = None


def init_logger(level: str = None) -> "SessionLogger":
    """初始化全局日志，在 start.py 启动时调用一次。"""
    global _global_logger
    _global_logger = SessionLogger(level=level)
    return _global_logger


def get_logger() -> "SessionLogger | _NullLogger":
    """获取全局 Logger，未初始化时返回 _NullLogger（静默忽略所有调用）。"""
    return _global_logger if _global_logger is not None else _null_logger


def close_logger():
    """刷盘并清理全局 Logger，在 start.py finally 块中调用。"""
    global _global_logger
    if _global_logger is not None:
        _global_logger.close()
        _global_logger = None


class SessionLogger:
    """每次 REPL 会话对应一个实例，拥有独立的日志文件。"""

    def __init__(self, level: str = None):
        cfg = get_config()
        log_cfg = cfg.log
        paths_cfg = cfg.paths
        self.level = level or log_cfg.level
        self._log_cfg = log_cfg

        log_dir = get_workdir() / paths_cfg.logs_dir
        log_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        self.session_id = (
            now.strftime("session_%Y-%m-%dT%H-%M-%S-")
            + f"{now.microsecond // 1000:03d}"
        )

        logger.remove()
        logger.add(
            log_dir / f"{self.session_id}.jsonl",
            level=self.level.upper(),
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
            "content": content[:self._log_cfg.max_reply_len],
        })

    def tool_call(self, tool_name: str, arguments: str, call_id: str):
        self._emit("INFO", "tool_call", {
            "tool_name": tool_name,
            "call_id": call_id,
            "arguments_summary": arguments[:self._log_cfg.max_args_info],
        })
        self._emit("DEBUG", "tool_call_full", {
            "tool_name": tool_name,
            "call_id": call_id,
            "arguments": arguments[:self._log_cfg.max_args_debug],
        })

    def tool_result(self, tool_name: str, call_id: str, result: str):
        self._emit("INFO", "tool_result", {
            "tool_name": tool_name,
            "call_id": call_id,
            "result_summary": result[:self._log_cfg.max_result_info],
            "result_length": len(result),
        })
        self._emit("DEBUG", "tool_result_full", {
            "tool_name": tool_name,
            "call_id": call_id,
            "result": result[:self._log_cfg.max_result_debug],
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
