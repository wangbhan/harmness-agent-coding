import os

import anthropic

from internal.Agent.config import get_config


def _create_client() -> anthropic.Anthropic:
    cfg = get_config().llm
    api_key = os.environ.get(cfg.api_key_env, cfg.api_key)
    if not api_key:
        raise ValueError(
            f"API Key 未配置：请设置环境变量 {cfg.api_key_env} 或在配置文件中填写 llm.api_key"
        )
    kwargs = {"api_key": api_key}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return anthropic.Anthropic(**kwargs)


client = _create_client()