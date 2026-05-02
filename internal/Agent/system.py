import platform
from datetime import datetime, timezone

from internal.Agent.tools.base import get_workdir
from internal.Agent.tools.skill import _get_skill_loader


def get_system_prompt() -> str:
    now_utc = datetime.now(timezone.utc)
    return (f"现在时间是：{now_utc}\n"
            f"当前系统是: {platform.system()}"
            f"你是一个在{get_workdir()}下的coding agent助手，使用任务工具来委派探索性任务或子任务。\n\n"
            f"Skills available:\n"
            f"{_get_skill_loader().get_descriptions()}")


def get_subagent_prompt() -> str:
    now_utc = datetime.now(timezone.utc)
    return (f"现在时间是：{now_utc}\n"
            f"你是一个在{get_workdir()}下的coding agent助手，完成给定任务，然后总结你的发现。")
