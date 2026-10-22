# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案說明

南投在地職人永續旅遊 AI 嚮導。前端是完整旅遊網站，右下角浮動按鈕可展開聊天視窗，AI 可呼叫工具搜尋商家、查詢空檔、完成預約。預約透過 n8n webhook 寫入 Supabase 並推播 LINE 通知給商家。商家確認或婉拒後，系統自動寄 email 通知旅客。商家可透過 LINE Bot 登錄活動、上傳活動照片，照片自動顯示在網站輪播。吉祥物「緣投」圓形頭像用於 nav logo、FAB 按鈕、聊天視窗標頭（圖檔：`static/images/mascot.jpg`）。

另有兩個子系統：
- **緣投探索者遊戲**（`/game`）：Google 登入，打卡（AI 驗人臉）、問答大挑戰、旅人動態、排行榜、升到最高等級寄永續達人證書
- **管理後台**（`/admin`）：Google 登入 + email 白名單，營運與遊戲數據儀表板，以及預約、LINE 綁定、首頁輪播、公告、遊戲設定、題庫、動態、玩家、限時活動的編輯；內建「問緣投」AI 助理（讀取後台數據回答管理員問題）、AI 出題、貼文 AI 自動審核

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

- Flask：`http://127.0.0.1:5001`（`debug=False`，**模板有快取，改了 `templates/` 或 `.py` 都要重啟伺服器**才看得到）
- n8n.cloud 常駐，不需本機啟動
- Supabase 常駐，不需本機啟動
- 本機 `.env` 預設只有 `GEMINI_API_KEY`：沒有 `SUPABASE_KEY` 時，遊戲、後台、輪播讀不到資料（API 回空陣列，不會報錯）；要用真實資料請在 `.env` 補上 `SUPABASE_KEY`
- 本機要測 Google 登入，需要 `GOOGLE_CLIENT_ID`，且 Google Cloud Console 的 Authorized JavaScript origins 要加 `http://localhost:5001`
- 後台登入還需要 `ADMIN_EMAILS`

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

### 檔案結構

| 檔案 | 用途 |
|------|------|
| `app.py` | Flask 主程式：首頁、AI 嚮導 `/api/chat`、預約工具、行程分享、評價頁；最後註冊 `game_bp`、`admin_bp` |
| `game.py` | 遊戲 Blueprint：`/game`、`/api/game/*`；也提供共用工具（Supabase 讀寫 `sb_*`、`settings()`、`site_settings()`、`send_email()`、`upload_image()`） |
| `admin.py` | 後台 Blueprint：`/admin`、`/admin/api/*`（統計、編輯、稽核） |
| `game_quiz.py` | 內建 40 題問答 |
| `merchants_data.py` | 46 家商家的正式名錄（名稱、地址、電話、介紹、標籤、9 大類別），是全站唯一的商家資料來源 |
| `merchant_directory.py` | 商家名錄的索引、店名比對與檢索（AI 嚮導、預約、後台共用） |
| `static/js/locations.js` | 46 家商家的地圖座標（首頁地圖與後台地圖共用） |
| `templates/index.html` / `category.html` / `game.html` / `admin.html` | 首頁、分類頁、遊戲頁、後台 |
| `n8n_*.json` | n8n 工作流匯出檔（含 token，不進 git） |

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

