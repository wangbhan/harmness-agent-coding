"""
配置加载器
支持 config.yaml（模板） + config.yaml.local（本地覆盖） + 环境变量覆盖
"""
import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


# ============================================================
# Pydantic 配置模型
# ============================================================

class LLMConfig(BaseModel):
    api_key_env: str = "ZAI_API_KEY"
    api_key: str = ""
    base_url: str = "https://api.z.ai/api/coding/paas/v4"
    default_model: str = "glm-5.1"
    default_max_tokens: int = 8000


class PathsConfig(BaseModel):
    workdir: str = ""
    transcripts_dir: str = ".transcripts"
    logs_dir: str = ".logs/sessions"
    tasks_dir: str = ".tasks"
    skills_dir: str = ""


class CompactConfig(BaseModel):
    keep_recent: int = 3
    preserve_result_tools: list[str] = Field(default_factory=lambda: ["read", "todo"])
    threshold: int = 50000
    model: str = "glm-5.1"
    conversation_slice: int = 80000
    max_tokens: int = 2000


class LogConfig(BaseModel):
    level: str = "DEBUG"
    max_args_info: int = 500
    max_args_debug: int = 10000
    max_result_info: int = 500
    max_result_debug: int = 10000
    max_reply_len: int = 2000


class BashToolConfig(BaseModel):
    dangerous_commands: list[str] = Field(
        default_factory=lambda: ["rm -rf /", "sudo", "reboot", "shutdown"]
    )
    timeout: int = 120
    encoding: str = "utf-8"
    max_output_len: int = 5000
    bg_timeout: int = 600
    bg_max_concurrent: int = 5


class ReadToolConfig(BaseModel):
    max_content_len: int = 50000


class SubAgentConfig(BaseModel):
    max_result_len: int = 8000


class TodoToolConfig(BaseModel):
    max_tasks: int = 20


class ToolsConfig(BaseModel):
    bash: BashToolConfig = Field(default_factory=BashToolConfig)
    read: ReadToolConfig = Field(default_factory=ReadToolConfig)
    sub_agent: SubAgentConfig = Field(default_factory=SubAgentConfig)
    todo: TodoToolConfig = Field(default_factory=TodoToolConfig)


class AgentConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    compact: CompactConfig = Field(default_factory=CompactConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)


# ============================================================
# 配置加载
# ============================================================

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent

_config_instance: Optional[AgentConfig] = None
_config_loaded: bool = False

# 环境变量 → 配置路径映射
_ENV_MAP = {
    "ZAI_API_KEY": ("llm", "api_key"),
    "ZAI_BASE_URL": ("llm", "base_url"),
    "AGENT_MODEL": ("llm", "default_model"),
    "AGENT_MAX_TOKENS": ("llm", "default_max_tokens"),
    "AGENT_LOG_LEVEL": ("log", "level"),
    "AGENT_WORKDIR": ("paths", "workdir"),
}

# 需要 int 转换的配置键
_INT_KEYS = {"default_max_tokens"}


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_overrides(config_dict: dict) -> dict:
    for env_var, path_tuple in _ENV_MAP.items():
        value = os.environ.get(env_var)
        if value is None:
            continue
        d = config_dict
        for key in path_tuple[:-1]:
            d = d.setdefault(key, {})
        target_key = path_tuple[-1]
        if target_key in _INT_KEYS:
            try:
                value = int(value)
            except ValueError:
                continue
        d[target_key] = value
    return config_dict


def init_config(config_dir: Path | None = None):
    """加载并合并配置（通常由 get_config 自动调用，也可手动调用指定 config_dir）"""
    global _config_instance, _config_loaded

    base_dir = config_dir or _CONFIG_DIR

    base = _load_yaml(base_dir / "config.yaml")
    local = _load_yaml(base_dir / "config.yaml.local")
    merged = _deep_merge(base, local)
    merged = _apply_env_overrides(merged)

    _config_instance = AgentConfig(**merged)
    _config_loaded = True


def get_config() -> AgentConfig:
    """获取配置，首次调用时自动加载"""
    if not _config_loaded:
        init_config()
    return _config_instance
