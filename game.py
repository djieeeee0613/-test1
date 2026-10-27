import os
import json
import time
import uuid
import random
import base64
import threading
import html as html_mod
from datetime import datetime, timezone, timedelta

import requests
from flask import Blueprint, request, jsonify, render_template, session, abort
from google import genai
from google.genai import types

from merchants_data import CATEGORIES, CATEGORY_ORDER
from game_quiz import QUIZ_QUESTIONS

game_bp = Blueprint('game', __name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://gbffodvtfirdtecsespm.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
N8N_CERT_WEBHOOK_URL = os.getenv("N8N_CERT_WEBHOOK_URL",
                                 "https://nantoutravel.app.n8n.cloud/webhook/nantou-certificate")
SITE_URL = os.getenv("SITE_URL", "https://nantoutravel.com")
STORAGE_BUCKET = "event-image"
STORAGE_PUBLIC_PREFIX = f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/"
TW = timezone(timedelta(hours=8))

_gemini = None
def _gemini_client():
    global _gemini
    if _gemini is None:
        _gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _gemini

# ───────── 等級與 XP（後台可改，存在 game_settings 表）─────────
DEFAULT_SETTINGS = {
    "levels": [{"xp": 0, "title": "旅人", "emoji": "🧳"},
               {"xp": 300, "title": "探索者", "emoji": "🗺️"},
               {"xp": 900, "title": "永續達人", "emoji": "🏆"}],
    "xp_checkin": 240,
    "xp_quiz_correct": 150,
    "xp_quiz_wrong": 30,
    "xp_quiz_combo": 30,       # 連續答對，每多一題 +N
    "xp_quiz_perfect": 300,    # 一局全對
    "xp_post": 60,
    "post_daily_limit": 3,
    "quiz_round_size": 10,
    "quiz_seconds": 20,
    "report_hide_threshold": 3,   # 被檢舉幾次自動隱藏
    "ai_moderation": True,        # 發文、留言先經 AI 審核
    "blocked_words": [],          # 含這些字的內容直接進待審核
}
PRIVATE_SETTINGS = {"levels", "report_hide_threshold", "ai_moderation", "blocked_words"}
SETTINGS_TTL = 60
_settings_cache = {"at": 0, "value": None}

def settings():
    if _settings_cache["value"] and time.time() - _settings_cache["at"] < SETTINGS_TTL:
        return _settings_cache["value"]
    stored = {}
    try:
        rows = sb_get("game_settings", {"key": "eq.game", "select": "value"})
        stored = rows[0]["value"] if rows else {}
    except Exception as e:
        print(f"⚠️ 讀取遊戲設定失敗，改用預設值：{e}")
    _settings_cache.update(at=time.time(), value={**DEFAULT_SETTINGS, **stored})
    return _settings_cache["value"]

def invalidate_settings():
    _settings_cache["at"] = 0

def levels(s=None):
    return [(l["xp"], i + 1, l["title"], l["emoji"]) for i, l in enumerate((s or settings())["levels"])]

def public_cfg(s=None):
    s = s or settings()
    top = levels(s)[-1]
    return {**{k: v for k, v in s.items() if k not in PRIVATE_SETTINGS},
            "max_level": top[1], "max_level_xp": top[0], "max_title": top[2]}

MERCHANT_GROUPS = [
    {"slug": s, "name": CATEGORIES[s]['name'], "emoji": CATEGORIES[s]['emoji'],
     "color": CATEGORIES[s]['color'], "merchants": [m['name'] for m in CATEGORIES[s]['merchants']]}
    for s in CATEGORY_ORDER
]
ALL_MERCHANTS = {m for g in MERCHANT_GROUPS for m in g['merchants']}
MERCHANT_CATEGORY = {m: g['slug'] for g in MERCHANT_GROUPS for m in g['merchants']}

# ───────── 限時活動（game_events 表）─────────
EVENT_SCOPES = {"all": "全部", "checkin": "打卡", "quiz": "問答", "post": "發文"}
_events_cache = {"at": 0, "value": None}

def active_events():
    if _events_cache["value"] is not None and time.time() - _events_cache["at"] < SETTINGS_TTL:
        return _events_cache["value"]
    now = datetime.now(timezone.utc).isoformat()
    try:
        rows = sb_get("game_events", {"enabled": "is.true", "start_at": f"lte.{now}", "end_at": f"gte.{now}", "select": "*"})
    except Exception as e:
        print(f"⚠️ 讀取限時活動失敗：{e}")
        rows = []
    _events_cache.update(at=time.time(), value=rows)
    return rows

def invalidate_events():
    _events_cache["at"] = 0

def xp_multiplier(kind, category=None):
    """取符合條件的進行中活動裡最高的倍率；有指定商家類別的活動只套用在該類別的打卡"""
    best = 1.0
    for e in active_events():
        if e.get("scope") not in ("all", kind):
            continue
        if e.get("category") and (kind != "checkin" or e["category"] != category):
            continue
        best = max(best, float(e.get("multiplier") or 1))
    return best

# ───────── 內容審核 ─────────
MODERATION_PROMPT = """你是南投在地旅遊社群的內容審核員。判斷下面這則使用者發布的內容，是否需要先隱藏、等管理員人工審核。
需要標記的情況：仇恨或歧視、騷擾或人身攻擊、色情或裸露、暴力血腥、違法內容、廣告／詐騙／垃圾訊息、洩露他人個資（電話、住址、身分證字號）。
一般旅遊心得、抱怨或負評、情緒性但沒有攻擊特定人的發言，都不需要標記。
只回傳 JSON：{"flag": true 或 false, "reason": "簡短的中文原因"}"""

def moderate(text, image_b64=None):
    """回傳 (類型, 原因)；沒問題時回傳 (None, None)。AI 出錯時放行，避免影響發文"""
    s = settings()
    low = (text or "").lower()
    for w in s.get("blocked_words") or []:
        if w and w.lower() in low:
            return "keyword", f"包含禁用詞「{w}」"
    if not s.get("ai_moderation", True):
        return None, None
    try:
        parts = [types.Part(text=f"{MODERATION_PROMPT}\n\n內容：\n{(text or '')[:1000]}")]
        if image_b64:
            parts.insert(0, types.Part(inline_data=types.Blob(mime_type='image/jpeg', data=base64.b64decode(image_b64))))
        resp = _gemini_client().models.generate_content(
            model='gemini-2.5-flash', contents=types.Content(role='user', parts=parts),
            config=types.GenerateContentConfig(response_mime_type='application/json', temperature=0))
        d = json.loads(resp.text or "{}")
        if d.get("flag"):
            return "ai", str(d.get("reason") or "AI 判定內容可能不適當")[:100]
    except Exception as e:
        print(f"⚠️ AI 審核失敗，先放行：{e}")
    return None, None

@game_bp.app_context_processor
def _inject_game_cfg():
    return {"game_cfg": public_cfg()}

def level_info(xp, s=None):
    lv = levels(s)
    cur = lv[0]
    for t in lv:
        if xp >= t[0]:
            cur = t
    nxt = next((t for t in lv if xp < t[0]), None)
    return {"xp": xp, "level": cur[1], "title": cur[2], "emoji": cur[3],
            "current_min": cur[0], "next_xp": nxt[0] if nxt else None,
            "next_title": nxt[2] if nxt else None, "max_level": lv[-1][1]}

# ───────── 題庫（後台可改，存在 game_questions 表；表是空的就用內建題目）─────────
_questions_cache = {"at": 0, "value": None}

def question_bank():
    if _questions_cache["value"] is not None and time.time() - _questions_cache["at"] < SETTINGS_TTL:
        return _questions_cache["value"]
    try:
        rows = sb_get("game_questions", {"enabled": "is.true", "select": "id,question,options,answer,fact"})
        if rows:
            bank = [{"id": r["id"], "q": r["question"], "options": r["options"],
                     "answer": r["answer"], "fact": r.get("fact") or ""} for r in rows]
        elif sb_get("game_questions", {"select": "id", "limit": "1"}):
            bank = []
        else:
            bank = [{"id": None, **q} for q in QUIZ_QUESTIONS]
    except Exception as e:
        print(f"⚠️ 讀取題庫失敗，改用內建題目：{e}")
        bank = [{"id": None, **q} for q in QUIZ_QUESTIONS]
    _questions_cache.update(at=time.time(), value=bank)
    return bank

def invalidate_questions():
    _questions_cache["at"] = 0

# ───────── Supabase ─────────
def _h(extra=None):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h

def sb_get(table, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=_h(), timeout=8)
    if r.status_code == 200:
        return r.json()
    print(f"❌ Supabase get {table} {r.status_code}: {r.text[:200]}")
    return []

def sb_insert(table, row):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", json=row,
                      headers=_h({"Prefer": "return=representation"}), timeout=8)
    if r.status_code in (200, 201):
        data = r.json()
        return data[0] if data else row
    print(f"❌ Supabase insert {table} {r.status_code}: {r.text[:200]}")
    return None

