# Day 4：后台任务执行 + 配置中心化 + 会话日志 + 架构重构

> 日期：2026-05-08
> 涉及文件：`internal/Agent/config.py`、`internal/Agent/conversation_log.py`、`internal/Agent/system.py`、`internal/Agent/base_agent.py`、`internal/Agent/start.py`、`internal/Agent/tools/background.py`、`internal/Agent/tools/base.py`、`internal/Agent/tools/__init__.py`、`config.yaml`

## 一、项目结构变更

相比 Day 3，新增配置中心、会话日志、后台任务等模块，`start.py` 大幅精简：

```
app/
├── config.yaml                # 🆕 配置模板（91行，中文注释）
├── config.yaml.local          # 🆕 本地配置覆盖（.gitignore）
└── internal/
    └── Agent/
        ├── config.py              # 🆕 Pydantic 配置模型 + 三级加载
        ├── conversation_log.py    # 🆕 loguru JSONL 会话日志
        ├── system.py              # 🆕 系统提示词模块（动态时间）
        ├── base_agent.py          # ⬆️ 集成日志 + 后台通知注入
        ├── start.py               # ⬆️ 157行 → 53行，拆分为独立模块
        └── tools/
            ├── __init__.py        # ⬆️ 显式注册13个工具 + 延迟初始化子代理
            ├── base.py            # ⬆️ WORKDIR 延迟初始化
            ├── background.py      # 🆕 BackGroundManager + BgTool
            ├── bash.py            # ⬆️ 参数从配置读取
            ├── read.py            # ⬆️ max_content_len 从配置读取
            ├── compact.py         # ⬆️ 压缩参数从配置读取
            ├── task.py            # ⬆️ tasks_dir 从配置读取
            ├── sub_agent.py       # ⬆️ max_result_len 从配置读取
            └── skill.py           # ⬆️ skills_dir 从配置读取
```

**本轮核心变更**：
- `background.py`：新增 `BackGroundManager` 后台任务管理器，线程池 + 锁 + 信号量保证并发安全
- `config.py`：使用 Pydantic v2 定义 10 个配置模型，三级优先加载（yaml → local → 环境变量）
- `conversation_log.py`：基于 loguru 的 JSONL 结构化会话日志
- `system.py`：系统提示词抽取为独立模块，时间从"启动固定"改为"每次动态获取"
- `start.py`：从 157 行精简为 53 行，所有职责拆分到独立模块

---

## 二、后台任务执行系统

对于耗时的 bash 命令（如长时间构建、测试），前台执行会阻塞 Agent 循环。新增 `BackGroundManager` + `BgTool`，支持在线程中异步执行，完成后通过通知队列注入对话。

### 2.1 BackGroundManager 类设计

```python
class BackGroundManager:
    def __init__(self, work_dir: Path, max_concurrent: int):
        self.tasks = {}                              # task_id → {status, command, result}
        self._lock = threading.Lock()                # 保护 tasks 和 _notifications
        self._notifications = []                     # 已完成任务的待通知列表
        self.work_dir = work_dir
        self._semaphore = threading.Semaphore(max_concurrent)  # 并发上限控制
```

**核心方法**：

| 方法 | 职责 | 线程安全 |
|------|------|----------|
| `run(command)` | 危险命令拦截 → 获取信号量 → UUID 任务ID → 启动守护线程 → 立即返回 | Lock 保护 tasks 写入 |
| `_work(task_id, command)` | 在线程中执行 subprocess.run，处理超时/异常，完成后更新状态、加入通知队列、释放信号量 | Lock 保护 tasks + notifications |
| `check(task_id)` | 查询任务状态，task_id 为空则列出所有任务 | Lock 保护 tasks 读取 |
| `drain_notifications()` | 获取并清空通知队列（被 Agent 循环每轮调用） | Lock 保护 notifications |

### 2.2 BgTool 工具封装

`BgTool` 继承 `BaseTool`，封装为 LLM 可调用的工具：

```python
class BgTool(BaseTool):
    name = "background"
    description = "在后台运行或查询bash命令。action='run'启动后台任务，action='check'查询任务状态和结果"

    def execute(self, action: str = "run", command: str = "", task_id: str = "") -> str:
        if action == "check":
            return _get_bg_manager().check(task_id)
        if not command:
            return "错误：action='run' 时必须提供 command 参数"
        return _get_bg_manager().run(command)
```

**参数说明**：

| 参数 | 用途 | 必填 |
|------|------|------|
| `action` | `"run"` 启动任务，`"check"` 查询状态 | 是 |
| `command` | bash 命令（action=run 时） | run 时必填 |
| `task_id` | 任务 ID（action=check 时，留空列出全部） | 否 |

### 2.3 线程安全与并发控制

