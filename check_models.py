import google.generativeai as genai

# 換成你的 API Key
genai.configure(api_key="AIzaSyCAS4voFOEflGrgyroPqNN0BNfSx-HmciU")

print("你的 API Key 支援以下可以用來聊天的模型：")
# 叫 Google 列出所有模型
for m in genai.list_models():
    if 'generateContent' in m.supported_generation_methods:
        print(m.name)