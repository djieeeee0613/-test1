import os
import sys
import json
import uuid
import re
import threading
import time
import random
import traceback
import requests
from datetime import datetime, date, timezone, timedelta
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, render_template_string, redirect
from google import genai
from merchants_data import CATEGORIES, CATEGORY_ORDER, UNCATEGORIZED
import merchant_directory as md
from game import sb_get, sb_insert, sb_patch, n8n_headers
from google.genai import types

load_dotenv()
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# 速率限制：每個 IP 每分鐘最多 20 次
_rate = {}
def check_rate(ip, limit=20, window=60):
    now = time.time()
    hits = [t for t in _rate.get(ip, []) if now - t < window]
    hits.append(now)
    _rate[ip] = hits
    return len(hits) <= limit

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
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

# ── 黑白線條圖示（Tabler Icons，MIT 授權，檔案在 static/icons/）──
# 模板裡用 {{ icon('phone') }} 嵌入 SVG；大小跟著文字（1em）、顏色跟著文字顏色
from markupsafe import Markup
_ICON_DIR = os.path.join(os.path.dirname(__file__), 'static', 'icons')
ICONS = {}
for _f in os.listdir(_ICON_DIR):
    if _f.endswith('.svg'):
        _svg = re.sub(r'\s+', ' ', open(os.path.join(_ICON_DIR, _f), encoding='utf-8').read()).strip()
        _svg = re.sub(r'\s(width|height)="24"', r' \1="1em"', _svg)
        ICONS[_f[:-4]] = re.sub(r'class="[^"]*"', 'class="ic" aria-hidden="true" focusable="false"', _svg)

def icon(name):
    return Markup(ICONS.get(name, ICONS['map-pin']))

app.jinja_env.globals['icon'] = icon

# ==========================================
# 2. AI 工具函式
# ==========================================
WMO_CODES = {
    0: "晴天", 1: "大致晴天", 2: "部分多雲", 3: "多雲",
    45: "有霧", 48: "濃霧",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    80: "陣雨", 81: "陣雨", 82: "強陣雨",
    95: "雷雨", 96: "強雷雨"
}

def _open_meteo_weather() -> str:
    """備援：中央氣象署取不到時改用 Open-Meteo（整個南投縣一組數字）"""
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
        return "天氣資料暫時無法取得"

import ssl
from requests.adapters import HTTPAdapter

CWA_DATASET = "F-D0047-023"   # 南投縣各鄉鎮未來一週天氣預報（12 小時一筆）
_cwa_cache = {"at": 0, "value": None}