def sb_patch(table, match, data):
    requests.patch(f"{SUPABASE_URL}/rest/v1/{table}", params=match, json=data, headers=_h(), timeout=8)

def sb_delete(table, match):
    requests.delete(f"{SUPABASE_URL}/rest/v1/{table}", params=match, headers=_h(), timeout=8)

def upload_image(image_b64, folder, gid):
    path = f"{folder}/{gid}_{int(time.time() * 1000)}.jpg"
    r = requests.post(f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{path}",
                      data=base64.b64decode(image_b64),
                      headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                               "Content-Type": "image/jpeg"}, timeout=15)
    if r.status_code in (200, 201):
        return STORAGE_PUBLIC_PREFIX + path
    print(f"❌ Storage upload {r.status_code}: {r.text[:200]}")
    return None

def get_user(gid):
    rows = sb_get("game_users", {"google_id": f"eq.{gid}", "select": "*"})
    return rows[0] if rows else None

def award_xp(gid, delta, inc=None):
    """加 XP；inc 為其他要累加的計數欄位，例如 {"quiz_correct": 1}"""
    user = get_user(gid)
    if not user:
        return None
    old = level_info(user.get("xp") or 0)
    new_xp = max(0, (user.get("xp") or 0) + delta)
    new = level_info(new_xp)
    patch = {"xp": new_xp, "level": new["level"], "title": new["title"]}
    for col, n in (inc or {}).items():
        patch[col] = max(0, (user.get(col) or 0) + n)

    if new["level"] == new["max_level"] and not user.get("certificate_sent"):
        patch["certificate_sent"] = True
        threading.Thread(target=send_certificate,
                         args=(user.get("email"), user.get("name"), new_xp,
                               patch.get("checkin_count", user.get("checkin_count") or 0)),
                         daemon=True).start()

    sb_patch("game_users", {"google_id": f"eq.{gid}"}, patch)
    new["level_up"] = new["level"] > old["level"]
    return new

