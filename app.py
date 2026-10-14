import os
import sys
import json
import uuid
import zipfile
import re
import time
import random
import traceback
import requests
import base64
from datetime import datetime, date, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, render_template_string, Response
from functools import wraps
from google import genai
from merchants_data import CATEGORIES, CATEGORY_ORDER
from google.genai import types

load_dotenv()
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# ==========================================
# 0. 資料載入
# ==========================================
def load_docx_text(path: str) -> str:
    with zipfile.ZipFile(path, 'r') as z:
        with z.open('word/document.xml') as f:
            xml = f.read().decode('utf-8')
    text = re.sub(r'<[^>]+>', ' ', xml)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

DOCX_PATH = os.path.join(os.path.dirname(__file__), '南投深度游資源 (1).docx')
MERCHANT_DATA = load_docx_text(DOCX_PATH)
print(f"✅ 已載入職人資料庫，共 {len(MERCHANT_DATA)} 字")

BOOKINGS_FILE = os.path.join(os.path.dirname(__file__), 'bookings.json')
PLANS = {}  # plan_id -> {text, created_at}

# 速率限制：每個 IP 每分鐘最多 20 次
_rate = {}
def check_rate(ip, limit=20, window=60):
    now = time.time()
    hits = [t for t in _rate.get(ip, []) if now - t < window]
    hits.append(now)
    _rate[ip] = hits
    return len(hits) <= limit

def load_bookings():
    if os.path.exists(BOOKINGS_FILE):
        with open(BOOKINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def load_bookings_from_supabase():
    """從 Supabase 讀取預約記錄，失敗時 fallback 到本機 JSON"""
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bookings",
            params={"select": "*", "order": "id.desc"},
            headers={"apikey": SUPABASE_KEY},
            timeout=8
        )
        if resp.status_code == 200:
            rows = resp.json()
            result = []
            for r in rows:
                created = r.get('created_at', '')
                timestamp = created[:16].replace('T', ' ') if created else '—'
                result.append({
                    'id': r.get('booking_id', str(r.get('id', ''))),
                    'timestamp': timestamp,
                    'artisan': r.get('artisan', ''),
                    'date': r.get('booking_date', ''),
                    'pax': r.get('people_count', ''),
                    'customer': r.get('customer_name', ''),
                    'phone': r.get('phone', ''),
                    'email': r.get('email', ''),
                    'status': r.get('status', '待確認'),
                    'rating': r.get('rating'),
                    'rating_comment': r.get('rating_comment', ''),
                })
            return result
    except Exception as e:
        print(f"❌ Supabase 讀取預約失敗：{e}")
    return load_bookings()

