from internal.Agent.config import get_config
from internal.Agent.base_agent import Agent
from internal.Agent.llm_config import client
from internal.Agent.tools import default_registry, setup_delegate
from internal.Agent.system import get_system_prompt
from internal.Agent.conversation_log import SessionLogger

# 延迟初始化子代理（避免循环导入）
setup_delegate()


# ============================================================
# CLI 入口
# ============================================================

if __name__ == '__main__':
    cfg = get_config()
    session_log = SessionLogger(level=cfg.log.level)

    parent_agent = Agent(
        client=client,
        registry=default_registry,
        tools=default_registry.get_openai_tools(),
        session_log=session_log,
    )

    system = get_system_prompt()
    session_log.session_start(system)
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
            session_log.user_input(query)
            history.append({"role": "user", "content": query})
            parent_agent.run(history)
            response_content = history[-1]["content"]
            if isinstance(response_content, list):
                for block in response_content:
                    if hasattr(block, "text"):
                        print(block.text)
            print()
    finally:
        session_log.session_end(
            turn_count=len([m for m in history if m["role"] == "user"])
        )
        session_log.close()
