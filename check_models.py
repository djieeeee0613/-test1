from dotenv import load_dotenv
from google import genai
import os

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

print("你的 API Key 支援以下可以用來聊天的模型：")
for m in client.models.list():
    if hasattr(m, 'supported_actions') and 'generateContent' in (m.supported_actions or []):
        print(m.name)
    elif hasattr(m, 'name'):
        print(m.name)
