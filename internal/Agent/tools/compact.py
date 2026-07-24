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

TRANSCRIPT_DIR = None
_TOOL_RESULT_PATH = None


def _ensure_transcript_dir():
    global TRANSCRIPT_DIR
    if TRANSCRIPT_DIR is None:
        cfg = get_config().paths
        TRANSCRIPT_DIR = get_workdir() / cfg.transcripts_dir


def _get_tool_result_path():
    global _TOOL_RESULT_PATH
    if _TOOL_RESULT_PATH is None:
        _TOOL_RESULT_PATH = get_workdir() / ".tool_task" / "tool_result"
    return _TOOL_RESULT_PATH


def _cfg():
    return get_config().compact

def _message_has_tool_use(msg: dict) -> bool:
    """判断message数组中是否存在调用tool的情况"""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content", [])
    if not content:
        return False
    return any(content_item.get("type") == "tool_use" for content_item in content)

def _is_tool_result_message(msg: dict):
    """判断当前的message数组中是否是tool_result的情况"""
    if msg.get("role") != "user":
        return False
    content = msg.get("content", [])
    if not content:
        return False
    return any(content_item.get("type") == "tool_result" for content_item in content)

def _persist_large_output(tool_use_id, output: str) -> str:
    """用于判断将大文件输出进行落盘并优化tool_result的输出"""
    path_dir = _get_tool_result_path()
    path_dir.mkdir(exist_ok=True, parents=True)
    path = path_dir / f"{tool_use_id}.txt"
    if not path.exists(): path.write_text(output)
    return f"<persisted-output>\n全部输出已保存至: {path}\nPreview: {output[:2000]}...\n</persisted_output>"


def snip_compact(messages: list, max_message: int = 50):
    """将message保留50条(头3条，尾47条)，但是要保证tool_use和tool_result成对出现"""
    if len(messages) <= max_message:
        return messages
    head_end, tail_start = 3, len(messages) - (max_message - 3)

    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1

    while tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
        tail_start += 1

    if head_end > tail_start:
        return messages
    return messages[:head_end] + [{"role": "user", "content": f"[snipped {tail_start - head_end} msgs]"}] + messages[tail_start:]


def tool_result_budget(messages: list) -> list:
    """计算tool_result的占用字符数，超过 budget_max 时将大条目落盘"""
    cfg = _cfg()
    last = messages[-1]
    if not last.get("role") == "user" or not isinstance(last.get("content"), list):
        return messages

    blocks = [(i, b) for i, b in enumerate(last.get("content")) if b.get("type") == "tool_result"]
    total = sum(len(str(b["content"])) for i, b in blocks)

    if total < cfg.budget_max:
        return messages

    for i, b in blocks:
        if total < cfg.budget_max:
            break
        content = str(b["content"])
        if len(content) < cfg.persist_threshold:
            continue
        tid = b.get("tool_use_id", "unknown")
        b["content"] = _persist_large_output(tid, content)
        total = sum(len(str(b["content"])) for i, b in blocks)
    return messages


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