class _CwaAdapter(HTTPAdapter):
    """氣象署憑證缺少 Subject Key Identifier，Python 3.13 預設的嚴格驗證會拒絕；只關掉嚴格旗標，仍然驗證憑證鏈"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

_cwa_session = requests.Session()
_cwa_session.mount("https://opendata.cwa.gov.tw", _CwaAdapter())

def _cwa_townships():
    """南投縣各鄉鎮預報（快取 30 分鐘）：{鄉鎮名: {元素名: [時段...]}}；沒設金鑰或失敗回傳 None"""
    key = os.environ.get("CWA_API_KEY")
    if not key:
        return None
    if _cwa_cache["value"] and time.time() - _cwa_cache["at"] < 1800:
        return _cwa_cache["value"]
    try:
        r = _cwa_session.get(f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{CWA_DATASET}",
                             headers={"Authorization": key}, timeout=10)   # 金鑰放 header，不進網址與錯誤訊息
        r.raise_for_status()
        locs = r.json()["records"]["Locations"][0]["Location"]
        data = {l["LocationName"]: {e["ElementName"]: e["Time"] for e in l["WeatherElement"]} for l in locs}
        _cwa_cache.update(at=time.time(), value=data)
        return data
    except Exception as e:
        print(f"⚠️ 中央氣象署天氣讀取失敗：{type(e).__name__}")
        return _cwa_cache["value"]    # 失敗時沿用舊快取（可能是 None）

def _cwa_val(el, key):
    try:
        return el["ElementValue"][0][key]
    except (KeyError, IndexError, TypeError):
        return None

def get_nantou_weather(township: str = "") -> str:
    """取得南投縣今明兩天的天氣預報（中央氣象署鄉鎮預報），用於建議適合的戶外或室內體驗。
    Args:
        township: 鄉鎮名稱，例如：埔里、魚池、信義、仁愛、竹山、鹿谷、集集、水里、國姓、草屯、南投市、名間、中寮、魚池、集集。
                  旅客有指定地點時帶入（山區與平地天氣常不同）；沒指定就不帶，預設查埔里。"""
    data = _cwa_townships()
    if not data:
        return _open_meteo_weather()
    want = (township or "埔里").strip()
    for place, town in (("日月潭", "魚池"), ("清境", "仁愛"), ("合歡山", "仁愛"), ("溪頭", "鹿谷"), ("杉林溪", "竹山"), ("奧萬大", "仁愛")):
        if place in want:
            want = town
    name = next((n for n in data if n == want or n.startswith(want) or want.startswith(n[:-1])), None)
    if not name:
        name = "埔里鎮"
    els = data[name]
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).date()
    lines = []
    for offset, label in ((0, "今天"), (1, "明天")):
        day = str(today + timedelta(days=offset))
        def rows(el):
            return [t for t in els.get(el, []) if str(t.get("StartTime", ""))[:10] == day]
        wx = rows("天氣現象")
        if not wx:
            continue
        mx = [int(v) for v in (_cwa_val(t, "MaxTemperature") for t in rows("最高溫度")) if v is not None]
        mn = [int(v) for v in (_cwa_val(t, "MinTemperature") for t in rows("最低溫度")) if v is not None]
        pop = [int(v) for v in (_cwa_val(t, "ProbabilityOfPrecipitation") for t in rows("12小時降雨機率")) if v not in (None, "-")]
        uv = _cwa_val(rows("紫外線指數")[0], "UVExposureLevel") if rows("紫外線指數") else None
        desc = "、".join(dict.fromkeys(_cwa_val(t, "Weather") for t in wx if _cwa_val(t, "Weather")))
        parts = [desc]
        if mn and mx:
            parts.append(f"氣溫 {min(mn)}～{max(mx)}°C")
        if pop:
            parts.append(f"降雨機率最高 {max(pop)}%")
        if uv:
            parts.append(f"紫外線{uv}")
        lines.append(f"{label}（{day}）：" + "，".join(parts))
    if not lines:
        return _open_meteo_weather()
    return f"{name}天氣（中央氣象署）\n" + "\n".join(lines)

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

def _tw_time(iso: str | None) -> str:
    """Supabase 的 timestamptz 轉成台灣時間字串"""
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return ""

def _check_date_not_past(date_str: str) -> str | None:
    """若日期已過去，回傳錯誤訊息；否則回傳 None"""
    d = _parse_booking_date(date_str)
    if d is None:
        return None
    today = _today_tw()
    if d < today:
        return (
            f"無法預約過去的日期（{date_str}）。"
            f"今天是 {today.strftime('%Y年%m月%d日')}，請選擇今天或之後的日期。"
        )
    return None

def official_name(artisan_name: str) -> str:
    """把 AI 送出的店名校正成商家名錄上的正式名稱（LINE 通知依正式店名找商家）；對不到就原樣使用"""
    m = md.match_merchant(artisan_name)
    if m and m["name"] != artisan_name:
        print(f"🏷️ 店名校正：{artisan_name} → {m['name']}")
    return m["name"] if m else artisan_name

def check_availability(artisan_name: str, date: str) -> str:
    """查詢特定職人空檔"""
    artisan_name = official_name(artisan_name)
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
    artisan_name = official_name(artisan_name)
    print(f"🚀 發送預約單：{artisan_name}, {date}, {pax}人, 旅客：{customer_name}")
    d = _parse_booking_date(date)
    booking_id = uuid.uuid4().hex[:8].upper()
    row = {
        "artisan": artisan_name, "booking_date": d.isoformat() if d else date, "people_count": pax,
        "customer_name": customer_name, "phone": phone, "email": email, "booking_id": booking_id,
    }
    try:
        response = requests.post(N8N_WEBHOOK_URL, json={**row, "status": "new_booking"},
                                 headers=n8n_headers(), timeout=10)
        if response.status_code == 200:
            return f"預約資料已成功送出！商家：{artisan_name}，您的預約編號為 **{booking_id}**。"
        print(f"❌ n8n 預約 webhook {response.status_code}: {response.text[:200]}")
    except Exception as e:
        print(f"❌ n8n 預約 webhook 連線失敗：{e}")
    # n8n 失敗：直接寫進 Supabase，管理員在後台看得到這筆待確認的預約，可再聯繫商家
    # n8n 可能已寫入 bookings 後才在推播 LINE 時失敗，先查避免重複
    if sb_get("bookings", {"booking_id": f"eq.{booking_id}", "select": "id"}) or sb_insert("bookings", {**row, "status": "待確認"}):
        return (f"預約已收到，預約編號 **{booking_id}**。目前無法即時通知商家，"
                f"平台人員會協助聯繫 {artisan_name}，確認後以 Email 通知您。")
    return "抱歉，預約系統暫時無法使用，這筆預約沒有送出，請稍後再試或直接聯絡商家。"

_events_cache = {"at": 0, "value": {}}
_month_bookings_cache = {"at": 0, "value": {}}

def _bookings_this_month():
    """本月各商家的預約數（快取 5 分鐘），搜尋時把預約較少的職人排前面，分散人流"""
    if time.time() - _month_bookings_cache["at"] < 300:
        return _month_bookings_cache["value"]
    counts = {}
    month_start = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-01T00:00:00+08:00')
    for b in sb_get("bookings", {"select": "artisan", "created_at": f"gte.{month_start}", "limit": "5000"}):
        m = md.match_merchant(b.get("artisan"))
        if m:
            counts[m["name"]] = counts.get(m["name"], 0) + 1
    _month_bookings_cache.update(at=time.time(), value=counts)
    return counts

def _active_events_by_merchant():
    """首頁輪播上架中的活動（快取 5 分鐘），依正式店名分組，讓 AI 推薦時可以提到"""
    if time.time() - _events_cache["at"] < 300:
        return _events_cache["value"]
    grouped = {}
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/events", params={"active": "eq.true", "select": "*", "order": "id.desc"},
                         headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=6)
        for e in (r.json() if r.status_code == 200 else []):
            m = md.match_merchant(e.get("artisan"))
            if m:
                grouped.setdefault(m["name"], []).append(e)
    except Exception as e:
        print(f"⚠️ 讀取活動失敗：{e}")
    _events_cache.update(at=time.time(), value=grouped)
    return grouped

def search_local_merchants(category: str = "", township: str = "", keyword: str = "") -> str:
    """搜尋南投在地職人商家（資料來自正式商家名錄）。結果依「本月預約數」由少到多排序，數量相同時隨機，排在前面的請優先推薦。
    三個參數都可以留空；留空代表不篩選，全部留空會回傳全部商家。

    Args:
        category: 類別名稱，可填：文化永續、農業永續、部落永續、永續住宿、永續餐飲、在地購物。
        township: 鄉鎮，例如：魚池、埔里、國姓、鹿谷、竹山、信義、仁愛、水里、集集、草屯、南投市。
        keyword: 關鍵字，會比對店名、介紹與永續依據，例如：手沖、柴燒、親子、溫泉、布農。
    """
    rows, slug = md.search(category, township, keyword)
    notes = []
    if category and not slug:
        notes.append(f"（找不到「{category}」這個類別，已改為不限類別）")
    if not rows and slug and not (township or keyword):
        rows, _ = md.search()
        notes.append(f"（「{CATEGORIES[slug]['name']}」目前還沒有歸類的商家，以下列出全部商家，請挑選相關的推薦）")
    if not rows and (township or keyword):
        rows, _ = md.search(category if slug else "")
        notes.append("（沒有完全符合的商家，以下改列同類別的全部商家）")
    counts = _bookings_this_month()
    rows = sorted(rows, key=lambda m: counts.get(m["name"], 0))   # rows 已隨機打亂，sorted 是穩定排序，同數量維持隨機
    print(f"🔎 搜尋商家：category={category!r} township={township!r} keyword={keyword!r} → {len(rows)} 家")
    header = f"共 {len(rows)} 家（店名以【】內為正式名稱；已依本月預約數由少到多排序，請優先推薦排在前面的職人）："
    return "\n".join(notes + [header, "", md.format_for_ai(rows, _active_events_by_merchant())])

# ==========================================
# 3. 模型設定與系統提示
# ==========================================
MODEL_NAME = 'models/gemini-2.5-flash'
FALLBACK_MODEL = 'models/gemini-2.0-flash'

BASE_SYSTEM_INSTRUCTION = """
你是「緣投」，南投山林的精靈，也是南投職人深度旅遊的 AI 嚮導。

