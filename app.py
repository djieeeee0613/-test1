from flask import Flask, request, jsonify, render_template
import google.generativeai as genai
import requests
import json

# ==========================================
# 1. 系統設定與 API 金鑰
# ==========================================
# ⚠️ 請確保引號內沒有任何空白或換行！
genai.configure(api_key="AIzaSyCAS4voFOEflGrgyroPqNN0BNfSx-HmciU")

# ==========================================
# 2. 定義 AI 工具 (Function Calling)
# ==========================================

# 工具一：查詢空擋
def check_availability(artisan_name: str, date: str) -> str:
    """查詢特定南投職人或店家的預約空擋狀態。"""
    print(f"👉 [系統] 查詢空檔：{artisan_name} 於 {date}")
    if "阿明" in artisan_name:
        return f"{artisan_name} 在 {date} 還有 2 個 VIP 名額！"
    else:
        return f"不好意思，{artisan_name} 在 {date} 已經預約客滿了。"

# 工具二：發送訂單給 n8n
def book_experience(artisan_name: str, date: str, pax: int) -> str:
    """當旅客確定要預約行程時，呼叫此工具。"""
    print(f"🚀 [系統] 發送預約單給 n8n！ 目標：{artisan_name}, 日期：{date}, 人數：{pax}")
    n8n_webhook_url = "http://localhost:5678/webhook/nantou-booking"
    payload = {
        "artisan": artisan_name,
        "booking_date": date,
        "people_count": pax,
        "status": "new_booking"
    }
    try:
        response = requests.post(n8n_webhook_url, json=payload)
        if response.status_code == 200:
            return "預約資料已成功送出給系統後台！"
        else:
            return "預約發送失敗，請稍後再試。"
    except Exception as e:
        print(f"n8n 連線錯誤：{e}")
        return "預約系統連線異常。"

# 工具三：搜尋推薦商家
def search_local_merchants(location: str = "埔里", category: str = "特色商家") -> str:
    """當旅客請你「推薦商家」或「不知道去哪」時，呼叫此工具。"""
    print(f"🔎 [系統] 啟動雷達搜尋：地點={location}, 類別={category}")
    return """
    找到以下優質商家：
    1. 台灣惠蓀咖啡埔里品牌館：推廣台灣在地農作咖啡豆，空間設計極具森林永續感。
    2. 阿明老闆的手沖咖啡：2023冠軍，招牌百香果香氣淺焙，具備極高在地文化適當性。
    3. 玉山星空酒莊：原住民傳統釀酒技術，可體驗梅園生態走讀。
    """

# ==========================================
# 3. 初始化 AI 模型與工具
# ==========================================
model = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    tools=[check_availability, book_experience, search_local_merchants]
)

# ==========================================
# 4. Flask 伺服器架構
# ==========================================
app = Flask(__name__)

def read_secret_manual():
    try:
        with open('nantou_data.txt', 'r', encoding='utf-8') as file:
            return file.read()
    except FileNotFoundError:
        return "警告：找不到 nantou_data.txt 秘笈檔案！"

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/chat', methods=['POST'])
def chat():
    user_data = request.json
    user_message = user_data.get('message', '')

    try:
        secret_data = read_secret_manual()
        
        # 👑 核心：霸道總裁版的推銷員指令
        system_instruction = f"""
        你是一個南投在地永續旅遊嚮導。請嚴格根據以下秘笈回答：
        {secret_data}
        
        【工具使用規則與對話流程】：
        1. 🌟 主動推薦商家（嚴格禁止反問）：
           當旅客說「推薦商家」、「有什麼好玩的」、「找餐廳」，請「直接」呼叫 search_local_merchants 工具。
           ⚠️ 絕對不要反問旅客想要的範圍、預算或類型！如果旅客沒有指明地點，請預設地點為「埔里」，並直接給出你認為最棒的在地推薦！
           
        2. 🌟 主動引導預約（必備結語）：
           在每次向旅客介紹完推薦的商家後，⚠️ 你必須在回覆的最後一段主動加上這句話（或類似語氣）：
           「如果您對上述行程有興趣，我可以協助您直接預訂喔！請問您預計想安排在【哪一天】，以及【總共幾個人】前往呢？」

        3. 查詢與執行預約：
           如果旅客提供「名字、日期、人數」要預約，請直接呼叫 book_experience 工具下單。絕對不要推託自己是AI無法下單。
        """
        
        chat_session = model.start_chat(enable_automatic_function_calling=True)
        full_prompt = system_instruction + "\n\n旅客問：" + user_message
        
        response = chat_session.send_message(full_prompt)
        bot_reply = response.text

    except Exception as e:
        print(f"發生錯誤：{e}")
        bot_reply = "不好意思，我的大腦暫時連線失敗了，請稍後再試。"

    return jsonify({"reply": bot_reply})

if __name__ == '__main__':
    app.run(debug=True, port=5001)