# ───────── 永續達人證書 ─────────
def certificate_html(name, xp, checkins, date_str):
    name = html_mod.escape(name or "探索者")
    return f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>南投永續達人證書</title></head>
<body style="margin:0;padding:0;background:#F4F1EA;font-family:'Noto Sans TC','Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F4F1EA;"><tr><td align="center" style="padding:32px 12px;">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border-radius:20px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.08);">
<tr><td style="background:#2d4a35;padding:44px 32px 36px;text-align:center;">
  <img src="{SITE_URL}/static/images/mascot.jpg" width="84" height="84" alt="緣投" style="border-radius:50%;border:3px solid #C8A96E;display:block;margin:0 auto 18px;">
  <div style="color:#C8A96E;font-size:11px;letter-spacing:4px;margin-bottom:10px;">CERTIFICATE OF SUSTAINABLE TRAVEL</div>
  <div style="color:#f0e0b0;font-size:30px;font-weight:900;letter-spacing:4px;">南投永續達人</div>
  <div style="color:rgba(255,255,255,.55);font-size:13px;letter-spacing:8px;margin-top:6px;">認證證書</div>
</td></tr>
<tr><td style="height:4px;background:#C8A96E;"></td></tr>
<tr><td style="padding:40px 40px 12px;text-align:center;">
  <div style="font-size:13px;color:#999;letter-spacing:3px;">茲證明</div>
  <div style="font-size:30px;font-weight:900;color:#1a1a1a;margin:10px 0 4px;">{name}</div>
  <div style="width:120px;height:2px;background:#C8A96E;margin:0 auto 26px;"></div>
  <div style="font-size:15px;color:#444;line-height:2;">
    以雙腳走訪南投的山林與職人工坊，<br>
    用心認識在地的工藝、茶香、部落與土地，<br>
    以實際行動支持在地經濟與文化傳承，<br>
    實踐「慢旅行・深體驗・零負擔」的永續精神。
  </div>
  <div style="margin:26px 0 8px;font-size:15px;color:#444;">特授予</div>
  <div style="display:inline-block;background:#2d4a35;color:#f0e0b0;font-size:22px;font-weight:900;padding:10px 28px;border-radius:40px;letter-spacing:2px;">🏆 南投永續達人 🏆</div>
