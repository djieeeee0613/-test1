# AI 簡報導師 · PPT Tutor

丟一份簡報進去，AI 當家教一頁一頁帶你讀。英文簡報自動翻成中文講解，並且會記得前面講過什麼，把每一頁串起來。

## 用法

1. 直接用瀏覽器打開 `index.html`（雙擊即可，不需要伺服器）。
2. 第一次會跳出「設定」：
   - 選 **AI 供應商**（Google Gemini 或 OpenAI 相容端點）
   - 貼上你自己的 **API 金鑰**
     - Gemini 免費金鑰：<https://aistudio.google.com/app/apikey>
     - OpenAI：<https://platform.openai.com/api-keys>
   - 模型留空會用預設（`gemini-2.5-flash` / `gpt-4o-mini`）
   - 講解語言、導師 Prompt 都可微調（Prompt 已預先寫好）
3. 把 **PDF**（最推薦）、PPTX 或圖片拖進左側視窗。
4. 用 `‹ ›` 或鍵盤方向鍵翻頁，翻到想學的一頁。
5. 按 **「開始解析本頁」**，右側導師開始講解（串流輸出）。
6. 可在下方輸入框針對這一頁追問。

## 特色

- **自動翻譯**：投影片是英文時，導師先翻成中文再講解，術語標註「譯名（原文）」。
- **記憶 / 脈絡串接**：每解析一頁會存一句摘要；解析下一頁時，前面所有摘要會當成「課程脈絡回顧」一起送給 AI，讓它指出延續、對比、因果關係。頂部「已學習 N 頁」顯示記憶進度。
- **金鑰只留在本機**：存在瀏覽器 localStorage，不經過任何伺服器；API 請求由瀏覽器直接打到供應商。
- **PDF 檢視**：用 PDF.js 渲染真實版面；PPTX 只能抽取文字（建議先在 PowerPoint「另存 / 匯出成 PDF」）。

## 檔案

- `index.html` — 整個 App，單一檔案（CDN 載入 PDF.js + JSZip）

## 技術備註

- Gemini 走 `generativelanguage.googleapis.com/v1beta/...:streamGenerateContent?alt=sse`
- OpenAI 走 `/v1/chat/completions` `stream:true`；`Base URL` 可改成自架相容端點
- 兩者皆支援瀏覽器 CORS，`file://` 開啟即可運作
- 送給模型的投影片圖會壓到寬度 1500px / JPEG 0.85 以控制 token
