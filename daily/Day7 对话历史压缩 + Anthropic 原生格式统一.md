# Day 7：对话历史压缩 + Anthropic 原生格式统一

> 日期：2026-07-24
> 涉及文件：`internal/Agent/tools/compact.py`、`internal/Agent/base_agent.py`、`internal/Agent/llm_config.py`、`internal/Agent/config.py`、`internal/Agent/tools/base.py`、`internal/Agent/tools/registry.py`、`internal/Agent/tools/sub_agent.py`、`internal/Agent/tools/task.py`、`internal/Agent/tools/todo.py`、`internal/Agent/tools/__init__.py`、`internal/conversation_log.py`、`main.py`、`internal/Agent/start.py`、`config.yaml`、`pyproject.toml`、`tests/test_agent_hooks.py`、`tests/test_activity_logging.py`

## 一、本轮背景与结构变更

本轮完成两件事：

1. **对话历史压缩**：为 ReAct 循环引入三层分级压缩，控制上下文窗口增长，并在工具结果过大时落盘。
2. **Anthropic 原生格式统一**：内部消息、工具 schema、LLM 调用全部改为 Anthropic/Claude 原生格式，移除 OpenAI SDK 依赖与适配层。

```text
app/
├── pyproject.toml                              # ⬆️ 移除 openai 依赖
├── config.yaml                                 # ⬆️ LLMConfig 精简，仅留 Anthropic
├── main.py                                     # ⬆️ 入口改用 Anthropic 响应
├── tests/
│   ├── test_agent_hooks.py                     # ⬆️ Fake 响应改为 Anthropic block
│   └── test_activity_logging.py                # ⬆️ Fake 响应改为 Anthropic block
└── internal/
    ├── conversation_log.py                     # ⬆️ 日志字段 stop_reason / has_tool_uses
    └── Agent/
        ├── llm_adapter.py                      # 🗑️ 删除（单 provider 无需适配层）
        ├── llm_config.py                       # ⬆️ 直接创建 anthropic.Anthropic
        ├── config.py                           # ⬆️ LLMConfig 去 provider/base_url
        ├── base_agent.py                       # ⬆️ 接入三层压缩 + 核心循环改 Anthropic
        ├── start.py                            # ⬆️ 入口改用 Anthropic 响应
        └── tools/
            ├── compact.py                      # 🆕 三层压缩；⬆️ 移除 OpenAI 分支
            ├── base.py                         # ⬆️ to_anthropic_schema
            ├── registry.py                     # ⬆️ get_anthropic_tools
            ├── sub_agent.py                    # ⬆️ DELEGATE_SCHEMA + 响应读取
            ├── task.py                         # ⬆️ TASK_UPDATE_SCHEMA
            ├── todo.py                         # ⬆️ TODO_SCHEMA
            └── __init__.py                     # ⬆️ 工具列表调用更名
```

**核心变更**：

- 新增 `tool_result_budget`、`snip_compact`、`micro_compact`、`auto_compact`、`_persist_large_output` 构成的三层压缩管线
- 删除 `llm_adapter.py` 及 `Normalized*` 数据类、`_openai_*` 转换函数
- `base_agent.py` 的 LLM 调用按 `llm.stream` 在 `messages.stream` / `messages.create` 间切换，调用前只读分离 `system` 到顶层参数
- assistant 消息手动构造 content block（仅 text/tool_use 标准字段），避免 `model_dump()` 带出 `parsed_output` 触发 API 400
- `LLMConfig` 加回 `base_url`（兼容端点/代理）与 `stream` 开关
- 工具调用与结果统一为 Anthropic 的 `tool_use` / `tool_result` 内容块
- 工具 schema 统一为 `{name, description, input_schema}`
- 日志字段由 OpenAI 术语（`finish_reason`/`has_tool_calls`）改为 Anthropic 术语（`stop_reason`/`has_tool_uses`）

---

## 二、对话历史压缩系统

### 2.1 为什么需要分层压缩

