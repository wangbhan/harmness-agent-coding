import os

from internal.Agent.config import get_config
from internal.Agent.llm_adapter import LLMAdapter


def _create_adapter() -> LLMAdapter:
    cfg = get_config().llm
    api_key = os.environ.get(cfg.api_key_env, cfg.api_key)
    if not api_key:
        raise ValueError(
            f"API Key 未配置：请设置环境变量 {cfg.api_key_env} 或在配置文件中填写 llm.api_key"
        )
    provider = cfg.provider
    if provider == "anthropic":
        import anthropic
        raw_client = anthropic.Anthropic(api_key=api_key)
    else:
        from openai import OpenAI
        raw_client = OpenAI(base_url=cfg.base_url, api_key=api_key)
    return LLMAdapter(raw_client, provider)


client = _create_adapter()
