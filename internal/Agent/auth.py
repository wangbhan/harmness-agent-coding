"""
三阶段 Bash 命令权限认证
Phase 1 - 拒绝列表：字符串匹配，直接拒绝
Phase 2 - 规则匹配：正则匹配，风险命令进入人工审批
Phase 3 - 用户审批：加锁后通过 input() 等待用户 y/N 决定
"""
import re
import sys
import threading
from dataclasses import dataclass

from internal.Agent.config import get_config

# ── 模块级全局锁，串行化 Phase 3 的终端 I/O ──────────────────────────────────
_approval_lock = threading.Lock()


@dataclass
class AuthResult:
    """认证结果"""
    allowed: bool
    reason: str  # 拒绝时说明原因（含阶段信息），放行时为 ""


# ── 正则缓存：在首次调用时编译，避免每次重新编译 ─────────────────────────────
_compiled_patterns: list[tuple[re.Pattern, str]] | None = None
_compiled_from_cfg_id: int | None = None  # 用 id(cfg) 检测配置变更


def _get_compiled_patterns(cfg) -> list[tuple[re.Pattern, str]]:
    """返回 (pattern, description) 列表，按配置缓存编译结果。"""
    global _compiled_patterns, _compiled_from_cfg_id
    cfg_id = id(cfg)
    if _compiled_patterns is None or _compiled_from_cfg_id != cfg_id:
        _compiled_patterns = [
            (re.compile(p.pattern, re.IGNORECASE), p.description)
            for p in cfg.review_patterns
        ]
        _compiled_from_cfg_id = cfg_id
    return _compiled_patterns


# ── Phase 3 终端 UI ───────────────────────────────────────────────────────────

_BORDER = "─" * 60


def _print_approval_prompt(command: str, reason: str) -> None:
    """在终端打印审批提示框（在锁内调用，无竞争）。"""
    print(f"\n{_BORDER}", file=sys.stderr)
    print("  [需要审批] Agent 请求执行以下命令", file=sys.stderr)
    print(_BORDER, file=sys.stderr)
    print(f"  命令   : {command}", file=sys.stderr)
    print(f"  原因   : {reason}", file=sys.stderr)
    print(_BORDER, file=sys.stderr)


# ── 核心入口 ──────────────────────────────────────────────────────────────────

def check_command(command: str) -> AuthResult:
    """
    对命令执行三阶段认证，返回 AuthResult。
    可在任意线程中安全调用。
    """
    from internal.conversation_log import get_logger  # 懒加载避免循环依赖
    cfg = get_config().tools.bash

    # ── Phase 1: 拒绝列表（字符串子串匹配，保持向后兼容）─────────────────────
    for blocked in cfg.dangerous_commands:
        if blocked in command:
            get_logger().warning("command_denied", {"phase": 1, "command": command, "keyword": blocked})
            return AuthResult(
                allowed=False,
                reason=f"[Phase 1] 命令包含禁止关键字：{blocked!r}",
            )

    # ── Phase 2: 规则匹配（正则）─────────────────────────────────────────────
    matched_reason: str | None = None
    for pattern, description in _get_compiled_patterns(cfg):
        if pattern.search(command):
            matched_reason = description
            break

    if matched_reason is None:
        # 未匹配任何风险规则，直接放行
        return AuthResult(allowed=True, reason="")

    # ── Phase 3: 用户审批（串行化终端 I/O）───────────────────────────────────
    with _approval_lock:
        _print_approval_prompt(command, matched_reason)
        try:
            answer = input("  是否允许执行？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # 非交互环境或用户强制中断，保守拒绝
            print("\n  [审批] 输入中断，默认拒绝。", file=sys.stderr)
            print(_BORDER + "\n", file=sys.stderr)
            return AuthResult(
                allowed=False,
                reason=f"[Phase 3] 用户中断审批，拒绝：{matched_reason}",
            )

        if answer == "y":
            print(f"  [审批] 已批准。", file=sys.stderr)
            print(_BORDER + "\n", file=sys.stderr)
            get_logger().warning("command_approved", {"phase": 3, "command": command, "reason": matched_reason})
            return AuthResult(allowed=True, reason="")
        else:
            print(f"  [审批] 已拒绝。", file=sys.stderr)
            print(_BORDER + "\n", file=sys.stderr)
            get_logger().warning("command_denied", {"phase": 3, "command": command, "reason": matched_reason})
            return AuthResult(
                allowed=False,
                reason=f"[Phase 3] 用户拒绝：{matched_reason}",
            )