Agent 在长任务中会产生大量 `tool_result`，几轮之后上下文就会被撑爆。直接丢弃历史会丢失关键决策，而每次都调用 LLM 做摘要又过于昂贵。因此采用**按成本递增的三层策略**：先用廉价的结构化裁剪兜底，只在真正必要时才调用 LLM 生成摘要。

### 2.2 三层压缩策略总览

| 层级 | 函数 | 触发时机 | 成本 | 做法 |
|------|------|----------|------|------|
| Layer 1 | `tool_result_budget` | 每轮循环 | 极低 | `tool_result` 总字节超预算时，把超大结果落盘替换为摘要 |
| Layer 2 | `snip_compact` | 每轮循环 | 极低 | 消息数超 50 条时裁掉中段，保留头 3 + 尾 47 |
| Layer 3 | `micro_compact` | 每轮循环 | 极低 | 旧 `tool_result` 替换为占位符，保留最近 3 轮 |
| 摘要 | `auto_compact` | LLM 调用 `compact` 工具 | 高 | 落盘完整对话，调用 LLM 生成摘要替换历史 |

三层在 `base_agent.run()` 每轮循环开头顺序执行，互不冲突：

```python
# 被动压缩旧 tool_result
messages[:] = tool_result_budget(messages)
messages[:] = snip_compact(messages)
messages[:] = micro_compact(messages)
```

### 2.3 Layer 1：工具结果预算 `tool_result_budget`

监控所有 `tool_result` 内容块的总字符数。超过 `budget_max`（默认 200KB）时，把大于 `persist_threshold`（默认 30KB）的单条结果落盘，替换为带文件指针的精简摘要：

```python
def tool_result_budget(messages: list) -> list:
    cfg = _cfg()
    entries: list[tuple[dict, str]] = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    entries.append((block, block.get("tool_use_id", "unknown")))
    ...
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
        ...
```

落盘后的占位符形如：

```text
<persisted-output>
全部输出已保存至: .tool_task/tool_result/<tool_use_id>.txt
Preview: <前 2000 字符>...
</persisted-output>
```

### 2.4 Layer 2：消息数量裁剪 `snip_compact`

消息条数超过 50 时裁掉中段，插入 `[snipped N msgs]` 占位。关键约束：**裁剪边界不能落在 `tool_use` 与 `tool_result` 之间**，否则会破坏 Anthropic 协议要求的成对关系，导致 API 报错。因此裁剪点会向后跳过连续的 `tool_result`：

```python
def snip_compact(messages: list, max_message: int = 50):
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
```

### 2.5 Layer 3：占位符替换 `micro_compact`

把早于最近 `keep_recent`（默认 3）轮的 `tool_result` 替换为 `[Previous: used <tool_name>]`，向模型声明"这个工具调用过了、结果不再需要"，避免重复调用。`preserve_result_tools`（默认 `read`、`todo`）清单内的工具结果不压缩：

```python
to_clear = tool_results[:-cfg.keep_recent]
for obj, tool_id in to_clear:
    content = obj.get("content")
    if not isinstance(content, str) or len(content) <= 100:
        continue
    tool_name = tool_map.get(tool_id, "unknown")
    if tool_name in preserve:
        continue
    obj["content"] = f"[Previous: used {tool_name}]"
```

### 2.6 LLM 驱动摘要 `auto_compact` + `CompactTool`

当模型判断上下文过长时，主动调用注册的 `compact` 工具。`CompactTool.execute` 只返回信号，真正的压缩在 Agent 循环检测到调用后执行：

```python
class CompactTool(BaseTool):
    name = "compact"
    description = "压缩对话历史。当上下文过长或对话轮次过多时调用此工具..."

    def execute(self) -> str:
        return "压缩请求已接收，正在压缩对话历史..."
```

`auto_compact` 先把完整对话以 JSONL 追加落盘到 `.transcripts/transcript_<topic>.json`，再截取尾部 `conversation_slice` 字符送给 LLM 生成摘要，最后用摘要替换整个历史：