【語氣】
親切、簡潔，像在地朋友。回覆中不使用 emoji 或表情符號，用文字表達即可。依旅客使用的語言回覆（繁體中文、英文或日文），旅客用英文或日文時全程使用該語言。

【規則】
1. 只推薦資料庫（search_local_merchants 的結果）中的店家與職人，不可自行編造店名、地址、價格或營業時間。
2. 介紹景點與店家時，不要提及資料來源、永續依據或認證名稱，直接用自然的話介紹特色即可。資料庫沒有的資訊（例如營業時間、價格、交通方式細節），直接說「目前沒有這項資料」，並建議旅客向店家確認。
3. 介紹部落儀式、信仰與傳統時，只使用資料庫中職人或族人提供的內容，不誇大、不臆測，也不把儀式形容成表演。
4. 條件相近時，優先推薦被推薦次數較少的職人：搜尋結果已依本月預約數由少到多排序，請優先從排在前面的職人中挑選，不要每次都推薦同幾家。
5. 規劃行程時，提醒旅客可搭乘台灣好行、自備水杯與餐具，並優先選擇在地店家。
6. 行程或推薦包含部落（部落永續類別）或宗教場域（寺、廟、宮、禪寺、教堂等）時，主動提醒參訪禮儀，例如：拍照前先詢問、尊重祭儀時段、保持安靜。
7. 不回答與南投旅遊無關的問題，禮貌地把話題帶回旅程規劃。