后台任务系统通过三层机制保证线程安全：

```
用户调用 background run
        │
        ▼
  ┌─ 危险命令拦截 ─┐
  │ 黑名单匹配     │ → 命中则拒绝
  └────────────────┘
        │
        ▼
  ┌─ 信号量控制 ──┐
  │ acquire()     │ → 达到上限则拒绝
  │ max=5         │
  └────────────────┘
        │
        ▼
  ┌─ Lock 保护 ───┐
  │ tasks[id] =   │
  │ {running...}  │
  └────────────────┘
        │
        ▼
  Thread(daemon=True) → _work()
        │
        ├─ subprocess.run(timeout=600)
        ├─ Lock: 更新状态 + 加入通知队列
        └─ semaphore.release()
```

**三层保护**：

| 层级 | 机制 | 作用 |
|------|------|------|
| 1. 命令拦截 | `dangerous_commands` 黑名单 | 阻止 `rm -rf /`、`sudo` 等危险操作 |
| 2. 并发控制 | `threading.Semaphore(max_concurrent)` | 限制同时运行的后台任务数（默认 5） |
| 3. 数据保护 | `threading.Lock` | 保证 `tasks` 和 `_notifications` 的原子操作 |

### 2.4 通知队列与 Agent 集成

Agent 循环每轮开始时调用 `drain_notifications()`，将已完成的后台任务以 system 消息注入对话：

```python
# base_agent.py → run() 循环开头
notifs = _get_bg_manager().drain_notifications()
if notifs:
    lines = ["以下后台任务已完成："]
    for n in notifs:
        lines.append(f"任务 {n['task_id']}：\n  状态：{n['status']}\n  命令：{n['command']}")
    messages.append({"role": "system", "content": "\n".join(lines)})
```

**通知注入流程**：

```
Agent 循环每轮
    │
    ├─ ① drain_notifications()     ← 获取已完成任务 + 清空队列
    │      └─ 有通知 → 注入 system 消息
    │
    ├─ ② micro_compact(messages)   ← 被动压缩
    │
    ├─ ③ 调用 LLM                  ← LLM 能看到后台任务完成状态
    │
    └─ ④ 并行执行工具调用
```

---

## 三、配置中心化

Day 1-3 的所有参数（超时、黑名单、截断长度、模型名等）均为硬编码。本轮使用 Pydantic v2 定义配置模型，统一从 `config.yaml` 读取。

### 3.1 Pydantic 配置模型

共定义 10 个配置模型类，形成树形聚合结构：

```
AgentConfig (顶层)
├── llm: LLMConfig              # API Key、Base URL、模型、max_tokens
├── paths: PathsConfig          # workdir、transcripts/logs/tasks/skills 目录
├── compact: CompactConfig      # 压缩策略参数
├── log: LogConfig              # 日志级别、截断长度
└── tools: ToolsConfig
    ├── bash: BashToolConfig    # 黑名单、超时、编码、后台任务参数
    ├── read: ReadToolConfig    # 文件读取最大长度
    ├── sub_agent: SubAgentConfig  # 子 Agent 结果最大长度
    └── todo: TodoToolConfig    # 任务列表最大数量
```

**配置类职责**：

| 配置类 | 关键字段 | 默认值示例 |
|--------|----------|-----------|
| `LLMConfig` | api_key, base_url, default_model | `"glm-5.1"`, 8000 |
| `PathsConfig` | workdir, transcripts_dir, logs_dir | `""`, `".transcripts"`, `".logs/sessions"` |
| `CompactConfig` | keep_recent, threshold, conversation_slice | 3, 50000, 80000 |
| `LogConfig` | level, max_args_debug, max_result_debug | `"DEBUG"`, 10000, 10000 |
| `BashToolConfig` | dangerous_commands, timeout, bg_timeout, bg_max_concurrent | `["rm -rf /"...]`, 120, 600, 5 |
| `ReadToolConfig` | max_content_len | 50000 |
| `SubAgentConfig` | max_result_len | 8000 |
| `TodoToolConfig` | max_tasks | 20 |

### 3.2 三级配置加载

配置按优先级从低到高逐级覆盖：

```
config.yaml          ← 基础模板，提交到 Git
       ↓ _deep_merge()
config.yaml.local    ← 本地覆盖，含敏感信息（.gitignore）
       ↓ _apply_env_overrides()
环境变量              ← 最高优先级
```

**三级加载实现**：

```python
def init_config(config_dir: Path | None = None):
    base_dir = config_dir or _CONFIG_DIR
    base = _load_yaml(base_dir / "config.yaml")       # 第一级：基础模板
    local = _load_yaml(base_dir / "config.yaml.local") # 第二级：本地覆盖
    merged = _deep_merge(base, local)                   # 递归合并字典
    merged = _apply_env_overrides(merged)               # 第三级：环境变量覆盖
    _config_instance = AgentConfig(**merged)
```