```python
def auto_compact(messages: list, client, model: str = None, topic: str = "") -> str:
    ...
    response = client.messages.create(
        model=model,
        messages=[{"role": "user", "content": "请对本次对话进行总结..."]},
        max_tokens=cfg.max_tokens,
    )
    summary = "".join(b.text for b in response.content if b.type == "text") or "未生成摘要。"
    return f"[对话已压缩。对话保存位置： {transcript_path}]\n\n{summary}"
```

### 2.7 压缩在 Agent 循环中的集成

每轮 LLM 调用前跑三层被动压缩；检测到 `compact` 工具被调用且获准后，先追加所有并行工具结果，再触发 `auto_compact` 重构历史：

```python
if compact_called:
    system_msg = self._extract_system_message(messages)
    compact_content = auto_compact(messages, client=self.client, model=self.model)
    new = []
    if system_msg:
        new.append(system_msg)
    new.append({"role": "user", "content": compact_content})
    messages[:] = new
```

---

## 三、Anthropic 原生格式统一

### 3.1 演进：从双格式适配到原生统一

此前为兼容 OpenAI 兼容端点与 Anthropic 原生 API，存在一个 `llm_adapter.py` 适配层：内部消息保持 OpenAI 格式，调用 Anthropic 时在边界转换。代价是 `compact.py` 等模块必须对两种格式分别写分支。

确定只使用 Anthropic 一个 provider 后，适配层失去意义，本轮将其彻底移除，**内部消息、工具 schema、调用接口全部改为 Anthropic 原生格式**。这样 `compact.py` 等只需处理一种格式，代码更直接。

### 3.2 统一后的内部消息格式

| 概念 | 统一后的 Anthropic 格式 |
|------|------------------------|
| 工具调用 | `{role:"assistant", content:[{type:"tool_use", id, name, input:{...}}]}` |
| 工具结果 | `{role:"user", content:[{type:"tool_result", tool_use_id, content}]}`（一轮的多个结果合并为一条 user 消息） |
| 工具 schema | `{name, description, input_schema:{type, properties, required}}` |
| system | 仍以 `{role:"system"}` 存于 messages 列表，调用边界提取为顶层 `system` 参数 |
| 结束信号 | `response.stop_reason == "end_turn"` |

### 3.3 LLM 调用边界：system 分离 + 流式开关

Anthropic API 要求 `system` 作为顶层参数而非 messages 列表中的角色。`base_agent` 在每轮调用前**只读分离** system（不从 messages 移除，保留列表结构供压缩路径使用），并按 `llm.stream` 配置在流式与非流式之间切换：

```python
system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
dialog = [m for m in messages if m.get("role") != "system"]
kwargs = {"model": self.model, "messages": dialog, "max_tokens": self.max_tokens}
if self.tools:
    kwargs["tools"] = self.tools
if system_parts:
    kwargs["system"] = "\n\n".join(system_parts)

if self.stream:
    # 流式：逐字打印 text 增量，结束后取完整 message
    printed_text = False
    with self.client.messages.stream(**kwargs) as stream:
        for chunk in stream.text_stream:
            print(chunk, end="", flush=True)
            printed_text = True
        response = stream.get_final_message()
    if printed_text:
        print()
else:
    # 非流式：阻塞等待完整响应，end_turn 时统一打印 "回复：..."
    response = self.client.messages.create(**kwargs)
```

**为何需要流式开关**：Anthropic SDK 对非流式请求有一个保护——当 `max_tokens` 估算的生成时间超过默认 timeout（10 分钟）时强制要求 streaming，否则抛 `ValueError: Streaming is required for operations that may take longer than 10 minutes`。因此 `max_tokens` 较大（如 100000）时必须 `stream: true`；`max_tokens` 较小（如 4096）时两种模式均可，`stream: false` 更简单（阻塞返回，结束统一打印回复）。

### 3.4 响应解析：content blocks 的安全构造

Anthropic 响应的 `content` 是内容块列表（`TextBlock` / `ToolUseBlock`）。解析时按 `type` 拆出文本与工具调用，usage 用 `model_dump()` 转 dict：

