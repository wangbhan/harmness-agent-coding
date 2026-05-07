"""
Bash 命令执行工具
在沙箱环境中执行 bash 命令，支持超时控制和危险命令拦截。
"""
import os
import subprocess

from internal.Agent.config import get_config
from internal.Agent.tools.base import BaseTool


class BashTool(BaseTool):
    name = "bash"
    description = "运行bash命令"
    param_descriptions = {"command": "bash命令"}

    def execute(self, command: str) -> str:
        """运行bash命令"""
        cfg = get_config().tools.bash
        if any(cmd in command for cmd in cfg.dangerous_commands):
            return "请勿执行危险命令"
        try:
            result = subprocess.run(
                command, shell=True, cwd=os.getcwd(),
                capture_output=True, text=True,
                timeout=cfg.timeout, encoding=cfg.encoding,
            )
            out = ((result.stdout or "") + (result.stderr or "")).strip()
            return out[:cfg.max_output_len] if out else "没有输出"
        except subprocess.TimeoutExpired:
            return "命令执行超时"
        except Exception as e:
            return f"命令执行错误：{str(e)}"
