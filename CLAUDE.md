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
```

Render 環境變數（在 Render dashboard 設定）：
- `GEMINI_API_KEY`
- `N8N_WEBHOOK_URL`（預設 https://nantoutravel.app.n8n.cloud/webhook/nantou-booking）
- `SUPABASE_URL`（https://gbffodvtfirdtecsespm.supabase.co）
- `SUPABASE_KEY`（Supabase anon key）
- `GOOGLE_CLIENT_ID`（遊戲 Google 登入）
- `FLASK_SECRET_KEY`（登入 session 簽章；沒設的話每次重新部署所有人都會被登出）
- `N8N_CERT_WEBHOOK_URL`（預設 https://nantoutravel.app.n8n.cloud/webhook/nantou-certificate）
- `ADMIN_EMAILS`（管理後台管理員，逗號分隔）、`MODERATOR_EMAILS`（內容審核員，選填）
- `N8N_API_KEY`（後台讀取 n8n 工作流狀態，選填）

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

管理後台
  → GET /admin → Google 登入（ADMIN_EMAILS / MODERATOR_EMAILS）→ 從 Supabase 統計預約、商家、遊戲資料 + n8n API 狀態
```

### `app.py` 關鍵細節

- **資料來源**：啟動時從 `南投深度游資源 (1).docx` 以 zipfile 解 XML 取純文字，存入 `MERCHANT_DATA`
- **隨機推薦**：`search_local_merchants` 每次呼叫用 `re.split` 切段後 `random.shuffle`，避免 AI 永遠推薦相同商家
- **Agent 循環**：`/api/chat` 內 `while True` 跑 `generate_content` → 有 `function_call` 就執行並回送結果 → 無 `function_call` 才 return 文字
- **容錯**：`candidate.parts` 可能為 `None`（Gemini 偶發），已加 guard；503 自動指數退避重試，fallback 到 `gemini-2.0-flash`
- **對話記憶**：前端維護 `conversationHistory[]`，每次請求帶 `history` 陣列（最多 20 輪），後端 rebuild `contents`
- **速率限制**：`check_rate(ip, limit=20, window=60)`，超過 20 req/min 回傳 429
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

`n8n_certificate_workflow.json`（永續達人證書，webhook path: `nantou-certificate`）：
- Webhook → Gmail 節點（`emailType: html`），收件人、主旨、HTML 內容都由 Flask 傳入（`body.email` / `body.subject` / `body.html`）
- 匯入後需在「寄送證書」節點選擇 `Gmail 帳號` credential，並切成 Active

**圖片上傳注意事項**：
- Supabase Storage RLS：`storage.objects` 需有 anon INSERT policy，`WITH CHECK (bucket_id = 'event-image')`
- 上傳時**不可加** `x-upsert: true` header（會觸發 ON CONFLICT UPDATE，anon 無 UPDATE policy 會 RLS 失敗）
- 每個節點的 `apikey` header 需手動在 n8n UI 貼入，JSON import 不帶入 header 值

### 緣投探索者遊戲（`/game`）

- 後端在 `game.py`（Flask Blueprint，`app.py` 只做 `register_blueprint`），題庫在 `game_quiz.py`（40 題）
- **登入**：Google Identity Services → `POST /api/game/login` 驗 id_token → 寫入 Flask `session["gid"]`。所有寫入 API 都從 session 取使用者，**不信任前端傳的 google_id**
- **遊戲數值集中在 `game.py` 開頭**（`LEVELS`、`XP_*`、`QUIZ_ROUND_SIZE` 等），透過 `GAME_CFG` 傳給模板與前端 `CFG`，頁面文字自動跟著變；調數值只改這裡，不要在 `game.html` 寫死數字
- **等級**（目前最高 Lv.3，暫定）：旅人 0 → 探索者 300 → 永續達人 900 XP。最高等級的稱號固定為「永續達人」，因為證書綁定最高等級
- **XP**（已 ×3）：打卡 +240（Gemini Vision 驗人臉，每商家一次）；問答答對 +150、連擊每題 +30、答錯 +30、全對 +300；發文 +60（每日前 3 篇）
- **問答大挑戰**：每局 10 題、每題 20 秒、可無限局。注意目前一局全對約 +3,150 XP，一局就能到最高等級；正式上線前應提高門檻
- **問答 session** 存在記憶體 `QUIZ_SESSIONS`，答案不會送到前端；伺服器重啟會清空進行中的局
- **證書**：首次達到最高等級時（`certificate_sent` 為 false），`award_xp()` 開 thread POST 到 n8n `nantou-certificate` webhook（`n8n_certificate_workflow.json`），由 Gmail OAuth2 寄出 HTML 證書；`/game/certificate/<google_id>` 可線上查看
- **圖片**：打卡與貼文照片上傳到 Supabase Storage `event-image` bucket 的 `checkins/`、`posts/` 資料夾
- **前端** `templates/game.html`：左側抽屜選單（≥1000px 預設展開）、6 個頁面（主頁/打卡/問答/動態/排行/證書）、緣投浮動對話、升級彩帶動畫；所有使用者內容都經 `esc()` 轉義