**`_deep_merge`**：递归合并字典，`config.yaml.local` 中的键值覆盖 `config.yaml`。

**`_apply_env_overrides`**：映射 6 个环境变量到配置路径：

| 环境变量 | 配置路径 | 类型 |
|----------|----------|------|
| `ZAI_API_KEY` | `llm.api_key` | str |
| `ZAI_BASE_URL` | `llm.base_url` | str |
| `AGENT_MODEL` | `llm.default_model` | str |
| `AGENT_MAX_TOKENS` | `llm.default_max_tokens` | int |
| `AGENT_LOG_LEVEL` | `log.level` | str |
| `AGENT_WORKDIR` | `paths.workdir` | str |

### 3.3 单例延迟初始化

采用模块级变量 + `get_config()` 的单例模式：

```python
_config_instance: Optional[AgentConfig] = None
_config_loaded: bool = False

def get_config() -> AgentConfig:
    """获取配置，首次调用时自动加载"""
    if not _config_loaded:
        init_config()
    return _config_instance
```

首次调用 `get_config()` 时自动触发 `init_config()`，后续所有调用返回同一个实例。

---

## 四、懒加载模式

Day 4 引入多个全局管理器（`BackGroundManager`、`TaskManager`、`SkillLoader`），均采用 **模块级变量 + 懒加载函数** 的单例模式。

### 4.1 实现模式

```python
# 模块级变量
_BG_MANAGER: "BackGroundManager | None" = None

# 懒加载函数
def _get_bg_manager() -> "BackGroundManager":
    global _BG_MANAGER
    if _BG_MANAGER is None:
        cfg = get_config().tools.bash
        work_dir = get_workdir()
        _BG_MANAGER = BackGroundManager(work_dir, cfg.bg_max_concurrent)
    return _BG_MANAGER
```

三个关键步骤：
1. **定义模块级变量**（初始为 `None`）
2. **通过 global 声明共享**，函数内首次访问时创建实例
3. **后续所有调用返回同一个实例**

### 4.2 懒加载的好处

| 好处 | 说明 |
|------|------|
| **线程安全共享状态** | 所有调用者操作同一个 tasks/notifications，无需跨模块传递实例 |
| **避免循环导入** | `base_agent.py` 不需要在模块加载时构造 manager，而是运行时按需获取 |
| **配置就绪保证** | 确保 `init_config()` 和 `init_workdir()` 在实例化之前已完成 |

---

## 五、会话日志系统

由于暂未实现对话持久化存储，使用 loguru 实现结构化日志，记录完整的用户输入、LLM 响应、工具调用链路。

### 5.1 SessionLogger 类

每次 REPL 会话创建独立实例，生成 `.jsonl` 日志文件：

```python
class SessionLogger:
    def __init__(self, level: str = None):
        cfg = get_config()
        log_dir = get_workdir() / cfg.paths.logs_dir
        log_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        self.session_id = now.strftime("session_%Y-%m-%dT%H-%M-%S-") + f"{now.microsecond // 1000:03d}"

        logger.add(
            log_dir / f"{self.session_id}.jsonl",
            level=self.level.upper(),
            format=_json_formatter,        # 自定义 JSON 行格式
            encoding="utf-8",
        )
```

**事件记录方法**：

| 方法 | 记录内容 | 级别 |
|------|----------|------|
| `session_start()` | system prompt 长度和前 200 字符预览 | INFO |
| `session_end()` | 总交互轮数 | INFO |
| `user_input()` | 用户输入内容 | INFO |
| `llm_response()` | finish_reason、模型名、消息数、是否有工具调用 | DEBUG |
| `agent_reply()` | Agent 回复内容（截断到 max_reply_len） | INFO |
| `tool_call()` | 工具名、调用 ID、参数摘要 + 完整参数 | INFO + DEBUG |
| `tool_result()` | 工具名、调用 ID、结果摘要 + 完整结果 | INFO + DEBUG |
| `error()` / `warning()` | 错误/警告消息 | ERROR / WARNING |

### 5.2 JSONL 结构化格式

每条日志为一行 JSON，包含 `timestamp`、`level`、`session_id`、`event`、`data` 五个字段：

```json
{"timestamp": "2026-05-08T10:30:00.123456+00:00", "level": "INFO", "session_id": "session_2026-05-08T10-30-00-123", "event": "tool_call", "data": {"tool_name": "bash", "call_id": "call_xxx", "arguments_summary": "{\"command\":\"ls -la\"}"}}
{"timestamp": "2026-05-08T10:30:01.234567+00:00", "level": "DEBUG", "session_id": "session_2026-05-08T10-30-00-123", "event": "tool_call_full", "data": {"tool_name": "bash", "call_id": "call_xxx", "arguments": "{\"command\":\"ls -la /home/user/project\"}"}}
```