【天氣整合】如果旅客詢問行程規劃，主動先呼叫 get_nantou_weather 取得天氣（旅客提到地點時帶入 township，例如日月潭、清境、信義，山區與平地天氣不同），在行程中加入天氣提醒（如：今天有雨，建議攜帶雨具；天氣晴朗適合戶外部落體驗）。

【搜尋方式】search_local_merchants 可以依 category（類別）、township（鄉鎮）、keyword（關鍵字）篩選。旅客指定區域時帶入 township，指定體驗類型時帶入 category 或 keyword。

【價格】商家名錄沒有價格資料，只有「近期活動」會標示費用。旅客詢問價格或預算時，絕對不可自行編造金額；請說明費用依體驗內容而定，預約時商家會再確認，若有標示費用的近期活動可以優先介紹。

【正式店名】推薦與預約時，請使用搜尋結果中【】內的正式店名。

---

【互動選項格式】
當你需要詢問以下資訊時，必須在問句後加上 [選項: 選項1 | 選項2 | ...] 讓旅客點選，不得只用文字敘述：
- 人數 → [選項: 1-2人 | 3-4人 | 5-6人 | 7-8人 | 8人以上]
- 感興趣的區域 → [選項: 日月潭周邊 | 清境合歡山 | 埔里小鎮 | 竹山鹿谷 | 國姓咖啡公路 | 信義原民部落 | 其他]
日期讓旅客自由文字輸入，不加選項。

---

【A. 旅客詢問行程規劃】
觸發條件：旅客說「幫我規劃行程」、「一日遊」、「行程推薦」、「怎麼安排」等。

1. 先呼叫 get_nantou_weather 取得天氣。
2. 再呼叫 search_local_merchants 取得商家（旅客有指定區域或喜好時，帶入 township 或 keyword；沒有就不帶參數）。
3. 根據天氣安排早午晚三個時段，格式如下：

