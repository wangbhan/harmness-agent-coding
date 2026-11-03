# Day 8：用户记忆提取

> 日期：2026-10-10
> 涉及提交：`a200c78 feat(agent): 添加用户记忆提取功能`、`ad186eb fix(memory): 修复记忆提取功能的异常处理和兼容性问题`、`a1c9015 chore(config): 添加 .memory 文件到忽略列表`
> 涉及文件：`internal/Agent/memory.py`（新增）、`internal/Agent/base_agent.py`、`config.yaml`、`.gitignore`

## 一、本轮背景

目标是让 Agent 具备跨会话的长期记忆能力。设计上有两半（记录在 `memory.py` 顶部注释）：

1. **提取**：每轮对话结束后，从对话中识别用户偏好、约束条件、项目事实，落盘成记忆文件。
2. **注入**：调用 Agent 时把记忆以及历史中相关的对话片段一并注入上下文。

本轮只落地了第 1 部分（提取 + 存储 + 索引），第 2 部分尚未接入——目前 `.memory` 目录只有写入方，没有任何读取方把它拼进 prompt。

```text
harmness-agent-coding/
├── .gitignore                    # ⬆️ 新增 .memory 忽略规则
├── config.yaml                   # ⬆️ base_url 置空；workdir 改为 "."
└── internal/Agent/
    ├── memory.py                 # 🆕 MemoryExtractor：提取、落盘、索引
    └── base_agent.py             # ⬆️ ReAct 循环中接入记忆提取旁路
```

**核心变更**：

- 新增 `internal/Agent/memory.py`，以 `MemoryExtractor` 为中心实现"LLM 抽取 → JSON 解析 → Markdown 落盘 → 索引重建"
- 记忆以「一条记忆一个 `.md` 文件 + frontmatter 元数据」的形式存于 `<workdir>/.memory/`，并自动维护索引 `MEMORY.md`
- `base_agent` 在 `stop_reason != "tool_use"` 时（即一轮回复收尾）触发提取，作为**旁路功能**用 `try/except` 包裹，失败不影响主对话流程
- `_get_memory_extractor` 延迟初始化，规避模块导入期 `WORKDIR` 尚未就绪的问题
- `config.yaml` 的 `workdir` 由 `""` 改为 `"."`，否则 `.memory` 会写到项目上一级目录

---

## 二、记忆模块设计

### 2.1 存储结构

每条记忆一个 Markdown 文件，文件名取 `name` 的 kebab-case slug：

```markdown
---
name: user-preference-tabs
description: 用于索引检索的一行摘要
type: user
---
完整内容，Markdown 格式编写
```

目录内另有一份索引 `MEMORY.md`，由 `_rebuild_index()` 在每次写入后全量重建：

```text
- [user-preference-tabs](user-preference-tabs.md) - 用于索引检索的一行摘要
```

四种记忆类型（与 Claude Code 的记忆分类思路对齐）：

| type | 含义 |
|------|------|
| `user` | 用户偏好 |
| `feedback` | 用户给出的指导、要求、反馈 |
| `project` | 项目相关事实 |
| `reference` | 外部引用与参考信息 |

索引只存 `name + description`，`body` 留在各自文件里——这样后续做注入时可以先只读索引（廉价），再按需拉取具体记忆文件。

### 2.2 提取流程 `extract_memories`

输入是当前完整的 `messages` 列表，流程分四步：

```python
def extract_memories(self, messages: list):
    # 1. 取最近 10 条消息，摊平成 "role: content" 文本
    for message in messages[-10:]:
        ...
    dialogue = "\n".join(messages_part)
    if not dialogue.strip():
        return

    # 2. 列出已有记忆，作为"去重基线"喂给模型
    existing = self.list_memory_files()
    existing_desc = "\n".join(f"- {m['name']}: {m['description']}" for m in existing)

    # 3. 单轮 LLM 调用，要求返回 JSON 数组
    response = self.client.messages.create(
        model=self.model, messages=[{"role": "user", "content": prompt}], max_tokens=2000
    )

    # 4. 正则抠出 JSON 数组，逐条落盘
    match = re.search(r'\[.*\]', text, re.DOTALL)
    items = json.loads(match.group())
```