def save_bookings(bookings):
    with open(BOOKINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(bookings, f, ensure_ascii=False, indent=2)

def log_booking(data: dict) -> str:
    bookings = load_bookings()
    booking_id = str(uuid.uuid4())[:8].upper()
    data['id'] = booking_id
    data['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    data['status'] = '待確認'
    data['rating'] = None
    data['rating_comment'] = ''
    bookings.append(data)
    save_bookings(bookings)
    return booking_id

# ==========================================
# 1. 設定
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("請在 .env 檔案中設定 GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "https://nantoutravel.app.n8n.cloud/webhook/nantou-booking")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://gbffodvtfirdtecsespm.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
MERCHANT_PASSWORD = os.getenv("MERCHANT_PASSWORD", "nantou2026")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
app = Flask(__name__)

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.password != MERCHANT_PASSWORD:
            return Response("請輸入密碼", 401,
                {'WWW-Authenticate': 'Basic realm="商家後台"'})
        return f(*args, **kwargs)
    return decorated

# ==========================================
# 2. AI 工具函式
# ==========================================
WMO_CODES = {
    0: "晴天 ☀️", 1: "大致晴天 🌤", 2: "部分多雲 ⛅", 3: "多雲 ☁️",
    45: "有霧 🌫", 48: "濃霧 🌫",
    51: "毛毛雨 🌦", 53: "毛毛雨 🌦", 55: "毛毛雨 🌦",
    61: "小雨 🌧", 63: "中雨 🌧", 65: "大雨 🌧",
    80: "陣雨 🌦", 81: "陣雨 🌦", 82: "強陣雨 ⛈",
    95: "雷雨 ⛈", 96: "強雷雨 ⛈"
}

def get_nantou_weather() -> str:
    """取得南投縣今明兩天天氣預報，用於建議適合的戶外或室內體驗"""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 23.96, "longitude": 120.97,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
                "timezone": "Asia/Taipei", "forecast_days": 2
            },
            timeout=8
        )
        d = r.json()["daily"]
        lines = []
        for i, date in enumerate(d["time"]):
            label = "今天" if i == 0 else "明天"
            desc = WMO_CODES.get(d["weathercode"][i], "未知")
            lines.append(
                f"{label}（{date}）：{desc}，"
                f"氣溫 {d['temperature_2m_min'][i]}～{d['temperature_2m_max'][i]}°C，"
                f"降雨機率 {d['precipitation_probability_max'][i]}%"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"天氣資料暫時無法取得：{e}"

def _parse_booking_date(date_str: str):
    """嘗試解析各種日期格式，回傳 date 物件；解析失敗回傳 None"""
    date_str = date_str.strip()
    # 含年份的格式（優先嘗試）
    full_year_formats = [
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
        "%Y年%m月%d日", "%Y年%m月%d號",
    ]
    # 不含年份的格式（預設今年，年底自動跳明年）
    no_year_formats = [
        "%m-%d", "%m/%d",
        "%m月%d日", "%m月%d號",
    ]
    today = _today_tw()

    for fmt in full_year_formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue

    for fmt in no_year_formats:
        try:
            d = datetime.strptime(date_str, fmt).date().replace(year=today.year)
            # 如果補今年後日期已過去，自動跳明年
            if d < today:
                d = d.replace(year=today.year + 1)
            return d
        except ValueError:
            continue

    return None

def _today_tw() -> date:
    """回傳台灣時間今天的日期"""
    return datetime.now(timezone(timedelta(hours=8))).date()

def _check_date_not_past(date_str: str) -> str | None:
    """若日期已過去，回傳錯誤訊息；否則回傳 None"""
    d = _parse_booking_date(date_str)
    if d is None:
        return None
    today = _today_tw()
    if d < today:
        return (
            f"❌ 無法預約過去的日期（{date_str}）。"
            f"今天是 {today.strftime('%Y年%m月%d日')}，請選擇今天或之後的日期。"
        )
    return None

def check_availability(artisan_name: str, date: str) -> str:
    """查詢特定職人空檔"""
    print(f"👉 查詢空檔：{artisan_name} 於 {date}")
    err = _check_date_not_past(date)
    if err:
        return err
    return f"{artisan_name} 在 {date} 預約查詢中，目前狀態正常。"

def book_experience(artisan_name: str, date: str, pax: int,
                    customer_name: str, phone: str, email: str) -> str:
    """發送訂單給 n8n，包含旅客聯絡資訊"""
    err = _check_date_not_past(date)
    if err:
        return err
    print(f"🚀 發送預約單：{artisan_name}, {date}, {pax}人, 旅客：{customer_name}")
    booking_id = log_booking({
        "artisan": artisan_name, "date": date, "pax": pax,
        "customer": customer_name, "phone": phone, "email": email,
    })
    payload = {
        "artisan": artisan_name, "booking_date": date, "people_count": pax,
        "customer_name": customer_name, "phone": phone, "email": email,
        "booking_id": booking_id, "status": "new_booking"
    }
    try:
        response = requests.post(N8N_WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code == 200:
            return f"預約資料已成功送出！您的預約編號為 **{booking_id}**。"
        return f"系統錯誤 ({response.status_code})，預約編號 {booking_id} 已暫存。"
    except Exception as e:
        return f"連線異常（預約已記錄，編號 **{booking_id}**）：{str(e)}"

def search_local_merchants(location: str = "南投", budget: int = 0) -> str:
    """搜尋南投在地推薦商家與體驗，budget 為每人預算上限（元），0 表示不限"""
    blocks = re.split(r'\n(?=\d+[\.、]|\【)', MERCHANT_DATA)
    blocks = [b.strip() for b in blocks if b.strip()]
    if budget > 0:
        filtered = []
        for b in blocks:
            prices = re.findall(r'(\d[\d,]*)\s*元', b)
            if not prices or any(int(p.replace(',', '')) <= budget for p in prices):
                filtered.append(b)
        if filtered:
            blocks = filtered
    random.shuffle(blocks)
    return "\n\n".join(blocks)

# ==========================================
# 3. 模型設定與系統提示
# ==========================================
MODEL_NAME = 'models/gemini-2.5-flash'
FALLBACK_MODEL = 'models/gemini-2.0-flash'

BASE_SYSTEM_INSTRUCTION = """
你是南投在地永續旅遊 AI 嚮導，說話親切、熱情，熟悉當地所有職人與體驗資源。

【重要】每次呼叫 search_local_merchants 後，資料庫回傳的商家順序都是隨機排列的。請優先從排在前面的商家中挑選推薦，不要每次都推薦相同的商家，讓每位旅客看到不同的驚喜組合。

【多語言】旅客如果用英文或日文溝通，請全程用對應語言回答。

【天氣整合】如果旅客詢問行程規劃，主動先呼叫 get_nantou_weather 取得天氣，在行程中加入天氣提醒（如：今天有雨，建議攜帶雨具；天氣晴朗適合戶外部落體驗）。

【預算篩選】如果旅客提到「預算」或「多少錢以內」，呼叫 search_local_merchants 時帶入 budget 參數（整數，單位元）。

---

【互動選項格式】
當你需要詢問以下資訊時，必須在問句後加上 [選項: 選項1 | 選項2 | ...] 讓旅客點選，不得只用文字敘述：
- 人數 → [選項: 1-2人 | 3-4人 | 5-6人 | 7-8人 | 8人以上]
- 每人預算 → [選項: 1000元以下 | 1000-2000元 | 2000-3000元 | 3000元以上]
- 感興趣的區域 → [選項: 日月潭周邊 | 清境合歡山 | 埔里小鎮 | 竹山鹿谷 | 國姓咖啡公路 | 信義原民部落 | 其他]
日期讓旅客自由文字輸入，不加選項。

---

【A. 旅客詢問行程規劃】
觸發條件：旅客說「幫我規劃行程」、「一日遊」、「行程推薦」、「怎麼安排」等。

1. 先呼叫 get_nantou_weather 取得天氣。
2. 再呼叫 search_local_merchants 取得資料庫內容（若旅客指定預算，帶入 budget 參數）。
3. 根據天氣安排早午晚三個時段，格式如下：

---
🗺️ **南投一日深度體驗行程**

🌅 **上午｜9:00 - 12:00**
**商家名稱**
📍 地點｜一句話說明為何早上適合來這裡

🌞 **下午｜13:00 - 17:00**
**商家名稱**
📍 地點｜一句話說明為何下午適合來這裡

🌙 **傍晚／夜間｜18:00 以後**
**商家名稱**
📍 地點｜一句話說明為何傍晚適合來這裡

🌤 **天氣提醒**：（根據當天天氣給出實用提醒，例如是否帶傘、防曬）
---

4. 行程結尾加上：「✨ 以上是為您量身打造的南投一日行程！請問您對哪個時段的體驗最感興趣，想進一步預約嗎？」

---

【B. 旅客詢問分類推薦】
觸發條件：旅客說「有什麼推薦」、「好玩的地方」、「推薦商家」等。

1. 先呼叫 search_local_merchants 取得資料庫內容。
2. 依類別各挑最多 3 個，格式如下：

---
🌿 **南投精選體驗推薦**

☕ **職人體驗**
1. **商家名稱** — 一句話亮點

🌾 **農業休閒**
1. **商家名稱** — 一句話亮點

♨️ **秘境溫泉**
1. **商家名稱** — 一句話亮點

🍽️ **永續餐飲**
1. **商家名稱** — 一句話亮點

🏡 **特色住宿**
1. **商家名稱** — 一句話亮點

🏔️ **部落文化**
1. **商家名稱** — 一句話亮點
---

3. 結尾加上：「✨ 請問您對哪一家最感興趣？告訴我店家名稱，我就幫您安排預約！」

---

【C. 旅客點擊分類卡片】
觸發條件：訊息以「【分類探索】」開頭。

1. 呼叫 search_local_merchants 取得完整資料庫。
2. 從結果中篩選出與該分類最相關的商家：
   - ☕ 咖啡職人 → 咖啡、手沖、烘焙、莊園相關
   - 🌾 農業休閒 → 茶園、農場、採茶、有機、梅園相關
   - ♨️ 秘境溫泉 → 溫泉、湯屋、泡湯相關
   - 🍽️ 永續餐飲 → 餐廳、料理、蔬食、風味餐相關
   - 🏡 特色住宿 → 民宿、住宿、露營、帳篷相關
   - 🏔️ 部落文化 → 部落、原住民、族、獵人相關
   - 🎨 工藝手作 → 編織、陶藝、染布、木工、手作相關
3. 列出 3～5 個最相關的商家，格式如下：

---
[類別 emoji] **[類別名稱] 精選商家**

**1. [商家名稱]**
📍 [地點] ｜ [一句話亮點]
💰 [價格]（若資料庫有提供）

**2. [商家名稱]**
...以此類推
---

4. 結尾固定加上：「✨ 以上是[類別名稱]的精選商家！請問您對哪一家最感興趣？告訴我店家名稱，我可以幫您查詢空檔、安排預約，或規劃整天行程 😊」

---

【預約流程】

**第一階段：確認預約內容**
- 旅客說出想預約的店家後，若缺少日期或人數，分開詢問：
  - 詢問人數時，必須加上選項按鈕：「請問幾位參加？\n[選項: 1-2人 | 3-4人 | 5-6人 | 7-8人 | 8人以上]」
  - 詢問日期時，讓旅客自由輸入文字，不加選項。
  - 若旅客在第一句話已提供人數或日期，跳過對應詢問。
- **【絕對禁止】旅客提供店家名稱時，絕對不可呼叫 search_local_merchants 去驗證該名稱是否存在。不論店家名稱是否出現在推薦資料庫中，一律直接進入預約流程。推薦資料庫只用於「旅客要求推薦」的情境，不用於驗證預約對象。**
- **【日期傳遞規則】呼叫 check_availability 或 book_experience 時，日期參數必須原樣傳入旅客提供的字串。旅客說「6月29日」就傳「6月29日」，旅客說「6/29」就傳「6/29」。絕對不可自行推斷或補上年份（例如不可改成「2024-06-29」或「2025-06-29」），年份由系統自動處理。**
- 日期必須是今天或之後的日期。若系統回覆日期已過去，立即告知旅客並請重新選擇日期，不得繼續詢問聯絡資料。
- 確認後，發送以下提示請旅客填寫聯絡資料：

「📋 最後一步！請您用以下格式傳送聯絡資料：

姓名：王小明
電話：0912-345-678
Email：example@email.com

直接複製上方格式填入即可 😊」

**第二階段：收到聯絡資料後完成預約**
- 從訊息中擷取姓名、電話、Email。
- 立刻呼叫 book_experience 完成預約。
- 預約成功後回覆：「🎉 已成功為您預約 [店家名稱]！您的預約編號：[booking_id]。商家收到通知後會與您聯繫確認，期待您的南投之旅！」
"""

# ==========================================
# 4. Flask 路由
# ==========================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/category/<slug>')
def category_page(slug):
    cat = CATEGORIES.get(slug)
    if not cat:
        return "<h2 style='font-family:sans-serif;text-align:center;margin-top:80px'>找不到此分類</h2>", 404
    cats = [CATEGORIES[s] for s in CATEGORY_ORDER]
    return render_template('category.html', cat=cat, categories=cats)

@app.route('/api/events')
def get_events():
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/events",
            params={"active": "eq.true", "select": "*", "order": "id.desc", "limit": "10"},
            headers={"apikey": SUPABASE_KEY},
            timeout=8
        )
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify([])
    except Exception:
        return jsonify([])

@app.route('/webhook/line-merchant', methods=['POST', 'GET'])
def line_merchant_proxy():
    try:
        resp = requests.post(
            os.getenv('N8N_LINE_WEBHOOK_URL', 'https://nantoutravel.app.n8n.cloud/webhook/line-merchant'),
            json=request.get_json(silent=True),
            headers={'Content-Type': 'application/json'},
            timeout=10
        )
        return jsonify(resp.json() if resp.content else {}), resp.status_code
    except Exception as e:
        print(f"❌ LINE webhook 代理錯誤：{e}")
        return jsonify({}), 200

# --- 行程分享 ---
@app.route('/api/share-plan', methods=['POST'])
def share_plan():
    plan_text = request.json.get('plan', '')
    plan_id = str(uuid.uuid4())[:8]
    PLANS[plan_id] = {'text': plan_text, 'created_at': datetime.now().strftime('%Y-%m-%d %H:%M')}
    return jsonify({"plan_id": plan_id, "url": f"/plan/{plan_id}"})

@app.route('/plan/<plan_id>')
def view_plan(plan_id):
    plan = PLANS.get(plan_id)
    if not plan:
        return "<h2 style='font-family:sans-serif;text-align:center;margin-top:80px'>行程不存在或已過期</h2>", 404
    import html as html_mod
    text = html_mod.escape(plan['text'])
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'^---$', '<hr>', text, flags=re.MULTILINE)
    text = text.replace('\n', '<br>')
    return render_template_string("""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>南投旅遊行程 {{ plan_id }}</title>
<style>
  body { font-family:'Helvetica Neue',Arial,sans-serif; background:#F4F1EA; margin:0; padding:20px; }
  .card { max-width:600px; margin:40px auto; background:white; border-radius:20px; padding:36px; box-shadow:0 8px 32px rgba(0,0,0,0.1); }
  .logo { color:#4A5D4E; font-size:14px; font-weight:bold; letter-spacing:2px; margin-bottom:24px; }
  h2 { color:#333; font-size:22px; margin-bottom:6px; }
  .meta { color:#999; font-size:13px; margin-bottom:28px; }
  .content { line-height:1.9; color:#333; font-size:15px; }
  hr { border:none; border-top:1px solid #eee; margin:18px 0; }
  .footer { text-align:center; margin-top:36px; padding-top:24px; border-top:1px solid #f0f0f0; }
  .btn { background:#4A5D4E; color:white; padding:13px 32px; border-radius:26px; text-decoration:none; font-weight:bold; font-size:15px; display:inline-block; }
  .btn:hover { background:#38473b; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">🌿 南投職人旅遊平台</div>
  <h2>我的南投深度行程</h2>
  <div class="meta">建立時間：{{ created_at }} ｜ 行程編號：{{ plan_id }}</div>
  <div class="content">{{ content|safe }}</div>
  <div class="footer">
    <a href="/" class="btn">回到官網預約體驗 →</a>
  </div>
</div>
</body>
</html>""", plan_id=plan_id, created_at=plan['created_at'], content=text)

# --- 商家管理後台 ---
@app.route('/merchant')
@require_auth
def merchant_dashboard():
    bookings = load_bookings_from_supabase()
    rows = ""
    status_colors = {"待確認": "#f59e0b", "已確認": "#10b981", "已婉拒": "#ef4444"}
    for b in reversed(bookings):
        color = status_colors.get(b.get("status", ""), "#6b7280")
        stars = "⭐" * int(b["rating"]) if b.get("rating") else "—"
        rows += f"""<tr>
            <td><code>{b.get('id','')}</code></td>
            <td>{b.get('timestamp','')}</td>
            <td><strong>{b.get('artisan','')}</strong></td>
            <td>{b.get('date','')}</td>
            <td style="text-align:center">{b.get('pax','')}人</td>
            <td>{b.get('customer','')}</td>
            <td>{b.get('phone','')}</td>
            <td style="color:{color};font-weight:bold">{b.get('status','')}</td>
            <td>{stars}</td>
            <td style="font-size:12px;color:#999">{b.get('rating_comment','')}</td>
        </tr>"""
    total = len(bookings)
    confirmed = sum(1 for b in bookings if b.get('status') == '已確認')
    pending = sum(1 for b in bookings if b.get('status') == '待確認')
    avg_rating = None
    rated = [b['rating'] for b in bookings if b.get('rating')]
    if rated:
        avg_rating = round(sum(rated) / len(rated), 1)
    return render_template_string("""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>南投職人平台 — 商家後台</title>
<style>
  :root{--primary:#4A5D4E;}
  *{margin:0;padding:0;box-sizing:border-box;}
  body{font-family:'Helvetica Neue',Arial,sans-serif;background:#f5f5f0;color:#333;}
  header{background:var(--primary);color:white;padding:18px 32px;display:flex;justify-content:space-between;align-items:center;}
  header h1{font-size:18px;letter-spacing:1px;}
  .refresh-btn{background:rgba(255,255,255,0.15);color:white;border:1px solid rgba(255,255,255,0.3);padding:6px 16px;border-radius:16px;cursor:pointer;font-size:13px;}
  .stats{display:flex;gap:16px;padding:24px 32px;}
  .stat{background:white;border-radius:14px;padding:20px 28px;flex:1;box-shadow:0 2px 10px rgba(0,0,0,0.06);}
  .stat .num{font-size:38px;font-weight:900;color:var(--primary);line-height:1;}
  .stat .num.green{color:#10b981;}
  .stat .num.amber{color:#f59e0b;}
  .stat .num.star{color:#f59e0b;}
  .stat .label{font-size:13px;color:#888;margin-top:6px;}
  .wrap{margin:0 32px 32px;background:white;border-radius:14px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.06);}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th{background:var(--primary);color:white;padding:11px 14px;text-align:left;font-weight:600;white-space:nowrap;}
  td{padding:11px 14px;border-bottom:1px solid #f3f3f0;}
  tr:hover td{background:#fafaf7;}
  code{background:#f0f0e8;padding:2px 6px;border-radius:4px;font-size:12px;}
  .empty{text-align:center;padding:48px;color:#aaa;font-size:15px;}
</style>
</head>
<body>
<header>
  <h1>🌿 南投職人旅遊平台 — 商家管理後台</h1>
  <button class="refresh-btn" onclick="location.reload()">⟳ 刷新</button>
</header>
<div class="stats">
  <div class="stat"><div class="num">{{ total }}</div><div class="label">總預約數</div></div>
  <div class="stat"><div class="num green">{{ confirmed }}</div><div class="label">已確認</div></div>
  <div class="stat"><div class="num amber">{{ pending }}</div><div class="label">待確認</div></div>
  <div class="stat"><div class="num star">{{ avg_rating or '—' }}</div><div class="label">平均評分（滿 5 分）</div></div>
</div>
<div class="wrap">
{% if rows %}
<table>
  <tr><th>編號</th><th>預約時間</th><th>職人商家</th><th>體驗日期</th><th>人數</th><th>旅客姓名</th><th>電話</th><th>狀態</th><th>評分</th><th>旅客評語</th></tr>
  {{ rows|safe }}
</table>
{% else %}
<div class="empty">尚無預約記錄</div>
{% endif %}
</div>
<script>setTimeout(()=>location.reload(), 30000);</script>
</body>
</html>""", rows=rows, total=total, confirmed=confirmed, pending=pending, avg_rating=avg_rating)

# --- 旅客評價頁 ---
@app.route('/rate/<booking_id>')
def rate_page(booking_id):
    bookings = load_bookings()
    booking = next((b for b in bookings if b.get('id') == booking_id), None)
    artisan = booking.get('artisan', '') if booking else ''
    already_rated = bool(booking and booking.get('rating'))
    return render_template_string("""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>體驗評價</title>
<style>
  body{font-family:'Helvetica Neue',Arial,sans-serif;background:#F4F1EA;min-height:100vh;display:flex;align-items:center;justify-content:center;}
  .card{background:white;border-radius:24px;padding:44px 36px;max-width:420px;width:90%;box-shadow:0 8px 40px rgba(0,0,0,0.1);text-align:center;}
  .icon{font-size:48px;margin-bottom:16px;}
  h2{color:#4A5D4E;font-size:22px;margin-bottom:8px;}
  .sub{color:#666;font-size:14px;line-height:1.6;margin-bottom:32px;}
  .stars{display:flex;justify-content:center;gap:8px;margin-bottom:24px;}
  .star{font-size:44px;cursor:pointer;opacity:0.25;transition:opacity 0.15s,transform 0.1s;}
  .star.lit{opacity:1;}
  .star:hover{transform:scale(1.15);}
  textarea{width:100%;border:1.5px solid #ddd;border-radius:14px;padding:14px;font-size:14px;resize:none;outline:none;margin-bottom:20px;font-family:inherit;color:#333;}
  textarea:focus{border-color:#4A5D4E;}
  .btn{background:#4A5D4E;color:white;border:none;padding:14px 0;border-radius:26px;font-size:16px;cursor:pointer;width:100%;font-weight:bold;transition:background 0.2s;}
  .btn:hover{background:#38473b;}
  .success{color:#10b981;font-size:18px;font-weight:bold;margin-top:20px;display:none;}
  .already{color:#10b981;font-size:16px;margin-top:16px;}
</style>
</head>
<body>
<div class="card">
  <div class="icon">🌿</div>
  {% if already_rated %}
  <h2>已收到您的評價</h2>
  <div class="already">感謝您的回饋！</div>
  {% else %}
  <h2>感謝您的南投體驗！</h2>
  <div class="sub">{{ artisan }}的服務如何？<br>您的回饋幫助更多旅客發現南投的美好<br><small style="color:#bbb">預約編號：{{ booking_id }}</small></div>
  <div class="stars" id="stars">
    <span class="star" data-v="1">⭐</span>
    <span class="star" data-v="2">⭐</span>
    <span class="star" data-v="3">⭐</span>
    <span class="star" data-v="4">⭐</span>
    <span class="star" data-v="5">⭐</span>
  </div>
  <textarea id="comment" rows="3" placeholder="分享您的體驗心得（選填）"></textarea>
  <button class="btn" onclick="submitRating()">送出評價</button>
  <div class="success" id="success">🎉 感謝您的評價，期待再次相遇！</div>
  {% endif %}
</div>
<script>
let rating = 0;
document.querySelectorAll('.star').forEach((s, i) => {
  s.addEventListener('mouseenter', () => highlight(i + 1));
  s.addEventListener('mouseleave', () => highlight(rating));
  s.addEventListener('click', () => { rating = i + 1; highlight(rating); });
});
function highlight(n) {
  document.querySelectorAll('.star').forEach((s, i) => s.classList.toggle('lit', i < n));
}
function submitRating() {
  if (!rating) { alert('請選擇星級'); return; }
  fetch('/api/rate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ booking_id: '{{ booking_id }}', rating, comment: document.getElementById('comment').value })
  }).then(() => {
    document.getElementById('success').style.display = 'block';
    document.querySelector('.btn').style.display = 'none';
    document.getElementById('stars').style.pointerEvents = 'none';
  });
}
</script>
</body>
</html>""", booking_id=booking_id, artisan=artisan, already_rated=already_rated)

@app.route('/api/rate', methods=['POST'])
def submit_rate():
    data = request.json
    bookings = load_bookings()
    for b in bookings:
        if b.get('id') == data.get('booking_id'):
            b['rating'] = data.get('rating')
            b['rating_comment'] = data.get('comment', '')
            break
    save_bookings(bookings)
    return jsonify({"ok": True})

# ==========================================
# GAME — 緣投探索者
# ==========================================

ALL_MERCHANT_NAMES = sorted(m['name'] for s in CATEGORY_ORDER for m in CATEGORIES[s]['merchants'])

LEVEL_THRESHOLDS = [
    (0,    1, "旅人",    "🧳"),
    (100,  2, "探索者",  "🗺️"),
    (300,  3, "職人學徒", "📚"),
    (600,  4, "在地達人", "🌿"),
    (1000, 5, "南投職人", "⭐"),
    (1500, 6, "職人大師", "🏆"),
]

QUIZ_QUESTIONS = [
    {"q": "南投縣哪個鄉鎮以竹藝工藝聞名，素有「竹之鄉」之稱？",
     "options": ["竹山鎮", "集集鎮", "魚池鄉", "鹿谷鄉"], "answer": 0,
     "fact": "竹山鎮林業資源豐富，竹炭、竹編等工藝享譽全台，是台灣重要的竹藝產業重鎮。"},
    {"q": "日月潭位於南投縣哪個鄉？",
     "options": ["水里鄉", "魚池鄉", "信義鄉", "國姓鄉"], "answer": 1,
     "fact": "日月潭位於魚池鄉，是台灣最大的天然湖泊，因北形如日、南形如月而得名。"},
    {"q": "南投縣生產「凍頂烏龍茶」最著名的地區是？",
     "options": ["埔里", "鹿谷", "竹山", "信義"], "answer": 1,
     "fact": "鹿谷鄉凍頂烏龍茶馳名中外，是台灣高山茶的代表，海拔600-1000公尺的茶園造就其獨特風味。"},
    {"q": "「桃米生態村」以哪種生態著稱，是921地震後轉型的代表？",
     "options": ["螢火蟲", "青蛙", "台灣黑熊", "石虎"], "answer": 1,
     "fact": "桃米里位於埔里鎮，擁有台灣29種青蛙中的23種，是921地震後社區重建的典範案例。"},
    {"q": "南投縣最主要的原住民族群是哪一族？",
     "options": ["排灣族", "布農族", "泰雅族", "阿美族"], "answer": 1,
     "fact": "布農族是南投縣最主要的原住民族群，以八部合音、打耳祭聞名，主要分布在信義鄉、仁愛鄉等山區。"},
    {"q": "台灣菸酒南投酒廠（紹興酒）位於哪個地方？",
     "options": ["竹山", "埔里", "魚池", "集集"], "answer": 1,
     "fact": "台灣菸酒南投酒廠位於埔里，利用埔里優質水源釀造紹興酒，已有百年歷史，是埔里的代表性特產。"},
    {"q": "集集綠色隧道由哪種百年老樹構成，全長約5公里？",
     "options": ["樟樹", "楓樹", "茄苳", "台灣欒樹"], "answer": 0,
     "fact": "集集綠色隧道由百年以上的老樟樹構成，是著名的觀光景點，秋天落葉時景色尤為迷人。"},
    {"q": "中台禪寺位於南投縣哪個鄉鎮，是台灣規模最大的佛教寺院之一？",
     "options": ["埔里鎮", "魚池鄉", "草屯鎮", "南投市"], "answer": 0,
     "fact": "中台禪寺位於埔里鎮，由惟覺老和尚創建，融合中西建築美學，每年吸引數十萬信眾朝聖。"},
    {"q": "台灣最高峰玉山主峰海拔多少公尺，位於南投縣信義鄉境內？",
     "options": ["3,286公尺", "3,886公尺", "3,952公尺", "4,020公尺"], "answer": 2,
     "fact": "玉山主峰海拔3952公尺，是台灣最高峰，也是東北亞最高峰，每年吸引無數登山客朝聖。"},
    {"q": "南投縣哪條公路被稱為「咖啡公路」，兩旁遍佈精品咖啡農場？",
     "options": ["台14線", "台16線", "台21線", "縣道131線"], "answer": 3,
     "fact": "縣道131線（國姓鄉）海拔適中、雲霧繚繞，沿線咖啡農場林立，所產的咖啡風味獨特，曾多次獲得國際獎項。"},
    {"q": "南投縣的「水里蛇窯」是台灣最古老的柴燒陶窯，創立於哪個年代？",
     "options": ["清朝末年（1890年代）", "日治初期（1900年代）", "日治中期（1926年）", "光復後（1950年代）"], "answer": 2,
     "fact": "水里蛇窯創立於1926年，是台灣保存最完整的柴燒蛇形窯，見證了台灣陶瓷產業的百年歷史。"},
    {"q": "以下哪個正確描述了「埔里紙業」的特色？",
     "options": ["以稻草為主原料", "利用埔里優質水源製造高品質宣紙", "使用進口原料製造牛皮紙", "以竹纖維製造紙張，已停產"], "answer": 1,
     "fact": "埔里水質清甜、水量豐沛，自古以來即是台灣最重要的製紙重鎮，廣興紙寮等老字號延續傳統手工造紙工藝至今。"},
]

def get_quiz_today():
    day = datetime.now(timezone(timedelta(hours=8))).timetuple().tm_yday
    q = QUIZ_QUESTIONS[day % len(QUIZ_QUESTIONS)]
    return {"question": q["q"], "options": q["options"], "fact": q["fact"], "answer": q["answer"]}

def calc_level_info(xp):
    result = LEVEL_THRESHOLDS[0]
    for t in LEVEL_THRESHOLDS:
        if xp >= t[0]:
            result = t
    next_xp = next((t[0] for t in LEVEL_THRESHOLDS if xp < t[0]), None)
    return {"level": result[1], "title": result[2], "emoji": result[3],
            "next_xp": next_xp, "current_min": result[0], "xp": xp}

def verify_google_token(id_token):
    try:
        resp = requests.get("https://oauth2.googleapis.com/tokeninfo",
                            params={"id_token": id_token}, timeout=8)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if GOOGLE_CLIENT_ID and data.get("aud") != GOOGLE_CLIENT_ID:
            return None
        return {"google_id": data.get("sub"), "name": data.get("name"),
                "email": data.get("email"), "avatar_url": data.get("picture")}
    except Exception as e:
        print(f"❌ Google token error: {e}")
        return None

def _supabase_headers(extra=None):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h

def get_or_create_game_user(google_id, name, email, avatar_url):
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/game_users",
                        params={"google_id": f"eq.{google_id}", "select": "*"},
                        headers=_supabase_headers(), timeout=8)
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    new_user = {"google_id": google_id, "name": name, "email": email,
                "avatar_url": avatar_url, "xp": 0, "level": 1,
                "title": "旅人", "checkin_count": 0}
    resp = requests.post(f"{SUPABASE_URL}/rest/v1/game_users", json=new_user,
                         headers=_supabase_headers({"Prefer": "return=representation"}),
                         timeout=8)
    return resp.json()[0] if resp.status_code in (200, 201) and resp.json() else new_user