---
**南投一日深度體驗行程**

**上午｜9:00 - 12:00**
**商家名稱**
地點｜一句話說明為何早上適合來這裡

**下午｜13:00 - 17:00**
**商家名稱**
地點｜一句話說明為何下午適合來這裡

**傍晚／夜間｜18:00 以後**
**商家名稱**
地點｜一句話說明為何傍晚適合來這裡

**天氣提醒**：（根據當天天氣給出實用提醒，例如是否帶傘、防曬）
**綠色旅行**：可搭乘台灣好行，記得自備水杯與餐具
**參訪禮儀**：（行程有部落或宗教場域時才加這行）
---

4. 行程結尾加上：「以上是為您量身打造的南投一日行程！請問您對哪個時段的體驗最感興趣，想進一步預約嗎？」

---

【B. 旅客詢問分類推薦】
觸發條件：旅客說「有什麼推薦」、「好玩的地方」、「推薦商家」等。

1. 先呼叫 search_local_merchants 取得資料庫內容。
2. 依類別各挑最多 3 個，格式如下：

---
**南投精選體驗推薦**

**文化永續**
1. **商家名稱** — 一句話亮點

**農業永續**
1. **商家名稱** — 一句話亮點

**部落永續**
1. **商家名稱** — 一句話亮點

**永續住宿**
1. **商家名稱** — 一句話亮點

**永續餐飲**
1. **商家名稱** — 一句話亮點

**在地購物**
1. **商家名稱** — 一句話亮點
---

3. 結尾加上：「請問您對哪一家最感興趣？告訴我店家名稱，我就幫您安排預約！」

---

【C. 旅客點擊分類卡片】
觸發條件：訊息以「【分類探索】」開頭。

1. 呼叫 search_local_merchants，把訊息中的類別名稱填入 category（例如 category="文化永續"；「特色住宿」請填「永續住宿」）。
2. 從結果中挑選商家。
3. 列出 3～5 個商家，格式如下：

---
**[類別名稱] 精選商家**

**1. [商家名稱]**
[地點] ｜ [一句話亮點]
[價格]（只有近期活動有標示費用時才寫，沒有就省略這行）

**2. [商家名稱]**
...以此類推
---

4. 結尾固定加上：「以上是[類別名稱]的精選商家！請問您對哪一家最感興趣？告訴我店家名稱，我可以幫您查詢空檔、安排預約，或規劃整天行程 」

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

「最後一步！請您用以下格式傳送聯絡資料：

姓名：王小明
電話：0912-345-678
Email：example@email.com

直接複製上方格式填入即可 」