Prompt 里有几个刻意的约束：

- 对话内容截断到 `[:4000]` 字符，控制成本
- **把已有记忆的 name + description 一起给模型**，并在指令中要求"如果信息已包含在已有记忆中，返回 `[]`"——用模型判断来替代写前查重
- 要求输出严格 JSON 数组 `{name, type, description, body}`

解析上**没有要求模型只输出 JSON**，而是用 `re.search(r'\[.*\]', text, re.DOTALL)` 从自由文本里抠出数组，容忍模型附带解释性文字。

### 2.3 写文件与"更新"语义

```python
def write_memory_file(self, name, mem_type, desc, body):
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filepath = self.MEMORY_DIR / f"{slug}.md"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(f"---\nname: {name}\ndescription: {desc}\ntype: {mem_type}\n---\n{body}")
    self._rebuild_index()
```

写入用的是 `write_text` 全量覆盖，同名文件直接替换。**记忆更新因此依赖于 name 稳定**：模型若对同一件事给出不同 `name`，会产生两份并存记忆，而非覆盖。

### 2.4 frontmatter 解析

没有引入 YAML 依赖，`_parse_frontmatter` 手写按行 `split(":", 1)`：

```python
parts = text.split("---", 2)
for line in parts[1].strip().splitlines():
    if ":" in line:
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
```

只支持扁平 key-value，够用于 `name/description/type` 三个字段；body 缺少 description 时退化为取首行前 80 字符作为索引摘要。

---

## 三、在 Agent 循环中的接入与旁路化

### 3.1 触发时机

```python
stop_reason = response.stop_reason

# 开始提取用户记忆并保存在运行地（旁路功能，失败不应影响主对话流程）
if stop_reason != "tool_use":
    try:
        _get_memory_extractor(self.client, self.model, self.max_tokens).extract_memories(messages)
    except Exception as e:
        print(f"\033[31m[Memory] 记忆提取旁路异常：{e}\033[0m")
```

条件选的是 `stop_reason != "tool_use"`，即**只在 ReAct 内循环的每一轮"模型不再请求工具"时提取**，工具密集的中段轮次一律跳过，避免每轮都多打一次 LLM。

一个已知副作用：提取发生在 assistant 消息 append 到 `messages` **之前**，因此本轮刚刚生成的这条回复不在 `messages[-10:]` 窗口内，提取看到的对话总是"截至上一条"。

### 3.2 为什么必须延迟初始化

`memory.py` 模块级就有 `WORK_DIR = get_workdir()`，而 `get_workdir()` 会触发 `init_workdir()` 读取配置。问题在于 `base_agent.py` 顶部 `from internal.Agent.memory import ...` 时，配置与 workdir 可能还没就绪，模块级常量会把一个错误的 workdir 固化下来。

因此单例构造不读模块常量，而是在首次真正用到时才取 workdir：

```python
_MEMORY_EXTRA: "MemoryExtractor | None" = None

def _get_memory_extractor(client, model, max_tokens) -> "MemoryExtractor":
    """延迟初始化 MemoryExtractor，确保配置和 WORKDIR 已就绪"""
    global _MEMORY_EXTRA
    if _MEMORY_EXTRA is None:
        work_dir = get_workdir()
        _MEMORY_EXTRA = MemoryExtractor(client, model, max_tokens, work_dir)
    return _MEMORY_EXTRA
```

同时 `MemoryExtractor.__init__` 接收 `work_dir` 参数，`self.MEMORY_DIR = work_dir / MEMORY_DIR`，不在类内部重新推导路径。

---

## 四、修复轮（`ad186eb`）：让旁路真正"打不死"

首轮实现跑起来后暴露四类问题，都在同一个提交里修掉：

### 4.1 静默吞异常 → 全部显式打日志

初版结尾是 `except Exception: pass`，出问题时完全无迹可循。改为分级输出：

| 场景 | 行为 |
|------|------|
| 最近对话为空 | `[Memory] 跳过：...`（黄） |
| 输出中找不到 JSON 数组 | `[Memory] 跳过：...`（黄，附前 200 字符） |
| JSON 解析失败 | `[Memory] 跳过：...（可能被截断）`（黄） |
| 提取/保存抛异常 | `[Memory] 提取/保存失败：{e}`（红） |
| 调用方层面兜底 | `base_agent` 外层再包一层 `try/except`（红） |

