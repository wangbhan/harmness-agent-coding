"""
Bash 命令执行工具
在沙箱环境中执行 bash 命令，支持超时控制和三阶段权限认证。
"""
import os
import subprocess

from internal.Agent.config import get_config
from internal.Agent.auth import check_command
from internal.Agent.tools.base import BaseTool


class BashTool(BaseTool):
    name = "bash"
    description = "运行bash命令"
    param_descriptions = {"command": "bash命令"}

    def execute(self, command: str) -> str:
        """运行bash命令"""
        # ── 三阶段认证 ──────────────────────────────────────────────
        result = check_command(command)
        if not result.allowed:
            return f"命令被拒绝：{result.reason}"
        # ── 执行 ────────────────────────────────────────────────────
        cfg = get_config().tools.bash
        try:
            proc = subprocess.run(
                command, shell=True, cwd=os.getcwd(),
                capture_output=True, text=True,
                timeout=cfg.timeout, encoding=cfg.encoding,
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            return out[:cfg.max_output_len] if out else "没有输出"
        except subprocess.TimeoutExpired:
            return "命令执行超时"
        except Exception as e:
            return f"命令执行错误：{str(e)}"