</td></tr>
<tr><td style="padding:28px 40px;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f7f5f0;border-radius:14px;"><tr>
    <td align="center" style="padding:18px 6px;"><div style="font-size:26px;font-weight:900;color:#C8A96E;">{xp:,}</div><div style="font-size:11px;color:#999;">累積 XP</div></td>
    <td align="center" style="padding:18px 6px;"><div style="font-size:26px;font-weight:900;color:#2d4a35;">{checkins}</div><div style="font-size:11px;color:#999;">走訪職人</div></td>
    <td align="center" style="padding:18px 6px;"><div style="font-size:26px;font-weight:900;color:#2d4a35;">Lv.{levels()[-1][1]}</div><div style="font-size:11px;color:#999;">最高等級</div></td>
  </tr></table>
</td></tr>
<tr><td style="padding:0 40px 8px;">
  <div style="font-size:13px;color:#666;line-height:1.9;background:#fffaf0;border-left:3px solid #C8A96E;padding:12px 16px;border-radius:0 10px 10px 0;">
    <b style="color:#2d4a35;">緣投想對你說：</b><br>
    謝謝你用旅行守護南投 🌱 每一次打卡、每一次預約，都讓職人的手藝多傳承一天。
    下次回來，記得帶朋友一起來，把這份美好分享出去！
  </div>
</td></tr>
<tr><td style="padding:28px 40px 12px;">
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td align="center" width="50%" style="border-right:1px solid #eee;"><div style="font-size:11px;color:#aaa;">發證日期</div><div style="font-size:14px;color:#333;margin-top:4px;">{date_str}</div></td>
    <td align="center" width="50%"><div style="font-size:11px;color:#aaa;">發證單位</div><div style="font-size:14px;color:#333;margin-top:4px;">南投職人旅遊平台</div></td>
  </tr></table>
</td></tr>
<tr><td align="center" style="padding:20px 40px 40px;">
  <a href="{SITE_URL}/game" style="display:inline-block;background:#C8A96E;color:#fff;text-decoration:none;padding:13px 34px;border-radius:30px;font-weight:700;font-size:14px;">回到緣投探索者 →</a>
</td></tr>
<tr><td style="background:#f7f5f0;padding:18px;text-align:center;font-size:11px;color:#aaa;line-height:1.8;">
  本證書由緣投 AI 嚮導自動核發 · nantoutravel.com
