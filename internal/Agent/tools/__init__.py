"""
工具包入口 - 显式注册所有工具到 default_registry
"""
from internal.Agent.tools.registry import default_registry, ToolRegistry
from internal.Agent.tools.base import BaseTool, safe_path, _get_file_encoding, WORKDIR

# 导入具体工具类
from internal.Agent.tools.bash import BashTool
from internal.Agent.tools.read import ReadTool
from internal.Agent.tools.write import WriteTool
from internal.Agent.tools.edit import EditTool
from internal.Agent.tools.todo import TodoTool
from internal.Agent.tools.skill import SkillTool
from internal.Agent.tools.sub_agent import delegate_tool

# ============================================================
# 注册基础工具
# ============================================================

default_registry.register(BashTool())
default_registry.register(ReadTool())
default_registry.register(WriteTool())
default_registry.register(EditTool())
default_registry.register(TodoTool())
default_registry.register(SkillTool())

# ============================================================
# 注册 delegate 工具 + 绑定子 Agent
# ============================================================

default_registry.register(delegate_tool)

from internal.Agent.base_agent import Agent
from internal.Agent.llm_config import client

sub_agent = Agent(
    client=client,
    registry=default_registry,
    tools=default_registry.get_openai_tools(exclude={"delegate"}),
)
delegate_tool.bind(sub_agent)
