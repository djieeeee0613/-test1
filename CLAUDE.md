# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案說明

南投在地職人永續旅遊 AI 嚮導。前端是完整旅遊網站，右下角浮動按鈕可展開聊天視窗，AI 可呼叫工具搜尋商家、查詢空檔、完成預約。預約透過 n8n webhook 寫入 Supabase 並推播 LINE 通知給商家。商家確認或婉拒後，系統自動寄 email 通知旅客。商家可透過 LINE Bot 登錄活動、上傳活動照片，照片自動顯示在網站輪播。吉祥物「緣投」圓形頭像用於 nav logo、FAB 按鈕、聊天視窗標頭（圖檔：`static/images/mascot.jpg`）。

## 雲端部署架構

| 服務 | 平台 | URL |
|------|------|-----|
| Flask 前後端 | Render | https://test1-yipa.onrender.com |
| 自訂網域 | Render | https://nantoutravel.com |
| n8n 工作流 | n8n.cloud | https://nantoutravel.app.n8n.cloud |
| 資料庫 | Supabase | https://gbffodvtfirdtecsespm.supabase.co |
| 圖片儲存 | Supabase Storage | bucket: `event-image`（public） |

## 本機開發啟動

```powershell
cd "C:\Users\user\Desktop\dj\黑客松"
python app.py
```

- Flask：`http://127.0.0.1:5001`（debug 模式，存檔自動重載）
- n8n.cloud 常駐，不需本機啟動
- Supabase 常駐，不需本機啟動

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
  → GET /            → Flask(Render) → templates/index.html（旅遊網站 + 聊天視窗 + Leaflet 地圖）
  → GET /api/events  → Flask(Render) → Supabase events → 活動輪播資料（含 image_url）
  → POST /api/chat   → Flask(Render) → 速率限制檢查 → Gemini Agent 循環 → 回覆文字
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
                             → 活動登錄：POST Supabase events（artisan + title + date + ...）
                             → 圖片上傳：GET LINE Content API → POST Supabase Storage
                                         → PATCH Supabase events.image_url
                             → 確認/婉拒 {booking_id}：PATCH Supabase bookings.status
                                         → GET Supabase bookings（取 email/name/artisan/date）
                                         → Gmail OAuth2 寄信給旅客
                             → 陌生訊息：回覆介紹文案

商家後台（基本驗證）
  → GET /merchant → HTTP Basic Auth（MERCHANT_PASSWORD）→ 從 Supabase bookings 讀取預約記錄
```

### `app.py` 關鍵細節

- **資料來源**：啟動時從 `南投深度游資源 (1).docx` 以 zipfile 解 XML 取純文字，存入 `MERCHANT_DATA`
- **隨機推薦**：`search_local_merchants` 每次呼叫用 `re.split` 切段後 `random.shuffle`，避免 AI 永遠推薦相同商家
- **Agent 循環**：`/api/chat` 內 `while True` 跑 `generate_content` → 有 `function_call` 就執行並回送結果 → 無 `function_call` 才 return 文字
- **容錯**：`candidate.parts` 可能為 `None`（Gemini 偶發），已加 guard；503 自動指數退避重試，fallback 到 `gemini-2.0-flash`
- **對話記憶**：前端維護 `conversationHistory[]`，每次請求帶 `history` 陣列（最多 20 輪），後端 rebuild `contents`
- **速率限制**：`check_rate(ip, limit=20, window=60)`，超過 20 req/min 回傳 429
- **商家後台**：`/merchant` 路由加 `require_auth` decorator，讀取 `MERCHANT_PASSWORD` 環境變數，從 Supabase bookings 撈資料
- **日期解析**：`_parse_booking_date()` 先嘗試含年份格式，再嘗試無年份格式（自動補當年，若已過期則補明年）
- **預約驗證規則**：AI 絕對不可呼叫 `search_local_merchants` 驗證店名；日期字串原樣傳入，年份由後端處理
- **活動 API**：`GET /api/events` 從 Supabase events 撈 active=true 的活動，前端輪播用

### System Prompt 關鍵行為規則

- `【絕對禁止】` 旅客提供店家名稱時，不可呼叫 `search_local_merchants` 驗證，一律直接進入預約流程
- `【日期傳遞規則】` 呼叫 `check_availability` / `book_experience` 時，日期字串原樣傳入（不自行補年份）
- `【互動選項格式】` 詢問人數、預算、區域時輸出 `[選項: A | B | C]` 格式，前端解析為可點擊按鈕
- 經典推薦及行程安排不詢問任何額外資訊，直接輸出

### `templates/index.html` 關鍵細節

- **手機優化**：hamburger 選單、聊天視窗全螢幕（88vh）、各 breakpoint（900px / 768px / 480px）響應式
- **聊天 UI**：開啟時顯示 6 個類別 inline chip（`pickCategory()` 送出並消失）；AI 回覆中的 `[選項: A | B | C]` 解析為 `.opt-chip` 按鈕（`selectOpt()` 點擊後消失並送出）
- **語言切換**：繁中 / EN / 日本語，切換後通知 AI 改換語言回覆
- **活動輪播**（`#ad-carousel`）：
  - `_loadAds()` 在 DOMContentLoaded 呼叫 `GET /api/events`
  - `_buildSlide()` 判斷 `ev.image_url`：有圖則顯示全版照片 + 半透明漸層文字列；無圖則顯示色塊卡片
  - 不足 3 張時補 placeholder 卡片（待推廣廣告位）