### 管理後台（`/admin`）

- 後端在 `admin.py`（Flask Blueprint），前端 `templates/admin.html`（Chart.js 4.4.1 + Leaflet）
- **登入**：Google 登入 → `POST /admin/api/login`，email 必須已驗證且在 `ADMIN_EMAILS`（管理員）或 `MODERATOR_EMAILS`（內容審核，只能看遊戲分析）名單內；每次請求都重新比對名單，移出名單即失效
- **頁面**：總覽、預約分析（含全部預約清單：搜尋、篩選、分頁、匯出 CSV）、商家表現、地圖視角、永續影響力、遊戲分析、系統健康
- **地圖圖示**：Font Awesome Free 6.5.1（cdnjs），每個類別一個圖示，對照表在 `admin.html` 的 `CAT_ICON`
- **資料**：`load_data()` 分頁讀取 Supabase 全表（每頁 1000 筆），在 Flask 端統計，快取 60 秒；`?fresh=1`（右上角重新整理）略過快取
- **商家名稱比對**：`match_merchant()` 依序做完全比對、包含比對、`difflib` 模糊比對（cutoff 0.7），把 AI 送出的店名對應回 `merchants_data` 的 46 家；對不到的列在「商家表現」頁
- **地圖座標**：商家座標放在 `static/js/locations.js`（首頁與後台共用），後台前端用 bigram 相似度把座標對應到統計資料
- **n8n 狀態**：需要 `N8N_API_KEY`（n8n Settings → n8n API），讀 `/api/v1/workflows` 與 `/api/v1/executions`；沒設定時頁面會顯示設定步驟
- **稽核紀錄**：登入、登出、被拒登入寫入 `admin_audit_log` 表
- 尚未提供：收入估算（缺商家價格表）、AI 嚮導分析與題目答對率（缺記錄表），列在第二批

### Supabase 資料表

**bookings**（RLS disabled）：
- `id`（int8, primary key）、`booking_id`、`artisan`、`booking_date`、`people_count`、`customer_name`、`phone`、`email`、`status`（text, 預設「待確認」）

**merchants**（RLS disabled）：
- `id`（int8, primary key）、`artisan`（text）、`line_id`（text）

**events**（RLS disabled）：
- `id`（int8, primary key）、`artisan`（text）、`title`（text）、`event_date`（text）、`capacity`（int）、`price`（int）、`description`（text）、`active`（bool）、`image_url`（text）

**game_users / checkins / game_posts / game_likes / game_comments**（RLS disabled）：遊戲用，欄位見 `game.py`

**admin_audit_log**（RLS disabled）：`email`、`action`、`detail`（jsonb）、`created_at`

**Supabase Storage**：
- bucket `event-image`（public）：商家透過 LINE Bot 上傳的活動照片

## 安全性注意事項

- `.env` 不進 git（含 `GEMINI_API_KEY`）
- `n8n_*.json` 含 LINE Channel Access Token，已加入 `.gitignore`
- 速率限制保護 `/api/chat`（20 req/min per IP）
- `/admin` 管理後台以 Google 登入 + email 白名單保護（舊的 `/merchant` 共用密碼後台已移除）

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