**第二階段：收到聯絡資料後完成預約**
- 從訊息中擷取姓名、電話、Email。
- 立刻呼叫 book_experience 完成預約。
- 預約成功後回覆：「已成功為您預約 [店家名稱]！您的預約編號：[booking_id]。商家收到通知後會與您聯繫確認，期待您的南投之旅！」
"""

# ==========================================
# 4. Flask 路由
# ==========================================
def _home_context():
    """首頁（index.html）與改版首頁（home_v2.html）共用：公告、分類、地圖用的店名→分類對照"""
    from game import site_settings
    ann = site_settings()["announcement"]
    cats = [CATEGORIES[s] for s in CATEGORY_ORDER]
    # 地圖依正式店名取分類顏色（locations.js 只存店名）；標記圖示用 cat_icons 的黑白線條 SVG
    merchant_cat = {m["name"]: {"cat": m["cat"], "color": m["color"]} for m in md.MERCHANTS}
    cat_icons = {s: str(icon(CATEGORIES[s]["icon"])) for s in CATEGORIES}
    ui_icons = {n: str(icon(n)) for n in ("phone", "message-chatbot", "share", "link", "star", "calendar", "users", "coin", "map-pin")}
    return dict(announcement=ann if ann.get("enabled") and ann.get("text") else None,
                categories=[c for c in cats if c["slug"] != UNCATEGORIZED], map_categories=[c for c in cats if c["merchants"]],
                merchant_cat=merchant_cat, cat_icons=cat_icons, ui_icons=ui_icons,
                game_stages=_game_stages(), total_merchants=len(md.MERCHANTS))


def _game_stages():
    """首頁遊戲橫幅的升級輪播：每個等級對應的緣投造型（與 game.html 的 formFor 同一套規則）"""
    from game import levels
    lv = levels()
    forms = ['sleep', 'farmer', 'foodie', 'cheer']
    top = len(lv)
    def form(n):
        return forms[3] if n >= top else forms[min(2, (n - 1) * 3 // max(1, top - 1))]
    return [{"level": n, "title": title, "xp": xp, "img": f"/static/images/mascot/mascot-{form(n)}.png"}
            for xp, n, title, _ in lv]

@app.route('/')
def index():
    return render_template('index.html', **_home_context())

@app.route('/new')
def home_v2():
    """首頁改版試作（templates/home_v2.html），確認後再決定是否取代正式首頁"""
    return render_template('home_v2.html', **_home_context())

@app.route('/category/<slug>')
def category_page(slug):
    cat = CATEGORIES.get(slug)
    if not cat:
        # 舊分類網址（coffee、tea、craft、spa）在 2026-10 改版後已不存在，導回首頁的分類區
        return redirect('/#experiences')
    if cat.get("external_url"):          # 在地購物導向「品味南投」平台
        return redirect(cat["external_url"])
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

# --- 行程分享 ---
@app.route('/api/share-plan', methods=['POST'])
def share_plan():
    plan_text = ((request.get_json(silent=True) or {}).get('plan') or '').strip()
    if not plan_text or len(plan_text) > 10000:
        return jsonify({"error": "行程內容不正確"}), 400
    plan_id = uuid.uuid4().hex[:8]
    if not sb_insert("shared_plans", {"plan_id": plan_id, "text": plan_text}):
        return jsonify({"error": "分享失敗，請稍後再試"}), 500
    return jsonify({"plan_id": plan_id, "url": f"/plan/{plan_id}"})

@app.route('/plan/<plan_id>')
def view_plan(plan_id):
    rows = sb_get("shared_plans", {"plan_id": f"eq.{plan_id}", "select": "text,created_at"})         if re.fullmatch(r'[0-9a-f]{8}', plan_id) else []
    plan = rows[0] if rows else None
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
<link rel="icon" type="image/png" href="/static/images/favicon.png">
<link rel="apple-touch-icon" href="/static/images/apple-touch-icon.png">
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
  <div class="logo"><img src="/static/images/logo-mark.png" alt="" style="width:26px;height:26px;border-radius:6px;vertical-align:middle;margin-right:8px">深度與美學旅遊研究室</div>
  <h2>我的南投深度行程</h2>
  <div class="meta">建立時間：{{ created_at }} ｜ 行程編號：{{ plan_id }}</div>
  <div class="content">{{ content|safe }}</div>
  <div class="footer">
    <a href="/" class="btn">回到官網預約體驗 →</a>
  </div>
</div>
</body>
</html>""", plan_id=plan_id, created_at=_tw_time(plan.get('created_at')), content=text)

# --- 旅客評價頁 ---
def _rating_state(booking_id: str):
    """回傳 (預約, 狀態)：missing 找不到 / rejected 已婉拒 / rated 已評價 / early 體驗日還沒到 / open 可評價"""
    if not re.fullmatch(r'[A-Z0-9]{8}', booking_id or ''):
        return None, 'missing'
    rows = sb_get("bookings", {"booking_id": f"eq.{booking_id}", "select": "artisan,booking_date,status,rating"})
    if not rows:
        return None, 'missing'
    b = rows[0]
    if b.get('status') == '已婉拒':
        return b, 'rejected'
    if b.get('rating'):
        return b, 'rated'
    # 只認含年份的日期；舊資料沒有年份時無法判斷，直接開放評價
    raw = str(b.get('booking_date') or '')
    d = _parse_booking_date(raw) if re.match(r'\d{4}', raw) else None
    if d and d > _today_tw():
        return b, 'early'
    return b, 'open'

