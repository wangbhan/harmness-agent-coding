"""
工具包入口 - 显式注册所有工具到 default_registry
"""
from internal.Agent.tools.background import BgTool
from internal.Agent.tools.registry import default_registry, ToolRegistry
from internal.Agent.tools.base import safe_path, _get_file_encoding, get_workdir

# 导入具体工具类
from internal.Agent.tools.bash import BashTool
from internal.Agent.tools.read import ReadTool
from internal.Agent.tools.write import WriteTool
from internal.Agent.tools.edit import EditTool
from internal.Agent.tools.task import TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool
from internal.Agent.tools.skill import SkillTool
from internal.Agent.tools.sub_agent import delegate_tool
from internal.Agent.tools.compact import compact_tool

# ============================================================
# 注册基础工具
# ============================================================

default_registry.register(BashTool())
default_registry.register(ReadTool())
default_registry.register(WriteTool())
default_registry.register(EditTool())
default_registry.register(SkillTool())
default_registry.register(BgTool())

# ============================================================
# 注册任务工具（替换原 todo 工具）
# ============================================================

default_registry.register(TaskCreateTool())
default_registry.register(TaskGetTool())
default_registry.register(TaskListTool())
default_registry.register(TaskUpdateTool())

# ============================================================
# 注册 compact 工具
# ============================================================

default_registry.register(compact_tool)

# ============================================================
# 注册 delegate 工具 + 绑定子 Agent
# ============================================================

default_registry.register(delegate_tool)

def setup_delegate():
    """延迟初始化子代理，避免模块加载时的循环导入"""
    from internal.Agent.base_agent import Agent
    from internal.Agent.llm_config import client

    sub_agent = Agent(
        client=client,
        registry=default_registry,
        tools=default_registry.get_anthropic_tools(exclude={"delegate"}),
        process_user_prompts=False,
    )
    delegate_tool.bind(sub_agent)