**双层 try/except 是刻意的**：`extract_memories` 内部负责"失败得有声有色"，外层负责"任何失败都绝不能打断主对话流程"——记忆是增值功能，不能拖垮核心链路。

### 4.2 content 块结构不统一

`extract_memories` 拿到的 `messages` 里，content block 可能是 dict（`{"type": "text", ...}`，Agent 自己 append 的）也可能是 Anthropic SDK 对象（`TextBlock`）。初版只用 `getattr(b, "text", "")` 按对象访问，遇到 dict 直接取空——表现为"最近对话为空，无可提取内容"。

修法是两条路径都走：

```python
for b in content:
    if isinstance(b, dict):
        if b.get("type") == "text":
            parts.append(str(b.get("text", "")))
    elif getattr(b, "type", None) == "text":
        parts.append(str(getattr(b, "text", "")))
content = " ".join(p for p in parts if p)
```

### 4.3 max_tokens 截断导致 JSON 解析失败

提取调用的 `max_tokens` 初版写死 `800`。多轮对话里模型倾向于输出较长 body，JSON 数组被从中间截断，`json.loads` 必然失败——而这类失败在 4.1 之前是静默的，很难定位。改为 `2000`，并在解析失败的日志里点明"可能被截断"。

注意：`_get_memory_extractor` 虽然把 `self.max_tokens` 传进了构造函数，但 `create()` 里用的是硬编码的 `2000`，形参实际未生效。

### 4.4 workdir 为空导致 `.memory` 写错位置

`config.yaml` 里 `workdir: ""` 时，`init_workdir()` 的回落逻辑是 `Path.cwd().parent`：

```python
if paths_config.workdir:
    _WORKDIR = Path(paths_config.workdir).resolve()
else:
    _WORKDIR = Path.cwd().parent
```

结果 `.memory` 被建到项目目录的**上一级**。改为 `workdir: "."`，落在项目根目录，同时补 `.gitignore`（`a1c9015`）避免把用户记忆提交进仓库。

---

## 五、配置项汇总

本轮未新增独立配置段，复用现有字段：

```yaml
llm:
  base_url: ""            # ⬆️ 由硬编码代理地址改为空，回落官方端点
  default_model: "claude-sonnet-4-6"   # 记忆提取同样用这个模型

paths:
  workdir: "."            # ⬆️ 原为 ""（会回落到 Path.cwd().parent，导致 .memory 写到上一级）
```

记忆提取的参数目前全部硬编码在 `memory.py` 中：最近消息条数 `10`、对话截断 `4000` 字符、`max_tokens=2000`、目录名 `.memory`。

---

## 六、后续规划（TODO）

- **记忆注入（设计的第 2 步）尚未实现**：当前只有写入方，没有任何读取方把 `MEMORY.md` / 记忆内容拼进 system prompt。没有注入，记忆就只是"记下来了但用不上"，这是下一步的主线
- **注入策略**：按 `memory.py` 顶部注释的设想，除记忆本身外还应"将之前对话和记忆相关的部分通过模型判断后注入"，需要一个相关性筛选环节
- **提取模型独立配置**：目前提取复用主对话模型（`claude-sonnet-4-6`），每轮收尾都多打一次调用。可参照 `compact.model` 的做法增加 `memory.model`，改用轻量模型并加开关
- **相关死代码**：`base_agent.py` 导入了 `MemoryExtractor` 却只用 `_get_memory_extractor`；`memory.py` 模块级 `WORK_DIR` 在延迟初始化后已无人使用；`write_memory_file` 标注 `-> None` 却 `return filepath`；构造函数收到的 `max_tokens` 未生效
- **去重仍靠模型自觉**：name 不稳定会产生重复记忆文件，后续可在写入侧加按 description 相似度合并，或提供显式的更新/删除入口
- **可观测性**：提取逻辑目前只 `print` 到终端，未接入 `activity_log`，与日志体系脱节
