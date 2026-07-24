from internal.Agent.config import get_config
from internal.Agent.base_agent import Agent
from internal.Agent.llm_config import client
from internal.Agent.tools import default_registry, setup_delegate
from internal.Agent.system import get_system_prompt
from internal.conversation_log import init_logger, close_logger, get_logger

# 延迟初始化子代理（避免循环导入）
setup_delegate()


# ============================================================
# CLI 入口
# ============================================================

if __name__ == '__main__':
    cfg = get_config()
    init_logger(level=cfg.log.level)

    parent_agent = Agent(
        client=client,
        registry=default_registry,
        tools=default_registry.get_anthropic_tools(),
    )

    system = get_system_prompt()
    get_logger().session_start(system)
    history = [
        {"role": "system", "content": system},
    ]
    try:
        while True:
            try:
                query = input("请输入问题：")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "exit", ""):
                break
            get_logger().user_input(query)
            history.append({"role": "user", "content": query})
            parent_agent.run(history)
            print()
    finally:
        get_logger().session_end(
            turn_count=len([m for m in history if m["role"] == "user"])
        )
        close_logger()
