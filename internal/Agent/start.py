import os
import subprocess
import json

from openai import OpenAI


key = os.environ["ZAI_API_KEY"]

client = OpenAI(
    base_url="https://api.z.ai/api/coding/paas/v4",
    api_key=key
)

system = "你是一个coding agent助手，使用bash命令来解决问题，并且无需解释，并且注意当前环境是Windows环境"

tools = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "运行bash命令",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "bash命令"
                    }
                },
                "required": ["command"]
            }
        }
    }
]

# history = [{"role": "system", "content": system},]
# while True:
#     query = input("请输入问题：")
#     history.append({"role": "user", "content": query})
#     response = client.chat.completions.create(
#         model="glm-5.1",
#         messages=history,
#         max_tokens=8000,
#         tools=tools,
#     )
#     print(response)

def run_bash(command: str) -> str:
    """
    运行bash命令
    :param command:
    :return:
    """
    dangerous_commands = ["rm -rf /", "sudo", "reboot", "shutdown"]
    if any(cmd in command for cmd in dangerous_commands):
        return "请勿执行危险命令"
    try:
        result = subprocess.run(command, shell=True, cwd=os.getcwd(), capture_output=True, text=True, timeout=120)
        out = (result.stdout + result.stderr).strip()
        return out[:5000] if out else "没有输出"
    except subprocess.TimeoutExpired:
        return "命令执行超时"
    except Exception as e:
        return f"命令执行错误：{str(e)}"

def agent_loop(messages: list[dict]):
    """
    执行agent循环
    :param messages:
    :return:
    """
    while True:
        response = client.chat.completions.create(
            model="glm-4.5",
            messages=messages,
            max_tokens=8000,
            tools=tools,
        )
        # 加入会话
        message = response.choices[0].message
        print("response:", response)
        messages.append({"role": "assistant", "content": message.content})


        finish_reason = response.choices[0].finish_reason
        # 不使用工具
        if finish_reason == "stop":
            print("回复：", message.content)
            messages.append({
                "role": "assistant",
                "content": message.content
            })
            return
        # 需要调用工具
        for block in message.tool_calls:
            if block.type == "function":
                output = run_bash(json.loads(block.function.arguments)['command'])
                print("output:", output[:200])
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.id,
                    "content": output
                })



if __name__ == '__main__':
    history = [
        {"role": "system", "content": system},
    ]
    while True:
        try:
            # 请帮我配置一个ANTHROPIC_BASE_URL的环境变量
            query = input("请输入问题：")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()