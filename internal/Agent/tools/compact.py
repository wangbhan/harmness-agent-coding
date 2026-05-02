"""
压缩策略：
    1. micro_compact: 将执行过的tool_result替换为占位符，保留近3轮调用历史，
       短结果和特定工具结果不做占位处理

    2. auto_compact: 将对话保存到磁盘后调用LLM生成摘要，返回压缩后的上下文

    3. CompactTool: 注册为工具供LLM调用，当上下文过长时LLM自行决定压缩时机
"""
import json

from internal.Agent.config import get_config
from internal.Agent.tools.base import BaseTool, get_workdir, _get_file_encoding

TRANSCRIPT_DIR = None  # 由模块导入时从配置初始化


def _ensure_transcript_dir():
    global TRANSCRIPT_DIR
    if TRANSCRIPT_DIR is None:
        cfg = get_config().paths
        TRANSCRIPT_DIR = get_workdir() / cfg.transcripts_dir


def _cfg():
    return get_config().compact


def micro_compact(messages: list):
    """将旧的tool_result变为占位符，并且保留某些读取的结果防止工具再次调用"""
    cfg = _cfg()
    tool_results = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool":
            tool_results.append((i, msg))

    if len(tool_results) <= cfg.keep_recent:
        return messages

    tool_map = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tool_call in msg.get("tool_calls", []):
                tool_map[tool_call["id"]] = tool_call["function"]["name"]

    to_clear = tool_results[:-cfg.keep_recent]
    preserve = set(cfg.preserve_result_tools)

    for i, msg in to_clear:
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= 100:
            continue
        tool_id = msg.get("tool_call_id", "")
        tool_name = tool_map.get(tool_id, "unknown")
        if tool_name in preserve:
            continue
        msg["content"] = f"[Previous: used {tool_name}]"

    return messages


def auto_compact(messages: list, client, model: str = None, topic: str = "") -> str:
    """将对话保存到磁盘，调用LLM生成摘要"""
    cfg = _cfg()
    _ensure_transcript_dir()
    model = model or cfg.model
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{topic}.json"
    with open(transcript_path, "a", encoding=_get_file_encoding()) as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[对话已经保存至： {transcript_path}]")
    conversation_text = json.dumps(messages, default=str)[-cfg.conversation_slice:]
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content":
            "请对本次对话进行总结，以确保后续工作的连贯性。总结内容应包含："
            "1) 已取得的成果；2) 当前的进展状态；3) 已做出的关键决策。 "
            "请力求简洁，但务必保留关键细节。\n\n" + conversation_text}],
        max_tokens=cfg.max_tokens,
    )
    summary = response.choices[0].message.content or "未生成摘要。"
    return f"[对话已压缩。对话保存位置： {transcript_path}]\n\n{summary}"


class CompactTool(BaseTool):
    """LLM 调用的压缩信号工具，实际压缩由 Agent 循环处理"""

    name = "compact"
    description = "压缩对话历史。当上下文过长或对话轮次过多时调用此工具，将历史对话压缩为摘要以释放上下文空间。请单独调用，不要与其他工具同时调用。"

    def execute(self) -> str:
        """请求压缩对话历史"""
        return "压缩请求已接收，正在压缩对话历史..."


compact_tool = CompactTool()
