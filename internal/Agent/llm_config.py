import os

from openai import OpenAI

key = os.environ["ZAI_API_KEY"]

client = OpenAI(
    base_url="https://api.z.ai/api/coding/paas/v4",
    api_key=key
)