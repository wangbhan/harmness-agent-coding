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
    """判断 assistant 消息是否包含工具调用（content 含 tool_use 块）。"""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content", [])
    if isinstance(content, list):
        return any(b.get("type") == "tool_use" for b in content)
    return False


def _is_tool_result_message(msg: dict) -> bool:
    """判断消息是否是工具结果（user 消息含 tool_result 块）。"""
    if msg.get("role") == "user":
        content = msg.get("content", [])
        if isinstance(content, list):
            return any(b.get("type") == "tool_result" for b in content)
    return False

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
    """计算 tool_result 的占用字符数，超过 budget_max 时将大条目落盘。
    同时兼容 OpenAI（role=="tool"）和 Anthropic（role=="user" 含 tool_result 块）格式。
    """
    cfg = _cfg()

    # 收集所有工具结果项：(容器对象, 内容getter, 内容setter, tool_use_id)
    # 用可变容器 + 键/索引来实现就地修改
    entries: list[tuple[dict, str]] = []  # (msg_or_block, tool_id)

    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    entries.append((block, block.get("tool_use_id", "unknown")))

    if not entries:
        return messages

    def _get_content(obj: dict) -> str:
        return str(obj.get("content", ""))

    total = sum(len(_get_content(obj)) for obj, _ in entries)
    if total < cfg.budget_max:
        return messages

    for obj, tid in entries:
        if total < cfg.budget_max:
            break
        content = _get_content(obj)
        if len(content) < cfg.persist_threshold:
            continue
        obj["content"] = _persist_large_output(tid, content)
        total = sum(len(_get_content(o)) for o, _ in entries)
    return messages


def micro_compact(messages: list):
    """将旧的 tool_result 变为占位符，保留某些读取结果防止工具再次调用。
    同时兼容 OpenAI（role=="tool"）和 Anthropic（role=="user" 含 tool_result 块）格式。
    """
    cfg = _cfg()
    preserve = set(cfg.preserve_result_tools)

    # 收集所有工具结果项：(可变对象引用, tool_id)
    # OpenAI: msg 本身；Anthropic: content 列表里的 block
    tool_results: list[tuple[dict, str]] = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    tool_results.append((block, block.get("tool_use_id", "")))

    if len(tool_results) <= cfg.keep_recent:
        return messages

    # 构建 tool_id → tool_name 映射，兼容两种格式
    tool_map: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_map[block["id"]] = block["name"]

    to_clear = tool_results[:-cfg.keep_recent]
    for obj, tool_id in to_clear:
        content = obj.get("content")
        if not isinstance(content, str) or len(content) <= 100:
            continue
        tool_name = tool_map.get(tool_id, "unknown")
        if tool_name in preserve:
            continue
        obj["content"] = f"[Previous: used {tool_name}]"

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
    response = client.messages.create(
        model=model,
        messages=[{"role": "user", "content":
            "请对本次对话进行总结，以确保后续工作的连贯性。总结内容应包含："
            "1) 已取得的成果；2) 当前的进展状态；3) 已做出的关键决策。 "
            "请力求简洁，但务必保留关键细节。\n\n" + conversation_text}],
        max_tokens=cfg.max_tokens,
    )
    summary = "".join(b.text for b in response.content if b.type == "text") or "未生成摘要。"
    return f"[对话已压缩。对话保存位置： {transcript_path}]\n\n{summary}"


class CompactTool(BaseTool):
    """LLM 调用的压缩信号工具，实际压缩由 Agent 循环处理"""

    name = "compact"
    description = "压缩对话历史。当上下文过长或对话轮次过多时调用此工具，将历史对话压缩为摘要以释放上下文空间。请单独调用，不要与其他工具同时调用。"

    def execute(self) -> str:
        """请求压缩对话历史"""
        return "压缩请求已接收，正在压缩对话历史..."


compact_tool = CompactTool()