</td></tr>
</table></td></tr></table></body></html>"""

def send_email(email, subject, html, name=None):
    """透過 n8n 的 Gmail 工作流寄 HTML 信（n8n 只負責轉寄 email / subject / html）"""
    if not email:
        return False
    try:
        r = requests.post(N8N_CERT_WEBHOOK_URL, json={"email": email, "name": name, "subject": subject, "html": html},
                          timeout=15)
        print(f"✉️ 寄信 {email}：{r.status_code}")
        return r.status_code < 400
    except Exception as e:
        print(f"❌ 寄信失敗：{e}")
        return False

def send_certificate(email, name, xp, checkins):
    date_str = datetime.now(TW).strftime('%Y 年 %m 月 %d 日')
    send_email(email, "🏆 恭喜！您已獲得「南投永續達人」認證證書", certificate_html(name, xp, checkins, date_str), name)

# ───────── 網站設定（公告列、AI 補充指示；存在 game_settings 表 key=site）─────────
DEFAULT_SITE = {"announcement": {"enabled": False, "text": "", "link": "", "style": "info"}, "ai_extra": ""}
_site_cache = {"at": 0, "value": None}

def site_settings():
    if _site_cache["value"] and time.time() - _site_cache["at"] < SETTINGS_TTL:
        return _site_cache["value"]
    stored = {}
    try:
        rows = sb_get("game_settings", {"key": "eq.site", "select": "value"})
        stored = rows[0]["value"] if rows else {}
    except Exception as e:
        print(f"⚠️ 讀取網站設定失敗：{e}")
    value = {**DEFAULT_SITE, **stored}
    value["announcement"] = {**DEFAULT_SITE["announcement"], **(stored.get("announcement") or {})}
    _site_cache.update(at=time.time(), value=value)
    return value

def invalidate_site():
    _site_cache["at"] = 0

# ───────── 驗證 ─────────
def verify_google_token(id_token):
    try:
        r = requests.get("https://oauth2.googleapis.com/tokeninfo",
                         params={"id_token": id_token}, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
        if GOOGLE_CLIENT_ID and d.get("aud") != GOOGLE_CLIENT_ID:
            return None
        return {"google_id": d["sub"], "name": d.get("name"),
                "email": d.get("email"), "avatar_url": d.get("picture"),
                "email_verified": str(d.get("email_verified")).lower() == "true"}
    except Exception as e:
        print(f"❌ Google token error: {e}")
        return None

def photo_has_person(image_b64):
    try:
        resp = _gemini_client().models.generate_content(
            model='gemini-2.5-flash',
            contents=types.Content(role='user', parts=[
                types.Part(inline_data=types.Blob(mime_type='image/jpeg', data=base64.b64decode(image_b64))),
                types.Part(text='Does this photo clearly show at least one real person (face or body)? Answer only YES or NO.'),
            ]))
        return (resp.text or "").strip().upper().startswith("YES")
    except Exception as e:
        print(f"❌ Photo verify error: {e}")
        return False

def current_gid():
    gid = session.get("gid")
    if not gid:
        abort(401)
    return gid

@game_bp.errorhandler(401)
def _unauth(_):
    return jsonify({"error": "請先登入"}), 401

# ───────── 頁面 ─────────
@game_bp.route('/game')
def game_page():
    s = settings()
    return render_template('game.html', google_client_id=GOOGLE_CLIENT_ID,
                           merchant_groups=MERCHANT_GROUPS, levels=levels(s),
                           total_merchants=len(ALL_MERCHANTS), cfg=public_cfg(s))

@game_bp.route('/game/certificate/<gid>')
def certificate_page(gid):
    user = get_user(gid)
    if not user:
        abort(404)
    li = level_info(user.get("xp") or 0)
    if li["level"] < li["max_level"]:
        abort(404)
    return certificate_html(user.get("name"), user.get("xp") or 0, user.get("checkin_count") or 0,
                            datetime.now(TW).strftime('%Y 年 %m 月 %d 日'))

# ───────── 帳號 ─────────
def _user_payload(user):
    return {"user": {k: user.get(k) for k in
                     ("google_id", "name", "avatar_url", "xp", "checkin_count",
                      "quiz_correct", "quiz_played", "post_count", "certificate_sent", "muted")},
            "level_info": level_info(user.get("xp") or 0)}

@game_bp.route('/api/game/login', methods=['POST'])
def login():
    info = verify_google_token((request.json or {}).get('id_token', ''))
    if not info:
        return jsonify({"error": "Google 驗證失敗"}), 401
    user = get_user(info["google_id"])
    if not user:
        profile = {k: info[k] for k in ("google_id", "name", "email", "avatar_url")}
        user = sb_insert("game_users", {**profile, "xp": 0, "level": 1, "title": "旅人",
                                        "checkin_count": 0}) or info
    session["gid"] = info["google_id"]
    session.permanent = True
    return jsonify(_user_payload(user))

@game_bp.route('/api/game/me')
def me():
    gid = session.get("gid")
    user = get_user(gid) if gid else None
    if not user:
        return jsonify({"user": None})
    return jsonify(_user_payload(user))

@game_bp.route('/api/game/logout', methods=['POST'])
def logout():
    session.pop("gid", None)
    return jsonify({"ok": True})

# ───────── 打卡 ─────────
@game_bp.route('/api/game/checkins')
def my_checkins():
    gid = current_gid()
    return jsonify(sb_get("checkins", {"google_id": f"eq.{gid}", "select": "*", "order": "created_at.desc"}))

@game_bp.route('/api/game/checkin', methods=['POST'])
def checkin():
    gid = current_gid()
    data = request.json or {}
    merchant, image = data.get('merchant_name'), data.get('image')
    if merchant not in ALL_MERCHANTS or not image:
        return jsonify({"error": "請選擇商家並上傳照片"}), 400
    if sb_get("checkins", {"google_id": f"eq.{gid}", "merchant_name": f"eq.{merchant}", "select": "id"}):
        return jsonify({"error": "你已經在這家打過卡囉！去探索其他職人吧"}), 400
    if not photo_has_person(image):
        return jsonify({"error": "緣投在照片裡沒看到你耶！請拍一張有你本人入鏡的照片 📸"}), 400

    mult = xp_multiplier("checkin", MERCHANT_CATEGORY.get(merchant))
    xp = round(settings()["xp_checkin"] * mult)
    photo_url = upload_image(image, "checkins", gid)
    if not sb_insert("checkins", {"google_id": gid, "merchant_name": merchant,
                                  "xp_earned": xp, "photo_url": photo_url}):
        return jsonify({"error": "打卡失敗，請稍後再試"}), 500
    stats = award_xp(gid, xp, {"checkin_count": 1})
    return jsonify({"xp_earned": xp, "multiplier": mult, "photo_url": photo_url, "new_stats": stats})

# ───────── 問答大挑戰 ─────────
QUIZ_SESSIONS = {}

@game_bp.route('/api/game/quiz/start', methods=['POST'])
def quiz_start():
    gid = current_gid()
    now = time.time()
    for k in [k for k, v in QUIZ_SESSIONS.items() if v["expires"] < now]:
        QUIZ_SESSIONS.pop(k, None)
    bank = question_bank()
    if not bank:
        return jsonify({"error": "題庫目前沒有啟用的題目，請稍後再來"}), 503
    s = settings()
    qs = random.sample(bank, min(s["quiz_round_size"], len(bank)))
    sid = uuid.uuid4().hex
    QUIZ_SESSIONS[sid] = {"gid": gid, "qs": qs, "idx": 0, "streak": 0, "correct": 0, "xp": 0,
                          "expires": now + 900, "cfg": {k: s[k] for k in s if k.startswith("xp_quiz")}}
    return jsonify({"session_id": sid, "total": len(qs), "seconds": s["quiz_seconds"],
                    "questions": [{"q": q["q"], "options": q["options"]} for q in qs]})

def _log_answer(question_id, gid, chosen, correct):
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/quiz_answers",
                      json={"question_id": question_id, "google_id": gid, "chosen": chosen, "correct": correct},
                      headers=_h(), timeout=5)
    except Exception as e:
        print(f"⚠️ 作答紀錄失敗：{e}")

@game_bp.route('/api/game/quiz/answer', methods=['POST'])
def quiz_answer():
    gid = current_gid()
    data = request.json or {}
    s = QUIZ_SESSIONS.get(data.get("session_id"))
    if not s or s["gid"] != gid or s["idx"] >= len(s["qs"]):
        return jsonify({"error": "這局挑戰已結束，請重新開始"}), 400

    q, cfg = s["qs"][s["idx"]], s["cfg"]
    chosen = data.get("answer")
    correct = chosen == q["answer"]
    if q.get("id"):
        threading.Thread(target=_log_answer, args=(q["id"], gid, chosen if isinstance(chosen, int) else -1, correct),
                         daemon=True).start()
    if correct:
        s["streak"] += 1
        s["correct"] += 1
        xp = cfg["xp_quiz_correct"] + cfg["xp_quiz_combo"] * (s["streak"] - 1)
    else:
        s["streak"] = 0
        xp = cfg["xp_quiz_wrong"]
    s["idx"] += 1
    done = s["idx"] >= len(s["qs"])
    perfect = done and s["correct"] == len(s["qs"])
    if perfect:
        xp += cfg["xp_quiz_perfect"]
    mult = xp_multiplier("quiz")
    xp = round(xp * mult)
    s["xp"] += xp

    inc = {"quiz_correct": 1} if correct else {}
    if done:
        inc["quiz_played"] = 1
        QUIZ_SESSIONS.pop(data.get("session_id"), None)
    stats = award_xp(gid, xp, inc)
    return jsonify({"correct": correct, "correct_answer": q["answer"], "fact": q["fact"],
                    "xp_earned": xp, "streak": s["streak"], "done": done, "perfect": perfect,
                    "round_correct": s["correct"], "round_xp": s["xp"], "multiplier": mult, "new_stats": stats})

# ───────── 旅人動態 ─────────
@game_bp.route('/api/game/posts')
def list_posts():
    params = {"select": "*", "order": "id.desc", "limit": "15", "hidden": "not.is.true", "pinned": "not.is.true"}
    before = request.args.get("before", type=int)
    if before:
        params["id"] = f"lt.{before}"
    posts = sb_get("game_posts", params)
    if not before:
        pinned = sb_get("game_posts", {"select": "*", "order": "id.desc", "hidden": "not.is.true", "pinned": "is.true"})
        posts = pinned + posts
    gid = session.get("gid")
    if gid and posts:
        ids = ",".join(str(p["id"]) for p in posts)
        liked = {l["post_id"] for l in sb_get("game_likes", {"google_id": f"eq.{gid}",
                                                             "post_id": f"in.({ids})", "select": "post_id"})}
        for p in posts:
            p["liked"] = p["id"] in liked
            p["mine"] = p["google_id"] == gid
    for p in posts:
        for k in ("google_id", "moderation", "moderation_reason", "report_count"):
            p.pop(k, None)
    return jsonify(posts)

@game_bp.route('/api/game/posts', methods=['POST'])
def create_post():
    gid = current_gid()
    data = request.json or {}
    content = (data.get("content") or "").strip()
    if not content or len(content) > 500:
        return jsonify({"error": "貼文內容需介於 1–500 字"}), 400
    merchant = data.get("merchant_name") or None
    if merchant and merchant not in ALL_MERCHANTS:
        merchant = None
    user = get_user(gid) or {}
    if user.get("muted"):
        return jsonify({"error": "你的帳號目前被暫停發言，如有疑問請聯絡我們"}), 403

    flag, reason = moderate(content, data.get("image"))
    image_url = None
    if data.get("image"):
        image_url = upload_image(data["image"], "posts", gid)
    elif (data.get("image_url") or "").startswith(STORAGE_PUBLIC_PREFIX):
        image_url = data["image_url"]

    post = sb_insert("game_posts", {
        "google_id": gid, "user_name": user.get("name"), "user_avatar": user.get("avatar_url"),
        "user_level": level_info(user.get("xp") or 0)["level"],
        "content": content, "merchant_name": merchant, "image_url": image_url,
        "likes": 0, "comment_count": 0, "hidden": bool(flag), "moderation": flag, "moderation_reason": reason})
    if not post:
        return jsonify({"error": "發文失敗，請稍後再試"}), 500
    if flag:
        return jsonify({"pending_review": True, "xp_earned": 0})

    today = datetime.now(TW).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    posted_today = sb_get("game_posts", {"google_id": f"eq.{gid}", "created_at": f"gte.{today}", "select": "id"})
    s = settings()
    mult = xp_multiplier("post")
    xp = round(s["xp_post"] * mult) if len(posted_today) <= s["post_daily_limit"] else 0
    stats = award_xp(gid, xp, {"post_count": 1})
    for k in ("google_id", "moderation", "moderation_reason", "report_count"):
        post.pop(k, None)
    post.update({"liked": False, "mine": True})
    return jsonify({"post": post, "xp_earned": xp, "multiplier": mult, "new_stats": stats})

@game_bp.route('/api/game/posts/<int:pid>', methods=['DELETE'])
def delete_post(pid):
    gid = current_gid()
    if not sb_get("game_posts", {"id": f"eq.{pid}", "google_id": f"eq.{gid}", "select": "id"}):
        return jsonify({"error": "找不到這篇貼文"}), 404
    delete_post_cascade(pid)
    return jsonify({"ok": True})

def delete_post_cascade(pid):
    sb_delete("game_comments", {"post_id": f"eq.{pid}"})
    sb_delete("game_likes", {"post_id": f"eq.{pid}"})
    sb_delete("game_posts", {"id": f"eq.{pid}"})

@game_bp.route('/api/game/posts/<int:pid>/like', methods=['POST'])
def toggle_like(pid):
    gid = current_gid()
    post = sb_get("game_posts", {"id": f"eq.{pid}", "select": "likes"})
    if not post:
        return jsonify({"error": "貼文不存在"}), 404
    likes = post[0].get("likes") or 0
    existing = sb_get("game_likes", {"google_id": f"eq.{gid}", "post_id": f"eq.{pid}", "select": "id"})
    if existing:
        sb_delete("game_likes", {"id": f"eq.{existing[0]['id']}"})
        likes = max(0, likes - 1)
        liked = False
    else:
        sb_insert("game_likes", {"google_id": gid, "post_id": pid})
        likes += 1
        liked = True
    sb_patch("game_posts", {"id": f"eq.{pid}"}, {"likes": likes})
    return jsonify({"liked": liked, "likes": likes})

@game_bp.route('/api/game/posts/<int:pid>/comments')
def list_comments(pid):
    rows = sb_get("game_comments", {"post_id": f"eq.{pid}", "select": "id,google_id,user_name,user_avatar,content,created_at",
                                    "hidden": "not.is.true", "order": "id.asc", "limit": "50"})
    gid = session.get("gid")
    for r in rows:
        r["mine"] = r.pop("google_id") == gid
    return jsonify(rows)

@game_bp.route('/api/game/posts/<int:pid>/comments', methods=['POST'])
def add_comment(pid):
    gid = current_gid()
    content = ((request.json or {}).get("content") or "").strip()
    if not content or len(content) > 200:
        return jsonify({"error": "留言需介於 1–200 字"}), 400
    post = sb_get("game_posts", {"id": f"eq.{pid}", "select": "comment_count"})
    if not post:
        return jsonify({"error": "貼文不存在"}), 404
    user = get_user(gid) or {}
    if user.get("muted"):
        return jsonify({"error": "你的帳號目前被暫停發言，如有疑問請聯絡我們"}), 403
    flag, reason = moderate(content)
    row = sb_insert("game_comments", {"post_id": pid, "google_id": gid, "user_name": user.get("name"),
                                      "user_avatar": user.get("avatar_url"), "content": content,
                                      "hidden": bool(flag), "moderation": flag, "moderation_reason": reason})
    if flag:
        return jsonify({"pending_review": True})
    sb_patch("game_posts", {"id": f"eq.{pid}"}, {"comment_count": (post[0].get("comment_count") or 0) + 1})
    if row:
        for k in ("google_id", "moderation", "moderation_reason", "report_count", "hidden"):
            row.pop(k, None)
        row["mine"] = True
    return jsonify(row or {})

# ───────── 檢舉 ─────────
REPORT_REASONS = ["不當言論", "騷擾或攻擊", "廣告或垃圾訊息", "洩露個人隱私", "其他"]

def adjust_comment_count(pid, delta):
    post = sb_get("game_posts", {"id": f"eq.{pid}", "select": "comment_count"})
    if post:
        sb_patch("game_posts", {"id": f"eq.{pid}"}, {"comment_count": max(0, (post[0].get("comment_count") or 0) + delta)})

@game_bp.route('/api/game/report', methods=['POST'])
def report_content():
    gid = current_gid()
    d = request.json or {}
    kind, tid = d.get("type"), d.get("id")
    if kind not in ("post", "comment") or not isinstance(tid, int):
        return jsonify({"error": "檢舉資料不正確"}), 400
    reason = d.get("reason") if d.get("reason") in REPORT_REASONS else "其他"
    table = "game_posts" if kind == "post" else "game_comments"
    cols = "google_id,report_count,moderation,hidden" + (",post_id" if kind == "comment" else "")
    row = sb_get(table, {"id": f"eq.{tid}", "select": cols})
    if not row:
        return jsonify({"error": "找不到這則內容"}), 404
    row = row[0]
    if row.get("google_id") == gid:
        return jsonify({"error": "不能檢舉自己的內容"}), 400
    if sb_get("game_reports", {"target_type": f"eq.{kind}", "target_id": f"eq.{tid}", "google_id": f"eq.{gid}", "select": "id"}):
        return jsonify({"ok": True, "already": True})
    sb_insert("game_reports", {"target_type": kind, "target_id": tid, "google_id": gid, "reason": reason, "status": "open"})
    count = (row.get("report_count") or 0) + 1
    patch = {"report_count": count}
    if count >= settings()["report_hide_threshold"] and row.get("moderation") != "approved" and not row.get("hidden"):
        patch.update(hidden=True, moderation="reports", moderation_reason=f"被檢舉 {count} 次")
        if kind == "comment":
            adjust_comment_count(row["post_id"], -1)
    sb_patch(table, {"id": f"eq.{tid}"}, patch)
    return jsonify({"ok": True})

# ───────── 限時活動（玩家端）─────────
def public_event(e):
    cat = next((g for g in MERCHANT_GROUPS if g["slug"] == e.get("category")), None)
    return {"name": e.get("name"), "description": e.get("description"), "multiplier": float(e.get("multiplier") or 1),
            "scope": e.get("scope"), "scope_name": EVENT_SCOPES.get(e.get("scope"), ""),
            "category_name": f"{cat['emoji']} {cat['name']}" if cat else None,
            "start_at": e.get("start_at"), "end_at": e.get("end_at")}

@game_bp.route('/api/game/events')
def list_events():
    now = datetime.now(timezone.utc)
    soon = (now + timedelta(days=7)).isoformat()
    upcoming = sb_get("game_events", {"enabled": "is.true", "start_at": f"gt.{now.isoformat()}", "select": "*", "order": "start_at.asc"})
    upcoming = [e for e in upcoming if (e.get("start_at") or "") <= soon][:3]
    return jsonify({"active": [public_event(e) for e in active_events()], "upcoming": [public_event(e) for e in upcoming]})

# ───────── 排行榜 ─────────
@game_bp.route('/api/game/leaderboard')
def leaderboard():
    rows = sb_get("game_users", {"select": "google_id,name,avatar_url,xp,checkin_count",
                                 "order": "xp.desc", "limit": "20"})
    gid = session.get("gid")
    for r in rows:
        li = level_info(r.get("xp") or 0)
        r.update({"level": li["level"], "title": li["title"], "emoji": li["emoji"],
                  "me": r.pop("google_id") == gid})
    return jsonify(rows)
