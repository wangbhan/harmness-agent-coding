import platform
from pathlib import Path
from datetime import datetime, timezone

from internal.Agent.tools.base_tools import WORKDIR
from internal.Agent.tools.skill import skill_loader

# 时间采用utc时间
now_utc = datetime.now(timezone.utc)

system = (f"现在时间是：{now_utc}\n"
          f"当前系统是: {platform.system()}"
          f"你是一个在{WORKDIR}下的coding agent助手，使用任务工具来委派探索性任务或子任务。\n\n"
          f"Skills available:\n"
          f"{skill_loader.get_descriptions()}")

subagent_system = (f"现在时间是：{now_utc}\n"
                   f"你是一个在{WORKDIR}下的coding agent助手，完成给定任务，然后总结你的发现。")