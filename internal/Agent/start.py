from internal.Agent.base_agent import Agent
from internal.Agent.llm_config import client
from internal.Agent.tools import default_registry
from internal.Agent.system import system

# 构建父 agent（基础工具 + delegate）
parent_agent = Agent(
    client=client,
    registry=default_registry,
    tools=default_registry.get_openai_tools(),
)


# ============================================================
# CLI 入口
# ============================================================

if __name__ == '__main__':
    history = [
        {"role": "system", "content": system},
    ]
    while True:
        try:
            query = input("请输入问题：")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        parent_agent.run(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
