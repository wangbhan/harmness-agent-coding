"""
Bash 命令执行工具
在沙箱环境中执行 bash 命令，支持超时控制和危险命令拦截。
"""
import os
import subprocess

from internal.Agent.tools.base import BaseTool


class BashTool(BaseTool):
    name = "bash"
    description = "运行bash命令"
    param_descriptions = {"command": "bash命令"}

    def execute(self, command: str) -> str:
        """运行bash命令"""
        dangerous_commands = ["rm -rf /", "sudo", "reboot", "shutdown"]
        if any(cmd in command for cmd in dangerous_commands):
            return "请勿执行危险命令"
        try:
            result = subprocess.run(command, shell=True, cwd=os.getcwd(), capture_output=True, text=True, timeout=120, encoding="utf-8")
            out = (result.stdout + result.stderr).strip()
            return out[:5000] if out else "没有输出"
        except subprocess.TimeoutExpired:
            return "命令执行超时"
        except Exception as e:
            return f"命令执行错误：{str(e)}"