def award_xp_to_user(google_id, xp_delta):
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/game_users",
                        params={"google_id": f"eq.{google_id}", "select": "xp"},
                        headers=_supabase_headers(), timeout=8)
    if resp.status_code != 200 or not resp.json():
        return None
    new_xp = resp.json()[0]["xp"] + xp_delta
    level_info = calc_level_info(new_xp)
    requests.patch(f"{SUPABASE_URL}/rest/v1/game_users",
                   params={"google_id": f"eq.{google_id}"},
                   json={"xp": new_xp, "level": level_info["level"], "title": level_info["title"]},
                   headers=_supabase_headers(), timeout=8)
    return level_info

def verify_photo_has_person(image_b64, mime_type="image/jpeg"):
    try:
        image_bytes = base64.b64decode(image_b64)
        response = client.models.generate_content(
            model='models/gemini-2.0-flash',
            contents=types.Content(role='user', parts=[
                types.Part(inline_data=types.Blob(mime_type=mime_type, data=image_bytes)),
                types.Part(text='Does this photo clearly show a person (human face or body visible)? Answer only YES or NO.')
            ])
        )
        return (response.text or "").strip().upper().startswith("YES")
    except Exception as e:
        print(f"❌ Photo verify error: {e}")
        return False

@app.route('/game')
def game_page():
    return render_template('game.html', google_client_id=GOOGLE_CLIENT_ID,
                           all_merchants=ALL_MERCHANT_NAMES)