@app.route('/rate/<booking_id>')
def rate_page(booking_id):
    booking, state = _rating_state(booking_id)
    artisan = booking.get('artisan', '') if booking else ''
    return render_template_string("""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>體驗評價</title>
<link rel="icon" type="image/png" href="/static/images/favicon.png">
<link rel="apple-touch-icon" href="/static/images/apple-touch-icon.png">
<style>
  body{font-family:'Helvetica Neue',Arial,sans-serif;background:#F4F1EA;min-height:100vh;display:flex;align-items:center;justify-content:center;}
  .card{background:white;border-radius:24px;padding:44px 36px;max-width:420px;width:90%;box-shadow:0 8px 40px rgba(0,0,0,0.1);text-align:center;}
  .icon{font-size:48px;margin-bottom:16px;}
  h2{color:#4A5D4E;font-size:22px;margin-bottom:8px;}
  .sub{color:#666;font-size:14px;line-height:1.6;margin-bottom:32px;}
  .stars{display:flex;justify-content:center;gap:8px;margin-bottom:24px;}
  .star{color:#f5b301;font-size:44px;cursor:pointer;opacity:0.25;transition:opacity 0.15s,transform 0.1s;}
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
  <div class="icon"><img src="/static/images/logo-mark.png" alt="深度與美學旅遊研究室" style="width:64px;height:64px;border-radius:14px"></div>
  {% if state == 'rated' %}
  <h2>已收到您的評價</h2>
  <div class="already">感謝您的回饋！</div>
  {% elif state == 'early' %}
  <h2>體驗還沒開始喔</h2>
  <div class="sub">{{ artisan }}的體驗日期是 {{ booking_date }}<br>體驗當天之後再回來這裡評價吧！</div>
  {% elif state != 'open' %}
  <h2>無法評價這筆預約</h2>
  <div class="sub">{{ '這筆預約已被婉拒' if state == 'rejected' else '找不到這筆預約，請確認連結是否正確' }}</div>
  {% else %}
  <h2>感謝您的南投體驗！</h2>
  <div class="sub">{{ artisan }}的服務如何？<br>您的回饋幫助更多旅客發現南投的美好<br><small style="color:#bbb">預約編號：{{ booking_id }}</small></div>
  <div class="stars" id="stars">
    <span class="star" data-v="1">★</span>
    <span class="star" data-v="2">★</span>
    <span class="star" data-v="3">★</span>
    <span class="star" data-v="4">★</span>
    <span class="star" data-v="5">★</span>
  </div>
  <textarea id="comment" rows="3" placeholder="分享您的體驗心得（選填）"></textarea>
  <button class="btn" onclick="submitRating()">送出評價</button>
  <div class="success" id="success">感謝您的評價，期待再次相遇！</div>
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
  }).then(r => r.json()).then(d => {
    if (d.error) { alert(d.error); return; }
    document.getElementById('success').style.display = 'block';
    document.querySelector('.btn').style.display = 'none';
    document.getElementById('stars').style.pointerEvents = 'none';
  });
}
</script>
</body>
</html>""", booking_id=booking_id, artisan=artisan, state=state,
       booking_date=booking.get('booking_date', '') if booking else '')

@app.route('/api/rate', methods=['POST'])
def submit_rate():
    data = request.get_json(silent=True) or {}
    booking_id = str(data.get('booking_id') or '')
    rating = data.get('rating')
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        return jsonify({"error": "請選擇 1–5 顆星"}), 400
    _, state = _rating_state(booking_id)
    if state != 'open':
        msg = {"rated": "這筆預約已經評價過了", "early": "體驗日期之後才能評價",
               "rejected": "這筆預約已被婉拒，無法評價"}.get(state, "找不到這筆預約")
        return jsonify({"error": msg}), 400
    sb_patch("bookings", {"booking_id": f"eq.{booking_id}", "rating": "is.null"},
             {"rating": rating, "rating_comment": str(data.get('comment') or '')[:500]})
    return jsonify({"ok": True})