### 5.3 双级别日志

`tool_call` 和 `tool_result` 同时输出 INFO 和 DEBUG 两个级别：

| 级别 | 截断长度 | 用途 |
|------|----------|------|
| INFO | 参数 500 / 结果 500 | 快速浏览，关注工具调用摘要 |
| DEBUG | 参数 10000 / 结果 10000 | 完整记录，用于问题排查 |

截断长度通过 `LogConfig` 配置，支持按需调整。

---

## 六、代码架构重构

### 6.1 start.py 精简

从 157 行缩减为 53 行，所有职责拆分到独立模块：

| 原有职责 | 拆分到 | 文件 |
|----------|--------|------|
| Agent 类定义 | `Agent` 类 | `base_agent.py` |
| 系统提示词 | `get_system_prompt()` / `get_subagent_prompt()` | `system.py` |
| LLM 客户端创建 | `client` 实例 | `llm_config.py` |
| 配置加载 | `get_config()` | `config.py` |
| 工具注册 | `default_registry` + `setup_delegate()` | `tools/__init__.py` |
| 会话日志 | `SessionLogger` | `conversation_log.py` |

重构后的 `start.py` 仅负责组装和运行 REPL 循环：

```python
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
    history = [{"role": "system", "content": system}]

    try:
        while True:
            query = input("请输入问题：")
            ...
            parent_agent.run(history)
    finally:
        session_log.session_end(turn_count=...)
        session_log.close()
```

`try/finally` 确保 `session_end()` 和 `close()` 被调用，日志正确刷盘。

### 6.2 系统提示词模块化

将原来硬编码在 `start.py` 中的系统提示词抽取为 `system.py`：

```python
def get_system_prompt() -> str:
    now_utc = datetime.now(timezone.utc)
    return (f"现在时间是：{now_utc}\n"
            f"当前系统是: {platform.system()}"
            f"你是一个在{get_workdir()}下的coding agent助手...\n"
            f"Skills available:\n"
            f"{_get_skill_loader().get_descriptions()}")
```

**关键改进**：时间从"启动时固定"改为"每次调用时动态获取"，解决了之前时间不更新的问题。

### 6.3 WORKDIR 延迟初始化

`tools/base.py` 中 WORKDIR 从模块级常量改为延迟初始化模式：

```python
# Day 3：模块级常量（加载时确定）
WORKDIR = Path.cwd()

# Day 4：延迟初始化
_WORKDIR: Path = None
_WORKDIR_INITIALIZED: bool = False

def init_workdir(paths_config=None):
    global _WORKDIR, _WORKDIR_INITIALIZED
    if _WORKDIR_INITIALIZED:
        return
    if paths_config is None:
        paths_config = get_config().paths
    if paths_config.workdir:
        _WORKDIR = Path(paths_config.workdir).resolve()
    else:
        _WORKDIR = Path.cwd().parent
    _WORKDIR_INITIALIZED = True

def get_workdir() -> Path:
    if not _WORKDIR_INITIALIZED:
        init_workdir()
    return _WORKDIR
```

**改进点**：
- workdir 从配置文件读取，而非硬编码 `Path.cwd()`
- 未初始化时自动触发初始化，防止"未初始化"错误
- 配置就绪后才确定路径，保证 workdir 的正确性

### 6.4 工具注册编排

`tools/__init__.py` 显式注册所有 13 个工具到 `default_registry`：

| 分类 | 工具 |
|------|------|
| 基础工具 | `BashTool`, `ReadTool`, `WriteTool`, `EditTool`, `SkillTool`, `BgTool` |
| 任务工具 | `TaskCreateTool`, `TaskGetTool`, `TaskListTool`, `TaskUpdateTool` |
| 压缩工具 | `CompactTool` |
| 委派工具 | `DelegateTool` |

`setup_delegate()` 延迟初始化子 Agent，避免模块加载时的循环导入。

---

## 七、后续规划（TODO）

- [ ] **对话持久化存储**：将对话历史保存到数据库（SQLite/JSON），支持会话恢复和回放
- [ ] **后台任务增强**：支持任务取消、任务优先级、任务超时重试
- [ ] **权限管理**：扩展 safe_path 为交互式权限确认机制
- [ ] **多类型子 Agent**：注册不同工具集的子 Agent（代码审查、测试等），按场景委派
- [ ] **配置热重载**：支持运行时修改配置无需重启
- [ ] **错误恢复**：后台任务异常时的重试策略，日志系统故障时的降级方案