@app.route('/api/game/login', methods=['POST'])
def game_login():
    id_token = request.json.get('id_token')
    if not id_token:
        return jsonify({"error": "Missing token"}), 400
    user_info = verify_google_token(id_token)
    if not user_info:
        return jsonify({"error": "Invalid token"}), 401
    user = get_or_create_game_user(**user_info)
    return jsonify({"user": user, "level_info": calc_level_info(user.get("xp", 0))})

@app.route('/api/game/user/<google_id>')
def game_get_user(google_id):
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/game_users",
                        params={"google_id": f"eq.{google_id}", "select": "*"},
                        headers=_supabase_headers(), timeout=8)
    if resp.status_code == 200 and resp.json():
        user = resp.json()[0]
        return jsonify({"user": user, "level_info": calc_level_info(user.get("xp", 0))})
    return jsonify({"error": "User not found"}), 404

@app.route('/api/game/checkin', methods=['POST'])
def game_checkin():
    data = request.json
    google_id = data.get('google_id')
    merchant_name = data.get('merchant_name')
    image_b64 = data.get('image')
    if not all([google_id, merchant_name, image_b64]):
        return jsonify({"error": "缺少必要欄位"}), 400

    resp = requests.get(f"{SUPABASE_URL}/rest/v1/checkins",
                        params={"google_id": f"eq.{google_id}",
                                "merchant_name": f"eq.{merchant_name}", "select": "id"},
                        headers=_supabase_headers(), timeout=8)
    if resp.status_code == 200 and resp.json():
        return jsonify({"error": "已打卡過此商家，每個商家只能打卡一次", "already_done": True}), 400

    if not verify_photo_has_person(image_b64):
        return jsonify({"error": "照片中未偵測到人臉，請確認您本人出現在照片中 😊"}), 400

    xp_earned = 30
    requests.post(f"{SUPABASE_URL}/rest/v1/checkins",
                  json={"google_id": google_id, "merchant_name": merchant_name, "xp_earned": xp_earned},
                  headers=_supabase_headers(), timeout=8)

    user_resp = requests.get(f"{SUPABASE_URL}/rest/v1/game_users",
                             params={"google_id": f"eq.{google_id}", "select": "checkin_count"},
                             headers=_supabase_headers(), timeout=8)
    if user_resp.status_code == 200 and user_resp.json():
        count = user_resp.json()[0].get("checkin_count", 0)
        requests.patch(f"{SUPABASE_URL}/rest/v1/game_users",
                       params={"google_id": f"eq.{google_id}"},
                       json={"checkin_count": count + 1},
                       headers=_supabase_headers(), timeout=8)

    new_stats = award_xp_to_user(google_id, xp_earned)
    return jsonify({"xp_earned": xp_earned, "new_stats": new_stats, "merchant": merchant_name})