# ==========================================
# GAME — 緣投探索者（見 game.py）
# ==========================================
from game import game_bp
app.register_blueprint(game_bp)
from admin import admin_bp
app.register_blueprint(admin_bp)

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
        return jsonify({"reply": "請求過於頻繁，請稍後再試 "}), 429

    data = request.json or {}
    user_message = data.get('message', '')
    lang = data.get('lang', 'zh-TW')
    t0 = time.time()
    state = {"tools": [], "booked": False}
    reply, error = run_chat(user_message, data.get('history', []), lang, state)
    threading.Thread(target=log_chat, daemon=True, args=({
        "session_id": re.sub(r"[^A-Za-z0-9-]", "", str(data.get('session_id') or ""))[:64] or None,
        "lang": lang, "category": chat_category(user_message), "tools": state["tools"],
        "booked": state["booked"], "error": error, "ms": int((time.time() - t0) * 1000)},)).start()
    return jsonify({"reply": reply})

def chat_category(msg):
    """只記錄旅客點了哪個類別按鈕，不記錄對話內容"""
    if msg.startswith("【分類探索】"):
        rest = msg[len("【分類探索】"):]
        m = re.search(r"「(.+?)」", rest)
        label = re.sub(r"^[^\w]+", "", (m.group(1) if m else rest).strip())
        return label[:20] or "分類探索"
    if "規劃" in msg and "行程" in msg:
        return "行程規劃"
    return None

def log_chat(row):
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/chat_logs", json=row,
                          headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                                   "Content-Type": "application/json"}, timeout=5)
        if r.status_code >= 300:
            print(f"⚠️ chat log {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ chat log failed: {e}")

def run_chat(user_message, history, lang, state):
    """回傳 (回覆文字, 是否發生錯誤)；state 會記下呼叫過的工具"""
    lang_note = {
        'zh-TW': "\n[LANGUAGE OVERRIDE]: 這次對話全程使用繁體中文回覆，即使先前訊息或工具結果出現其他語言也一樣。",
        'en': "\n[LANGUAGE OVERRIDE]: Respond entirely in English for this conversation, even if earlier messages or tool results are in another language. Never switch to Japanese or Chinese.",
        'ja': "\n[LANGUAGE OVERRIDE]: この会話はすべて日本語でお答えください。以前のメッセージやツール結果が他の言語でも同様です。",
    }.get(lang, "")
    lang_tag = {
        'zh-TW': "（請用繁體中文回覆）",
        'en': "\n[Reply in English only]",
        'ja': "\n[日本語のみで回答してください]",
    }.get(lang, "")
    from game import site_settings
    extra = (site_settings().get("ai_extra") or "").strip()
    system_instruction = BASE_SYSTEM_INSTRUCTION + lang_note
    if extra:
        system_instruction += f"\n\n【管理員補充指示（本期營運重點，請優先遵守，但不可違反上面的預約規則）】\n{extra}"

    for model in [MODEL_NAME, FALLBACK_MODEL]:
        for attempt in range(3):
            try:
                contents = []
                for h in history:
                    contents.append(types.Content(
                        role=h['role'],
                        parts=[types.Part(text=h['text'])]
                    ))
                contents.append(types.Content(role='user', parts=[types.Part(text=user_message + lang_tag)]))
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[check_availability, book_experience, search_local_merchants, get_nantou_weather],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                )

                while True:
                    response = client.models.generate_content(model=model, contents=contents, config=config)
                    candidate = response.candidates[0].content
                    parts = candidate.parts if candidate and candidate.parts else []
                    function_calls = [p.function_call for p in parts if p.function_call]

                    if not function_calls:
                        return (response.text or "抱歉，我無法生成回應，請再試一次。"), False

                    tool_results = []
                    for fc in function_calls:
                        func = TOOL_MAP.get(fc.name)
                        result = func(**dict(fc.args)) if func else f"找不到工具：{fc.name}"
                        print(f"🔧 工具呼叫 [{fc.name}]：{dict(fc.args)}")
                        state["tools"].append(fc.name)
                        if fc.name == "book_experience" and not str(result).startswith("❌"):
                            state["booked"] = True
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
                return f"系統發生錯誤：{str(e)[:200]}", True
            break

    return "目前 AI 服務需求量很大，請稍等片刻再試一次", True

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
