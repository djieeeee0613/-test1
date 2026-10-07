# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案說明

南投在地職人永續旅遊 AI 嚮導。前端是完整旅遊網站，右下角浮動按鈕可展開聊天視窗，AI 可呼叫工具搜尋商家、查詢空檔、完成預約。預約透過 n8n webhook 寫入 Supabase 並推播 LINE 通知給商家。

## 雲端部署架構

| 服務 | 平台 | URL |
|------|------|-----|
| Flask 前後端 | Render | https://test1-yipa.onrender.com |
| n8n 工作流 | n8n.cloud | https://nantoutravel.app.n8n.cloud |
| 資料庫 | Supabase | https://gbffodvtfirdtecsespm.supabase.co |

## 本機開發啟動

```powershell
# 僅啟動 Flask（開發用）
cd "C:\Users\user\Desktop\dj\黑客松"
python app.py
```

- Flask：`http://127.0.0.1:5001`（debug 模式，存檔自動重載）
- n8n.cloud 常駐，不需本機啟動
- Supabase 常駐，不需本機啟動

查詢可用 Gemini 模型：
```powershell
python check_models.py
```

## 環境設定

`.env`（不進 git，本機開發用）：
```
GEMINI_API_KEY=你的金鑰
MERCHANT_PASSWORD=商家後台密碼（預設 nantou2026）
```

Render 環境變數（在 Render dashboard 設定）：
- `GEMINI_API_KEY`
- `MERCHANT_PASSWORD`
- `N8N_WEBHOOK_URL`（預設 https://nantoutravel.app.n8n.cloud/webhook/nantou-booking）
- `SUPABASE_URL`（https://gbffodvtfirdtecsespm.supabase.co）
- `SUPABASE_KEY`（Supabase anon key）

## 架構

### 資料流

```
旅客瀏覽器
  → GET /          → Flask(Render) → templates/index.html（旅遊網站 + 聊天視窗 + Leaflet 地圖）
  → POST /api/chat → Flask(Render) → 速率限制檢查 → Gemini Agent 循環 → 回覆文字
                                ↓ function call
                           search_local_merchants / check_availability / book_experience
                                ↓ book_experience
                           POST n8n.cloud/webhook/nantou-booking
                                ↓
                           n8n：POST Supabase bookings → GET Supabase merchants(line_id)
                                → LINE push message（含 Quick Reply 按鈕）

商家 LINE Bot
  → LINE platform → POST n8n.cloud/webhook/line-merchant（直接打 n8n，不經 Flask）
                       ↓
                    解析訊息 → 商家登錄：POST Supabase merchants（artisan + line_id）
                             → 陌生訊息：回覆介紹文案

商家後台（基本驗證）
  → GET /merchant  → HTTP Basic Auth（MERCHANT_PASSWORD）→ 預約記錄列表
```

### `app.py` 關鍵細節

- **資料來源**：啟動時從 `南投深度游資源 (1).docx` 以 zipfile 解 XML 取純文字，存入 `MERCHANT_DATA`
- **隨機推薦**：`search_local_merchants` 每次呼叫用 `re.split` 切段後 `random.shuffle`，避免 AI 永遠推薦相同商家
- **Agent 循環**：`/api/chat` 內 `while True` 跑 `generate_content` → 有 `function_call` 就執行並回送結果 → 無 `function_call` 才 return 文字
- **容錯**：`candidate.parts` 可能為 `None`（Gemini 偶發），已加 guard；503 自動指數退避重試，fallback 到 `gemini-2.0-flash`
- **對話記憶**：前端維護 `conversationHistory[]`，每次請求帶 `history` 陣列（最多 20 輪），後端 rebuild `contents`
- **LINE webhook 代理**：`/webhook/line-merchant` 路由存在但 LINE Developers Console webhook URL 直接指向 n8n.cloud（不經 Flask 代理）
- **速率限制**：`check_rate(ip, limit=20, window=60)`，超過 20 req/min 回傳 429
- **商家後台**：`/merchant` 路由加 `require_auth` decorator，讀取 `MERCHANT_PASSWORD` 環境變數
- **日期解析**：`_parse_booking_date()` 先嘗試含年份格式，再嘗試無年份格式（自動補當年，若已過期則補明年）
- **預約驗證規則**：AI 絕對不可呼叫 `search_local_merchants` 驗證店名；日期字串原樣傳入，年份由後端處理

### System Prompt 關鍵行為規則

- `【絕對禁止】` 旅客提供店家名稱時，不可呼叫 `search_local_merchants` 驗證，一律直接進入預約流程
- `【日期傳遞規則】` 呼叫 `check_availability` / `book_experience` 時，日期字串原樣傳入（不自行補年份）
- `【互動選項格式】` 詢問人數、預算、區域時輸出 `[選項: A | B | C]` 格式，前端解析為可點擊按鈕
- 經典推薦及行程安排不詢問任何額外資訊，直接輸出

### `templates/index.html` 關鍵細節