@app.route('/api/game/checkins/<google_id>')
def game_get_checkins(google_id):
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/checkins",
                        params={"google_id": f"eq.{google_id}", "select": "*", "order": "created_at.desc"},
                        headers=_supabase_headers(), timeout=8)
    return jsonify(resp.json() if resp.status_code == 200 else [])

@app.route('/api/game/quiz')
def game_quiz():
    quiz = get_quiz_today()
    return jsonify({k: v for k, v in quiz.items() if k != 'answer'})

@app.route('/api/game/quiz/answer', methods=['POST'])
def game_quiz_answer():
    data = request.json
    google_id = data.get('google_id')
    answer_idx = data.get('answer')
    if not google_id or answer_idx is None:
        return jsonify({"error": "缺少必要欄位"}), 400

    quiz = get_quiz_today()
    today_str = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/game_users",
                        params={"google_id": f"eq.{google_id}", "select": "quiz_last_date"},
                        headers=_supabase_headers(), timeout=8)
    if resp.status_code == 200 and resp.json():
        if resp.json()[0].get("quiz_last_date") == today_str:
            return jsonify({"error": "今日已作答", "already_done": True}), 400

    correct = (answer_idx == quiz["answer"])
    xp_earned = 20 if correct else 5
    requests.patch(f"{SUPABASE_URL}/rest/v1/game_users",
                   params={"google_id": f"eq.{google_id}"},
                   json={"quiz_last_date": today_str},
                   headers=_supabase_headers(), timeout=8)
    new_stats = award_xp_to_user(google_id, xp_earned)
    return jsonify({"correct": correct, "correct_answer": quiz["answer"],
                    "fact": quiz["fact"], "xp_earned": xp_earned, "new_stats": new_stats})

