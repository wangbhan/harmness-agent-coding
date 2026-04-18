import os
import subprocess

from internal.Agent.tools.base_tools import tool


@tool
def run_bash(command: str) -> str:
    """
    运行bash命令
    :param command: bash命令
    :return: 命令输出
    """
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