```python
stop_reason = response.stop_reason
usage = getattr(response, "usage", None)
if usage is not None and hasattr(usage, "model_dump"):
    usage = usage.model_dump()
text = "".join(b.text for b in response.content if b.type == "text")
tool_uses = [b for b in response.content if b.type == "tool_use"]
```

**坑：不能直接用 `block.model_dump()` 构造 assistant 消息**。`TextBlock.model_dump()` 会带出 `parsed_output` 等 SDK 内部字段，把这样的 dict 作为 assistant content 追加到 messages 后，下一轮发回 API 会报：

```text
400 messages.N.content.M.text.parsed_output: Extra inputs are not permitted
```

因此 assistant 消息的 content 必须**只保留协议标准字段**，手动构造（流式与非流式都适用）：

```python
assistant_content = []
for b in response.content:
    if b.type == "text":
        assistant_content.append({"type": "text", "text": b.text})
    elif b.type == "tool_use":
        assistant_content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
assistant_msg = {"role": "assistant", "content": assistant_content}
messages.append(assistant_msg)
```

### 3.5 工具调用与结果：tool_use / tool_result

工具调用从 `tool_use` 块直接取 `name` / `input`（已是 dict，无需 `json.loads`）。结果不再逐条追加 `role:"tool"` 消息，而是收集为本轮所有结果合并的一条 `user` 消息，符合 Anthropic 协议：

```python
compact_called = False
tool_result_blocks = []
for block in tool_uses:
    output = results[block.id]
    tool_result_blocks.append({
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": output,
    })
    if block.name == "compact" and block.id in approved_ids:
        compact_called = True
messages.append({"role": "user", "content": tool_result_blocks})
```

### 3.6 工具 schema：input_schema

`BaseTool.to_anthropic_schema()` 从 `execute` 签名自动生成 Anthropic tool definition；嵌套类型（如 `task_update`、`todo`、`delegate`）通过 `schema_override` 手写：

```python
schema = {
    "name": self.name,
    "description": resolved_desc,
    "input_schema": {"type": "object", "properties": properties},
}
if required:
    schema["input_schema"]["required"] = required
```

### 3.7 客户端初始化（简化）

`llm_config.py` 不再有 provider 分支，直接创建 `anthropic.Anthropic`。`base_url` 非空时传入（用于代理/兼容端点），为空则回落到官方端点：

```python
def _create_client() -> anthropic.Anthropic:
    cfg = get_config().llm
    api_key = os.environ.get(cfg.api_key_env, cfg.api_key)
    if not api_key:
        raise ValueError(f"API Key 未配置：请设置环境变量 {cfg.api_key_env}")
    kwargs = {"api_key": api_key}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return anthropic.Anthropic(**kwargs)

client = _create_client()
```

---

## 四、配置项汇总

```yaml
llm:
  api_key_env: "ANTHROPIC_API_KEY"
  api_key: ""
  base_url: ""                          # 兼容端点/代理；空则用官方端点
  default_model: "claude-sonnet-4-6"
  default_max_tokens: 4096              # 较大时需配合 stream: true
  stream: false                         # 流式输出开关

compact:
  keep_recent: 3                          # micro_compact 保留的最近轮数
  preserve_result_tools: ["read", "todo"] # 不压缩的工具
  threshold: 50000                        # auto_compact 触发阈值
  model: "glm-5.1"                        # 摘要模型
  conversation_slice: 80000               # 送 LLM 的尾部字符数
  max_tokens: 2000                        # 摘要 max_tokens
  persist_threshold: 30000                # 单条结果落盘阈值
  budget_max: 204800                      # tool_result 总预算（200KB）
```

---

## 五、后续规划（TODO）

- 压缩阈值目前按字符数估算，未真实反映 token；可接入 tokenizer 做更精确的预算控制
- `micro_compact` 的 `preserve_result_tools` 仍是静态清单，可改为按工具元数据声明是否可压缩
- `auto_compact` 的摘要 prompt 目前硬编码，可抽到配置或 system prompt 中
- `todo.py` 未在默认注册表中使用，属历史遗留，后续可清理