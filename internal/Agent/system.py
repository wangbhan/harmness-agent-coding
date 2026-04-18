from pathlib import Path
from datetime import datetime, timezone

WORKDIR = Path.cwd()

# 时间采用utc时间
now_utc = datetime.now(timezone.utc)

system = (f"现在时间是：{now_utc}\n"
          f"你是一个在{WORKDIR}下的coding agent助手，使用任务工具来委派探索性任务或子任务。")

subagent_system = (f"现在时间是：{now_utc}\n"
                   f"你是一个在{WORKDIR}下的coding agent助手，完成给定任务，然后总结你的发现。")