遊戲玩家
  → GET /game → Google 登入 → /api/game/*（打卡、問答、動態、檢舉、排行、限時活動）→ Supabase game_* 表
     打卡照片 → Gemini Vision 驗人臉；貼文與留言 → 禁用詞 + Gemini 內容審核
     升到最高等級 → POST n8n.cloud/webhook/nantou-certificate → Gmail 寄證書

管理後台
  → GET /admin → Google 登入（ADMIN_EMAILS / MODERATOR_EMAILS）→ 從 Supabase 統計預約、商家、遊戲資料 + n8n API 狀態
     編輯操作 → 寫入對應的 Supabase 表 + admin_audit_log
```

### `app.py` 關鍵細節

- **單一資料來源**：AI 嚮導、網站、遊戲、後台都以 `merchants_data.py` 為準，透過 `merchant_directory.py` 存取（`MERCHANTS` 索引、`match_merchant()` 店名比對、`search()` 檢索、`format_for_ai()`）。`南投深度游資源 (1).docx` 只是原始資料，**程式已不再讀取**
- **結構化檢索**：`search_local_merchants(category, township, keyword)` 依類別、鄉鎮、關鍵字篩選，結果隨機排序避免永遠推薦同幾家；會附上首頁輪播上架中的活動（快取 5 分鐘）。名錄沒有價格資料，系統指示要求 AI 不可編造金額
- **正式店名**：`check_availability`、`book_experience` 先用 `official_name()` 把 AI 送出的店名校正成名錄正式名稱，n8n 才查得到商家的 LINE ID
- **Agent 循環**：`/api/chat` 只做速率限制與記錄，實際對話在 `run_chat(user_message, history, lang, state)`（**必須**設定 `automatic_function_calling=AutomaticFunctionCallingConfig(disable=True)`，否則 google-genai 會自動執行工具，手動迴圈與 `chat_logs` 的工具紀錄都收不到呼叫）：`while True` 跑 `generate_content` → 有 `function_call` 就執行並回送結果 → 無 `function_call` 才回傳文字；回傳 `(回覆, 是否錯誤)`，`state` 記下呼叫過的工具與是否完成預約
- **使用紀錄**：每次回覆後背景寫入 `chat_logs`（`session_id`、語言、`chat_category()` 解析出的類別、工具、是否預約、耗時），**不存對話內容**
- **AI 補充指示**：`run_chat()` 會把後台設定的 `site_settings()["ai_extra"]` 接在系統指示最後
- **首頁公告**：`index()` 把後台設定的公告（開啟且有內容時）以 `announcement` 傳給模板
- **容錯**：`candidate.parts` 可能為 `None`（Gemini 偶發），已加 guard；503 自動指數退避重試，fallback 到 `gemini-2.0-flash`
- **對話記憶**：前端維護 `conversationHistory[]`，每次請求帶 `history` 陣列（最多 20 輪），後端 rebuild `contents`
- **速率限制**：`check_rate(ip, limit=20, window=60)`，超過 20 req/min 回傳 429
- **日期解析**：`_parse_booking_date()` 先嘗試含年份格式，再嘗試無年份格式（自動補當年，若已過期則補明年）
- **預約驗證規則**：AI 絕對不可呼叫 `search_local_merchants` 驗證店名；日期字串原樣傳入，年份由後端處理
- **活動 API**：`GET /api/events` 從 Supabase events 撈 active=true 的活動，前端輪播用

### System Prompt 關鍵行為規則

- `【絕對禁止】` 旅客提供店家名稱時，不可呼叫 `search_local_merchants` 驗證，一律直接進入預約流程
- `【日期傳遞規則】` 呼叫 `check_availability` / `book_experience` 時，日期字串原樣傳入（不自行補年份）
- `【互動選項格式】` 詢問人數、區域時輸出 `[選項: A | B | C]` 格式，前端解析為可點擊按鈕（預算選項已移除，因為名錄沒有價格）
- `【搜尋方式】` 旅客指定區域帶 `township`、指定體驗類型帶 `category` 或 `keyword`；分類卡片（`【分類探索】`）直接把類別名稱填入 `category`
- `【價格】` 名錄沒有價格資料，不可編造金額；只有「🎉 近期活動」有標示費用
- `【正式店名】` 推薦與預約時使用搜尋結果 `【】` 內的正式店名
- 經典推薦及行程安排不詢問任何額外資訊，直接輸出

### `templates/index.html` 關鍵細節

- **手機優化**：hamburger 選單、聊天視窗全螢幕（88vh）、各 breakpoint（900px / 768px / 480px）響應式
- **聊天 UI**：開啟時顯示 6 個類別 inline chip（`pickCategory()` 送出並消失）；AI 回覆中的 `[選項: A | B | C]` 解析為 `.opt-chip` 按鈕（`selectOpt()` 點擊後消失並送出）
- **語言切換**：繁中 / EN / 日本語，切換後通知 AI 改換語言回覆
- **聊天 session**：每次開頁產生 `chatSessionId`，隨 `/api/chat` 送出，用於後台 AI 嚮導分析
- **遊戲入口**：導覽列流光按鈕 `.glow-btn.nav-game`（手機顯示「玩遊戲」）、CTA 前的滿版 `.game-banner`；文字裡的 XP、等級數字來自 `game_cfg`，不要寫死
- **公告列**：`announcement` 存在時顯示浮動 `.site-ann`，訪客關閉後以 sessionStorage 記住（公告文字改變會重新顯示）
- **頁尾**：「🔒 管理員登入」連到 `/admin`
- **活動輪播**（`#ad-carousel`）：
  - `_loadAds()` 在 DOMContentLoaded 呼叫 `GET /api/events`
  - `_buildSlide()` 判斷 `ev.image_url`：有圖則顯示全版照片 + 半透明漸層文字列；無圖則顯示色塊卡片
  - 不足 3 張時補 placeholder 卡片（待推廣廣告位）
- **Leaflet 互動地圖**（`#map` section）：
  - CDN：`cdnjs.cloudflare.com/leaflet/1.9.4`（CSS + JS）
  - 商家座標陣列 `LOCATIONS` 在 `static/js/locations.js`（後台地圖也用同一份）
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

- 後端在 `game.py`（Flask Blueprint，`app.py` 只做 `register_blueprint`）；`game_quiz.py` 是內建 40 題（題庫表 `game_questions` 是空的時才使用，也是後台「匯入內建題庫」的來源）
- **登入**：Google Identity Services → `POST /api/game/login` 驗 id_token → 寫入 Flask `session["gid"]`。所有寫入 API 都從 session 取使用者，**不信任前端傳的 google_id**
- **遊戲數值由後台「遊戲設定」頁修改**，存在 `game_settings` 表（key=`game`）；`game.settings()` 讀取並快取 60 秒，讀不到就用 `DEFAULT_SETTINGS`。程式一律透過 `settings()` / `levels()` / `public_cfg()` 取值，模板拿到 `cfg`（遊戲頁）或 `game_cfg`（全站），**不要在程式或 `game.html` 寫死數字**
- **預設值**：最高 Lv.3（旅人 0 → 探索者 300 → 永續達人 900 XP）；打卡 +240、答對 +150、連擊每題 +30、答錯 +30、全對 +300、發文 +60（每日前 3 篇）、每局 10 題、每題 20 秒。後台有平衡試算器，會警告「一局就能拿證書」這類數值
- **最高等級就是發證書的等級**（等級數可在後台增減）
- **題庫**：`question_bank()` 讀 `game_questions` 的啟用題目（快取 60 秒）；表是空的時用內建題目。每次作答寫入 `quiz_answers`（題目 id、選項、對錯），後台據此算答對率並標記「太簡單 / 太難 / 答案可能有誤」
- **問答 session** 存在記憶體 `QUIZ_SESSIONS`，答案不會送到前端；伺服器重啟會清空進行中的局
- **證書**：首次達到最高等級時（`certificate_sent` 為 false），`award_xp()` 開 thread POST 到 n8n `nantou-certificate` webhook（`n8n_certificate_workflow.json`），由 Gmail OAuth2 寄出 HTML 證書；`/game/certificate/<google_id>` 可線上查看
- **圖片**：打卡與貼文照片上傳到 Supabase Storage `event-image` bucket 的 `checkins/`、`posts/` 資料夾
- **動態**：`game_posts.hidden` 為 true 的貼文、`game_comments.hidden` 為 true 的留言玩家看不到；`pinned` 的貼文排在第一頁最上方
- **內容審核**：發文、留言先比對禁用詞，再由 `moderate()` 呼叫 Gemini 判斷（AI 出錯時放行）。被標記的內容 `hidden=true`、`moderation` 設為 `keyword` / `ai`，不給 XP，回傳 `pending_review` 給前端
- **檢舉**：`POST /api/game/report`，同一玩家對同一內容只算一次；累積達 `report_hide_threshold` 次自動隱藏（`moderation=reports`）。管理員核可後 `moderation=approved`，之後不會再被自動隱藏
- **暫停發言**：`game_users.muted` 為 true 時不能發文、留言（403）
- **限時活動**：`game_events` 表，`xp_multiplier(kind, category)` 取進行中活動的最高倍率（不相乘）；有指定商家類別的活動只套用在該類別打卡。活動時間以 UTC 存，後台以台灣時間輸入與顯示
- **XP 最低為 0**：`award_xp()` 會把 XP 和計數欄位夾在 0 以上（後台可扣分）
- **前端** `templates/game.html`：左側抽屜選單（≥1000px 預設展開）、6 個頁面（主頁/打卡/問答/動態/排行/證書）、緣投浮動對話、升級彩帶動畫；所有使用者內容都經 `esc()` 轉義

### 管理後台（`/admin`）

- 後端在 `admin.py`（Flask Blueprint），前端 `templates/admin.html`（Chart.js 4.4.1 + Leaflet）
- **登入**：Google 登入 → `POST /admin/api/login`，email 必須已驗證且在 `ADMIN_EMAILS`（管理員）或 `MODERATOR_EMAILS`（內容審核：見下方權限說明）名單內；每次請求都重新比對名單，移出名單即失效
- **頁面**：總覽、預約分析（含全部預約清單：搜尋、篩選、分頁、匯出 CSV）、商家表現、地圖視角、永續影響力、AI 嚮導、遊戲分析、遊戲設定、題目管理、動態管理（含待審核佇列）、玩家管理、限時活動、首頁輪播、LINE 綁定、公告與 AI 指示、系統健康、操作紀錄；側邊欄分「營運 / 遊戲 / 網站 / 系統」可收合群組
- **可編輯的資料**：遊戲設定（等級、XP、題數、檢舉門檻、AI 審核開關、禁用詞；可勾選立即補寄證書）、題目（新增 / 編輯 / 停用 / 刪除 / CSV 匯入 / AI 出題）、貼文與留言（核可 / 駁回 / 隱藏 / 置頂 / 刪除）、限時活動、玩家（調整 XP、撤銷打卡、暫停發言、補寄證書）。停用或刪除題目時，若啟用題數會少於每局題數會被擋下
- **權限**：內容審核員可用遊戲分析、題目管理（含 AI 出題）、動態管理、玩家管理（只能暫停發言，看不到 Email）；遊戲設定、限時活動、調整 XP、撤銷打卡、補寄證書只有管理員能用
- **營運資料編輯（只有管理員）**：
  - 預約：編輯（狀態、日期、人數、聯絡資料）、刪除、補登。補登時勾「用 LINE 通知商家」會改打 n8n `nantou-booking` webhook（由 n8n 寫入 bookings，Flask 不重複寫入）；改狀態時可勾選寄 email 通知旅客
  - 商家 LINE 綁定（`merchants` 表）：店名只能從 46 家名單選（必須和 AI 送出的店名完全一致，n8n 才查得到 line_id），LINE ID 需符合 `U` + 32 個英數字
  - 首頁輪播（`events` 表）：上下架、編輯、換照片（上傳到 `event-image/carousel/`）、刪除
  - 公告與 AI 指示：存在 `game_settings` 表 key=`site`；`game.site_settings()` 快取 60 秒。首頁 `index()` 傳入 `announcement` 顯示浮動公告列；`run_chat()` 把 `ai_extra` 接在系統指示最後
  - 操作紀錄頁：唯讀檢視 `admin_audit_log` 最新 300 筆
- **寄信**：`game.send_email(email, subject, html)` 透過 n8n `nantou-certificate` webhook 寄任意 HTML 信（證書與預約通知共用）
- **AI 出題**：`/admin/api/questions/generate` 只用選定商家或類別在 `merchants_data` 的介紹當素材，產生的題目是草稿，管理員勾選後才透過 bulk API 寫入題庫
- **AI 嚮導分析**：`/api/chat` 每次回覆後把語言、點選的類別、呼叫的工具、是否完成預約、耗時寫入 `chat_logs`（**不存對話內容**）；前端每次開頁產生一個 `session_id` 用來算對話數與轉換漏斗
- **地圖圖示**：Font Awesome Free 6.5.1（cdnjs），每個類別一個圖示，對照表在 `admin.html` 的 `CAT_ICON`
- **資料**：`load_data()` 分頁讀取 Supabase 全表（每頁 1000 筆），在 Flask 端統計，快取 60 秒；`?fresh=1`（右上角重新整理）略過快取
- **商家名稱比對**：`merchant_directory.match_merchant()` 依序做完全比對、包含比對、`difflib` 模糊比對（cutoff 0.7），把店名對應回 `merchants_data` 的 46 家；對不到的列在「商家表現」頁
- **後台 AI 助理**：右下角「問緣投」開啟側邊對話框，`POST /admin/api/assistant`。Gemini 只能呼叫 `ASSISTANT_TOOLS` 裡的唯讀工具（包裝既有統計函式，旅客姓名遮蔽、不含電話與 Email），依角色給工具（內容審核員只有遊戲分析、待審核、題目品質）；每次最多 6 輪工具呼叫、每人每分鐘 15 次；回答附上資料來源；提問寫入稽核紀錄。新增統計頁時，可順便在 `ASSISTANT_TOOLS` 加對應工具，並用明確的欄位名稱（例如 `pending_bookings_all_time`），避免 AI 誤解期間
- **地圖座標**：商家座標放在 `static/js/locations.js`（首頁與後台共用），後台前端用 bigram 相似度把座標對應到統計資料
- **n8n 狀態**：需要 `N8N_API_KEY`（n8n Settings → n8n API），讀 `/api/v1/workflows` 與 `/api/v1/executions`；沒設定時頁面會顯示設定步驟
- **稽核紀錄**：登入、登出、被拒登入，以及所有設定、題目、貼文、留言的修改都寫入 `admin_audit_log` 表
- 尚未提供：收入估算（缺商家價格表）

### Supabase 資料表

**bookings**（RLS disabled）：
- `id`（int8, primary key）、`booking_id`、`artisan`、`booking_date`、`people_count`、`customer_name`、`phone`、`email`、`status`（text, 預設「待確認」）

**merchants**（RLS disabled）：
- `id`（int8, primary key）、`artisan`（text）、`line_id`（text）

**events**（RLS disabled）：
- `id`（int8, primary key）、`artisan`（text）、`title`（text）、`event_date`（text）、`capacity`（int）、`price`（int）、`description`（text）、`active`（bool）、`image_url`（text）

**game_users / checkins / game_posts / game_likes / game_comments**（RLS disabled）：遊戲用，欄位見 `game.py`

**admin_audit_log**（RLS disabled）：`email`、`action`、`detail`（jsonb）、`created_at`

**game_settings**（`key` 主鍵、`value` jsonb）/ **game_settings_history** / **game_questions** / **quiz_answers** / **chat_logs**（RLS disabled）：後台第二批用，欄位見 `admin.py`；`game_posts`、`game_comments` 另有 `hidden`、`pinned`、`report_count`、`moderation`、`moderation_reason` 欄位

**game_reports** / **game_events** / **xp_adjustments**（RLS disabled）：檢舉、限時活動、XP 手動調整紀錄；`game_users.muted` 為暫停發言

**Supabase Storage**：
- bucket `event-image`（public）：根目錄是商家透過 LINE Bot 上傳的活動照片；`checkins/` 打卡照片、`posts/` 貼文照片、`carousel/` 後台上傳的輪播照片

## 開發注意事項

- **部署順序**：新程式會寫入的 Supabase 欄位或資料表，必須**先在 Supabase SQL Editor 建好再推上 Render**（例如貼文的 `hidden`、`moderation` 欄位不存在時，篩選或發文會整個失敗）。新建的表一律 `DISABLE ROW LEVEL SECURITY`，與現有表一致
- **Jinja 模板裡的 JavaScript**：不要寫出 `{{`、`{%`（例如 `${{ a: 1 }[k]}`），Jinja 會當成模板語法；需要時先存成變數
- **HTML 屬性或樣板字串裡的換行**：JS 字串、正規表示式要用跳脫寫法 `\n` 或 `String.fromCharCode(10)`，不可出現真正的換行字元，否則整段 `<script>` 會語法錯誤、頁面所有功能失效。改完 `admin.html` / `game.html` 建議抽出 `<script>` 用 `node --check` 檢查
- **Gemini 工具呼叫**：用 Python 函式當工具時，一定要關掉自動呼叫（`AutomaticFunctionCallingConfig(disable=True)`），自己跑工具迴圈；工具函式的 docstring 就是給 AI 看的說明，參數用法要寫清楚
- **給 AI 的資料**：欄位名稱要明確寫出期間與範圍（例如 `pending_bookings_all_time`、`bookings_last_7_days`），否則 AI 容易把全部數字講成「本週」
- **XSS**：使用者內容一律轉義（前端 `esc()`），不要用 `innerHTML` 直接插入未轉義的字串
- **專案沒有自動化測試**：改完請重啟本機伺服器實際操作；沒有 `SUPABASE_KEY` 時資料相關功能會是空的

## 安全性注意事項

- `.env` 不進 git（含 `GEMINI_API_KEY`）
- `n8n_*.json` 含 LINE Channel Access Token，已加入 `.gitignore`
- 速率限制保護 `/api/chat`（20 req/min per IP）
- `/admin` 管理後台以 Google 登入 + email 白名單保護（舊的 `/merchant` 共用密碼後台已移除）；所有修改寫入 `admin_audit_log`
- 遊戲與後台 API 一律從 Flask session 取得身分，不信任前端傳來的使用者 ID
- 使用者產生的內容（貼文、留言、公告、通知信）在前端用 `esc()`、在 Jinja 用自動轉義、在 Python 用 `html.escape` 處理；公告連結只接受 `https://` 或 `/` 開頭
- `chat_logs` 不存對話內容；內容審核員看不到玩家 Email
- `FLASK_SECRET_KEY`、`N8N_API_KEY` 只放 Render 環境變數，不進程式碼

## 資料庫商家清單（46 筆）

商家資料在 `merchants_data.py`（原始資料整理自 `南投深度游資源 (1).docx`），共 9 大類別 46 筆，涵蓋：
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
