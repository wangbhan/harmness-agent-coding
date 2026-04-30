import os

from openai import OpenAI

from internal.Agent.config import get_config


def _create_client():
    cfg = get_config().llm
    api_key = os.environ.get(cfg.api_key_env, cfg.api_key)
    if not api_key:
        raise ValueError(
            f"API Key 未配置：请设置环境变量 {cfg.api_key_env} 或在配置文件中填写 llm.api_key"
        )
    return OpenAI(base_url=cfg.base_url, api_key=api_key)


client = _create_client()