- **手機優化**：hamburger 選單、聊天視窗全螢幕（88vh）、各 breakpoint（900px / 768px / 480px）響應式
- **聊天 UI**：開啟時顯示 6 個類別 inline chip（`pickCategory()` 送出並消失）；AI 回覆中的 `[選項: A | B | C]` 解析為 `.opt-chip` 按鈕（`selectOpt()` 點擊後消失並送出）
- **語言切換**：繁中 / EN / 日本語，切換後通知 AI 改換語言回覆
- **Leaflet 互動地圖**（`#map` section）：
  - CDN：`cdnjs.cloudflare.com/leaflet/1.9.4`（CSS + JS）
  - 底圖：OpenStreetMap（免 API key）
  - **46 個商家 markers** 完整對應資料庫，9 大類別色碼：
    - ☕ 咖啡職人 `#4A5D4E`、🍃 茶&酒莊 `#2D6A4F`、🎨 工藝手作 `#D85A30`
    - 🌾 農業休閒 `#C8A96E`、♨️ 秘境溫泉 `#3B82F6`、🍽️ 永續餐飲 `#E85D04`
    - 🏡 永續住宿 `#0F6E56`、🏔️ 部落文化 `#7C3AED`、🏛️ 宗教文化 `#92400E`
  - Popup 顯示：店名、描述、電話、「🤖 讓 AI 推薦」按鈕（呼叫 `askAI()` 開啟聊天並送出查詢）
  - `filterMap(cat)` 可按類別篩選，圖例點擊觸發
- **多語系內容**：`data-zh` / `data-en` / `data-ja` attribute 驅動切換

### n8n 工作流（n8n.cloud）

`n8n_booking_workflow.json`（預約流程，webhook path: `nantou-booking`）：
- Webhook1 → HTTP Request POST Supabase bookings → HTTP Request GET Supabase merchants（依 artisan 查 line_id）→ Code node（組 LINE push payload，`to: lineId`，`lineId = $input.item.json.line_id`）→ HTTP Request LINE push API
- 商家收到通知後有 **LINE Quick Reply 按鈕**：「✅ 確認預約」/ 「❌ 婉拒預約」（`確認/婉拒 {booking_id}` 訊息格式）
- Supabase merchants 的 `artisan` 必須與 AI 送出的 `artisan` 值**完全一致**，否則查不到 line_id

`n8n_merchant_onboarding.json`（商家加入，webhook path: `line-merchant`）：
- 商家傳 `商家：[名稱]` → POST Supabase merchants（artisan + line_id）→ 回覆登錄成功
- 傳 `我的ID` → 回覆 LINE User ID
- 陌生訊息 → 回覆介紹文案
- LINE Developers Console webhook URL 直接設為 `https://nantoutravel.app.n8n.cloud/webhook/line-merchant`

### Supabase 資料表

**bookings**（RLS disabled）：
- `id`（int8, primary key）、`booking_id`、`artisan`、`booking_date`、`people_count`、`customer_name`、`phone`、`email`

**merchants**（RLS disabled）：
- `id`（int8, primary key）、`artisan`（text）、`line_id`（text）

## 安全性注意事項

- `.env` 不進 git（含 `GEMINI_API_KEY`、`MERCHANT_PASSWORD`）
- `n8n_*.json` 含 LINE Channel Access Token，已加入 `.gitignore`
- 第一次 commit 的舊 Gemini API key 已曝光在 GitHub 歷史，需至 https://aistudio.google.com/app/apikey 手動撤銷
- 速率限制保護 `/api/chat`（20 req/min per IP）
- `/merchant` 後台以 HTTP Basic Auth 保護

## 資料庫商家清單（46 筆）

商家資料來自 `南投深度游資源 (1).docx`，共 9 大類別 46 筆，涵蓋：
- **咖啡職人**（5）：向陽咖啡莊園、日晨咖啡烘焙、佳芳咖啡、山豬衝吧咖啡館、百勝村咖啡莊園
- **茶&酒莊**（8）：威石東葡萄酒莊、台灣菸酒南投酒廠、蔡氏釀酒、吉臣茶廠、和菓森林茶廠、山中茶學、連勝茶廠、煜倫茶業
- **工藝手作**（9）：元泰竹藝社、武岫竹炭窯、龍南天然漆博物館、毓秀美術館、添興窯、水里蛇窯、葉寶蓮竹編、攻玉山房、大禾竹藝工坊（+ 林喜美賽德克織布）
- **農業休閒**（3）：大雁休閒農業區、桃米休閒農業區、糯米橋休閒農業區
- **秘境溫泉**（3）：秋山居、蟬說雅築、達谷蘭溫泉渡假村
- **永續餐飲**（4）：慢午廚房X野食、仕合廖家、山中小廚房、魚光窯烤麵包
- **永續住宿**（8）：陶花巷弄民宿、散步的雲、兩腳詩集生活旅店、四季時光、雲品溫泉酒店、承萬尊爵渡假酒店、日月潭力麗溫德姆、溪頭福華渡假飯店
- **部落文化**（3）：埔里巴宰原鄉文化園區、地利站&丹大布農生態旅遊、原夢觀光農園
- **宗教文化**（2）：寶湖宮天地堂地母廟、中台禪寺

## 套件

```powershell
pip install flask google-genai python-dotenv requests
```