- **Leaflet 互動地圖**（`#map` section）：
  - CDN：`cdnjs.cloudflare.com/leaflet/1.9.4`（CSS + JS）
  - 底圖：OpenStreetMap（免 API key）
  - **46 個商家 markers**，9 大類別色碼：
    - ☕ 咖啡職人 `#4A5D4E`、🍃 茶&酒莊 `#2D6A4F`、🎨 工藝手作 `#D85A30`
    - 🌾 農業休閒 `#C8A96E`、♨️ 秘境溫泉 `#3B82F6`、🍽️ 永續餐飲 `#E85D04`
    - 🏡 永續住宿 `#0F6E56`、🏔️ 部落文化 `#7C3AED`、🏛️ 宗教文化 `#92400E`
  - Popup 顯示：店名、描述、電話、「🤖 讓 AI 推薦」按鈕（呼叫 `askAI()` 開啟聊天並送出查詢）
  - `filterMap(cat)` 可按類別篩選，圖例點擊觸發
- **多語系內容**：`data-zh` / `data-en` / `data-ja` attribute 驅動切換

### n8n 工作流（n8n.cloud）

`n8n_booking_workflow.json`（預約流程，webhook path: `nantou-booking`）：
- Webhook → POST Supabase bookings → GET Supabase merchants（依 artisan 查 line_id）→ LINE push message
- 商家收到通知後有 **LINE Quick Reply 按鈕**：「✅ 確認預約」/ 「❌ 婉拒預約」（`確認/婉拒 {booking_id}` 訊息格式）
- Supabase merchants 的 `artisan` 必須與 AI 送出的 `artisan` 值**完全一致**，否則查不到 line_id

`n8n_merchant_onboarding.json`（商家加入 + 活動管理，webhook path: `line-merchant`）：
- `商家：[名稱]` → POST Supabase merchants（artisan + line_id）
- `活動登錄\n名稱：...\n日期：...\n名額：...\n費用：...\n描述：...` → POST Supabase events
- 圖片訊息 → GET LINE Content API（binary）→ POST Supabase Storage `event-image/{userId}_{eventId}_{ts}.jpg` → PATCH events.image_url
- `確認 {booking_id}` → PATCH bookings.status=已確認 → GET bookings → Gmail 寄確認信給旅客
- `婉拒 {booking_id}` → PATCH bookings.status=已婉拒 → GET bookings → Gmail 寄婉拒信給旅客
- `我的ID` → 回覆 LINE User ID
- 陌生訊息 → 回覆介紹文案
- LINE Developers Console webhook URL：`https://nantoutravel.app.n8n.cloud/webhook/line-merchant`

**Email 通知注意事項**：
- 使用 **Gmail OAuth2 credential**（n8n Gmail node），不用 SMTP（n8n.cloud 有 IPv6 連線問題）
- 確認/婉拒 email 節點：`n8n-nodes-base.gmail`，credential 名稱 `Gmail 帳號`
- `To Email` 和 `message` 欄位用 `$json.email`、`$json.customer_name` 等（HTTP Request 回傳陣列後 n8n 自動拆成 individual item，不需 `$json[0].xxx`）

**圖片上傳注意事項**：
- Supabase Storage RLS：`storage.objects` 需有 anon INSERT policy，`WITH CHECK (bucket_id = 'event-image')`
- 上傳時**不可加** `x-upsert: true` header（會觸發 ON CONFLICT UPDATE，anon 無 UPDATE policy 會 RLS 失敗）
- 每個節點的 `apikey` header 需手動在 n8n UI 貼入，JSON import 不帶入 header 值

### Supabase 資料表

**bookings**（RLS disabled）：
- `id`（int8, primary key）、`booking_id`、`artisan`、`booking_date`、`people_count`、`customer_name`、`phone`、`email`、`status`（text, 預設「待確認」）

**merchants**（RLS disabled）：
- `id`（int8, primary key）、`artisan`（text）、`line_id`（text）

**events**（RLS disabled）：
- `id`（int8, primary key）、`artisan`（text）、`title`（text）、`event_date`（text）、`capacity`（int）、`price`（int）、`description`（text）、`active`（bool）、`image_url`（text）

**Supabase Storage**：
- bucket `event-image`（public）：商家透過 LINE Bot 上傳的活動照片

## 安全性注意事項

- `.env` 不進 git（含 `GEMINI_API_KEY`、`MERCHANT_PASSWORD`）
- `n8n_*.json` 含 LINE Channel Access Token，已加入 `.gitignore`
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