@app.route('/api/game/leaderboard')
def game_leaderboard():
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/game_users",
                        params={"select": "name,avatar_url,xp,level,title,checkin_count",
                                "order": "xp.desc", "limit": "10"},
                        headers=_supabase_headers(), timeout=8)
    return jsonify(resp.json() if resp.status_code == 200 else [])

# ==========================================
# 5. Chat API
# ==========================================
TOOL_MAP = {
    'check_availability': check_availability,
    'book_experience': book_experience,
    'search_local_merchants': search_local_merchants,
    'get_nantou_weather': get_nantou_weather,
}

@app.route('/api/chat', methods=['POST'])
def chat():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    if not check_rate(ip):
        return jsonify({"reply": "請求過於頻繁，請稍後再試 🙏"}), 429

    user_message = request.json.get('message', '')
    history = request.json.get('history', [])
    lang = request.json.get('lang', 'zh-TW')

    lang_note = {
        'en': "\n[LANGUAGE OVERRIDE]: Respond entirely in English for this conversation.",
        'ja': "\n[LANGUAGE OVERRIDE]: この会話はすべて日本語でお答えください。",
    }.get(lang, "")
    system_instruction = BASE_SYSTEM_INSTRUCTION + lang_note

    for model in [MODEL_NAME, FALLBACK_MODEL]:
        for attempt in range(3):
            try:
                contents = []
                for h in history:
                    contents.append(types.Content(
                        role=h['role'],
                        parts=[types.Part(text=h['text'])]
                    ))
                contents.append(types.Content(role='user', parts=[types.Part(text=user_message)]))
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[check_availability, book_experience, search_local_merchants, get_nantou_weather],
                )

                while True:
                    response = client.models.generate_content(model=model, contents=contents, config=config)
                    candidate = response.candidates[0].content
                    parts = candidate.parts if candidate and candidate.parts else []
                    function_calls = [p.function_call for p in parts if p.function_call]

                    if not function_calls:
                        reply_text = response.text if response.text else "抱歉，我無法生成回應，請再試一次。"
                        return jsonify({"reply": reply_text})

                    tool_results = []
                    for fc in function_calls:
                        func = TOOL_MAP.get(fc.name)
                        result = func(**dict(fc.args)) if func else f"找不到工具：{fc.name}"
                        print(f"🔧 工具呼叫 [{fc.name}]：{dict(fc.args)}")
                        tool_results.append(types.Part(function_response=types.FunctionResponse(
                            name=fc.name, response={"result": result}
                        )))

                    contents.append(candidate)
                    contents.append(types.Content(role='user', parts=tool_results))

            except Exception as e:
                err = str(e)
                if '503' in err or 'UNAVAILABLE' in err:
                    wait = 2 ** attempt
                    print(f"⚠️ {model} 流量過高，{wait}秒後重試（第{attempt+1}次）")
                    time.sleep(wait)
                    continue
                traceback.print_exc()
                print(f"❌ 錯誤：{e}")
                return jsonify({"reply": f"系統發生錯誤：{str(e)[:200]}"})
            break

    return jsonify({"reply": "目前 AI 服務需求量很大，請稍等片刻再試一次 🙏"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
