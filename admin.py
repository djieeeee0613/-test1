import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta, timezone
from functools import wraps

import requests
from flask import Blueprint, request, jsonify, render_template, session, redirect

from merchants_data import CATEGORIES, CATEGORY_ORDER
import threading
from game import (SUPABASE_URL, SUPABASE_KEY, GOOGLE_CLIENT_ID, TW, DEFAULT_SETTINGS,
                  _h, sb_get, sb_insert, sb_patch, sb_delete, verify_google_token, level_info, levels,
                  settings as game_settings, invalidate_settings, invalidate_questions, question_bank,
                  delete_post_cascade, send_certificate, adjust_comment_count, award_xp, get_user,
                  invalidate_events, MERCHANT_GROUPS, EVENT_SCOPES, _gemini_client,
                  send_email, upload_image, site_settings, invalidate_site, n8n_headers)
import uuid
from html import escape as html_escape
from google.genai import types as genai_types
import json as json_mod
from game_quiz import QUIZ_QUESTIONS

admin_bp = Blueprint('admin', __name__)

N8N_BASE_URL = os.getenv("N8N_BASE_URL", "https://nantoutravel.app.n8n.cloud").rstrip("/")
N8N_API_KEY = os.getenv("N8N_API_KEY", "")
INDIGENOUS_TOWNSHIPS = {"信義鄉", "仁愛鄉"}
OVERDUE_HOURS = 24
CACHE_TTL = 60

def _emails(var):
    return {e.strip().lower() for e in os.getenv(var, "").split(",") if e.strip()}

def role_for(email):
    email = (email or "").lower()
    if email in _emails("ADMIN_EMAILS"):
        return "admin"
    if email in _emails("MODERATOR_EMAILS"):
        return "moderator"
    return None

def current_admin():
    a = session.get("admin")
    if not a:
        return None
    role = role_for(a.get("email"))
    if not role:
        session.pop("admin", None)
        return None
    a["role"] = role
    return a

def require_role(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            a = current_admin()
            if not a:
                return jsonify({"error": "請先登入"}), 401
            if roles and a["role"] not in roles:
                return jsonify({"error": "權限不足"}), 403
            return f(*args, **kwargs)
        return wrapper
    return deco

def audit(email, action, detail=None):
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/admin_audit_log",
                      json={"email": email, "action": action, "detail": detail or {}},
                      headers=_h(), timeout=5)
    except Exception as e:
        print(f"⚠️ audit log failed: {e}")

# ───────── 資料讀取與快取 ─────────
def fetch_all(table, select="*", params=None, max_rows=20000):
    rows, offset, page = [], 0, 1000
    while offset < max_rows:
        p = {"select": select, "limit": str(page), "offset": str(offset), **(params or {})}
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", params=p, headers=_h(), timeout=15)
        if r.status_code != 200:
            print(f"⚠️ fetch {table} {r.status_code}: {r.text[:160]}")
            break
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows

_cache = {}
def cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL and request.args.get("fresh") != "1":
        return hit[1]
    data = fn()
    _cache[key] = (time.time(), data)
    return data

def load_data():
    return cached("raw", lambda: {
        "bookings": fetch_all("bookings"),
        "merchants": fetch_all("merchants", "artisan,line_id"),
        "users": fetch_all("game_users", "google_id,name,avatar_url,xp,checkin_count,quiz_correct,quiz_played,post_count,certificate_sent,created_at"),
        "checkins": fetch_all("checkins", "merchant_name,created_at"),
        "posts": fetch_all("game_posts", "id,user_name,content,merchant_name,image_url,likes,comment_count,created_at"),
        "comments": fetch_all("game_comments", "created_at"),
        "fetched_at": datetime.now(TW).isoformat(),
    })

# ───────── 商家名稱比對（共用 merchant_directory）─────────
from merchant_directory import MERCHANTS, BY_NAME as _BY_NAME, match_merchant

# ───────── 時間工具 ─────────
def to_tw(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TW)
    except ValueError:
        return None

def today():
    return datetime.now(TW).date()

def parse_booking_date(s, created):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y年%m月%d號"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    base = created.date() if created else today()
    for fmt in ("%m-%d", "%m/%d", "%m月%d日", "%m月%d號"):
        try:
            d = datetime.strptime(s, fmt).date().replace(year=base.year)
            return d if d >= base else d.replace(year=base.year + 1)
        except ValueError:
            pass
    return None

def daily_series(dts, days=30):
    end = today()
    start = end - timedelta(days=days - 1)
    counts = Counter(d.date() for d in dts if d and d.date() >= start)
    labels = [start + timedelta(days=i) for i in range(days)]
    return [d.strftime("%m/%d") for d in labels], [counts.get(d, 0) for d in labels]

def window_counts(dts, days):
    now = datetime.now(TW)
    cur = sum(1 for d in dts if d and now - d <= timedelta(days=days))
    prev = sum(1 for d in dts if d and timedelta(days=days) < now - d <= timedelta(days=days * 2))
    return cur, prev

def bucket(value, edges):
    for label, lo, hi in edges:
        if lo <= value <= hi:
            return label
    return edges[-1][0]

# ───────── 統計 ─────────
def enrich_bookings(bookings):
    out = []
    for b in bookings:
        created = to_tw(b.get("created_at"))
        m = match_merchant(b.get("artisan"))
        out.append({**b, "_created": created, "_m": m,
                    "_date": parse_booking_date(b.get("booking_date"), created),
                    "_pax": int(b.get("people_count") or 0) if str(b.get("people_count") or "0").isdigit() else 0,
                    "_status": b.get("status") or "待確認"})
    return out

def stats_overview(d):
    bks = enrich_bookings(d["bookings"])
    users_dt = [to_tw(u.get("created_at")) for u in d["users"]]
    ck_dt = [to_tw(c.get("created_at")) for c in d["checkins"]]
    bk_dt = [b["_created"] for b in bks]
    now = datetime.now(TW)

    pending = [b for b in bks if b["_status"] == "待確認"]
    overdue = sorted([b for b in pending if b["_created"] and now - b["_created"] > timedelta(hours=OVERDUE_HOURS)],
                     key=lambda b: b["_created"])
    decided = [b for b in bks if b["_status"] in ("已確認", "已婉拒")]
    confirmed = sum(1 for b in decided if b["_status"] == "已確認")
    ratings = [float(b["rating"]) for b in bks if b.get("rating")]

    bound = {m["name"] for m in (match_merchant(r.get("artisan")) for r in d["merchants"] if r.get("line_id")) if m}
    labels, bk_series = daily_series(bk_dt)
    _, ck_series = daily_series(ck_dt)
    _, user_series = daily_series(users_dt)

    def kpi(dts, days=7):
        cur, prev = window_counts(dts, days)
        return {"value": cur, "prev": prev}

    return {
        "kpis": {
            "bookings_7d": {**kpi(bk_dt), "spark": bk_series[-7:]},
            "pending": {"value": len(pending), "overdue": len(overdue)},
            "confirm_rate": {"value": round(confirmed / len(decided) * 100) if decided else None,
                             "decided": len(decided)},
            "rating": {"value": round(sum(ratings) / len(ratings), 2) if ratings else None, "count": len(ratings)},
            "new_players_7d": {**kpi(users_dt), "spark": user_series[-7:]},
            "checkins_today": {"value": ck_series[-1], "spark": ck_series[-7:]},
        },
        "trend": {"labels": labels, "bookings": bk_series, "checkins": ck_series, "players": user_series},
        "todo": {
            "overdue": [{"id": b.get("booking_id"), "artisan": b.get("artisan"), "date": b.get("booking_date"),
                         "pax": b["_pax"], "hours": int((now - b["_created"]).total_seconds() // 3600)}
                        for b in overdue[:10]],
            "unbound": [m["name"] for m in MERCHANTS if m["name"] not in bound],
        },
    }

LEAD_EDGES = [("當天", 0, 0), ("1–3 天", 1, 3), ("4–7 天", 4, 7), ("8–14 天", 8, 14),
              ("15–30 天", 15, 30), ("30 天以上", 31, 10 ** 6)]
PAX_EDGES = [("1 人", 1, 1), ("2 人", 2, 2), ("3–4 人", 3, 4), ("5–6 人", 5, 6), ("7 人以上", 7, 10 ** 6)]

def stats_bookings(d):
    bks = enrich_bookings(d["bookings"])
    status = Counter(b["_status"] for b in bks)
    by_cat = defaultdict(lambda: {"bookings": 0, "people": 0})
    by_merchant = Counter()
    for b in bks:
        key = b["_m"]["cat"] if b["_m"] else "other"
        by_cat[key]["bookings"] += 1
        by_cat[key]["people"] += b["_pax"]
        by_merchant[b["_m"]["name"] if b["_m"] else (b.get("artisan") or "未知")] += 1

    cats = [{"slug": s, "name": CATEGORIES[s]["name"], "emoji": CATEGORIES[s]["emoji"],
             "color": CATEGORIES[s]["color"], **by_cat[s]} for s in CATEGORY_ORDER]
    if by_cat.get("other"):
        cats.append({"slug": "other", "name": "未對應商家", "emoji": "❔", "color": "#9ca3af", **by_cat["other"]})

    t = today()
    cal = Counter(b["_date"] for b in bks if b["_date"] and b["_status"] != "已婉拒")
    start = t - timedelta(days=t.weekday())
    calendar = [{"date": (start + timedelta(days=i)).isoformat(), "count": cal.get(start + timedelta(days=i), 0)}
                for i in range(7 * 12)]

    heat = [[0] * 24 for _ in range(7)]
    for b in bks:
        if b["_created"]:
            heat[b["_created"].weekday()][b["_created"].hour] += 1

    lead = Counter()
    for b in bks:
        if b["_date"] and b["_created"]:
            lead[bucket(max(0, (b["_date"] - b["_created"].date()).days), LEAD_EDGES)] += 1
    pax = Counter(bucket(b["_pax"], PAX_EDGES) for b in bks if b["_pax"])

    return {
        "total": len(bks), "people": sum(b["_pax"] for b in bks),
        "status": {k: status.get(k, 0) for k in ("待確認", "已確認", "已婉拒")},
        "categories": cats,
        "top_merchants": by_merchant.most_common(10),
        "calendar": calendar, "today": t.isoformat(),
        "heatmap": heat,
        "lead_time": [[lbl, lead.get(lbl, 0)] for lbl, _, _ in LEAD_EDGES],
        "party_size": [[lbl, pax.get(lbl, 0)] for lbl, _, _ in PAX_EDGES],
    }

def merchant_table(d):
    bks = enrich_bookings(d["bookings"])
    rows = {m["name"]: {"name": m["name"], "cat": m["cat"], "cat_name": m["cat_name"], "emoji": m["emoji"],
                        "color": m["color"], "township": m["township"], "bookings": 0, "people": 0,
                        "confirmed": 0, "rejected": 0, "pending": 0, "ratings": [], "checkins": 0,
                        "posts": 0, "line_bound": False} for m in MERCHANTS}
    unmatched = Counter()
    for b in bks:
        if not b["_m"]:
            unmatched[b.get("artisan") or "（空白）"] += 1
            continue
        r = rows[b["_m"]["name"]]
        r["bookings"] += 1
        r["people"] += b["_pax"]
        r[{"已確認": "confirmed", "已婉拒": "rejected"}.get(b["_status"], "pending")] += 1
        if b.get("rating"):
            r["ratings"].append(float(b["rating"]))
    for c in d["checkins"]:
        if c.get("merchant_name") in rows:
            rows[c["merchant_name"]]["checkins"] += 1
    for p in d["posts"]:
        if p.get("merchant_name") in rows:
            rows[p["merchant_name"]]["posts"] += 1
    for r in d["merchants"]:
        m = match_merchant(r.get("artisan"))
        if m and r.get("line_id"):
            rows[m["name"]]["line_bound"] = True
    out = []
    for r in rows.values():
        decided = r["confirmed"] + r["rejected"]
        ratings = r.pop("ratings")
        r["confirm_rate"] = round(r["confirmed"] / decided * 100) if decided else None
        r["rating"] = round(sum(ratings) / len(ratings), 1) if ratings else None
        r["visits"] = r["bookings"] + r["checkins"]
        out.append(r)
    return out, unmatched

def stats_merchants(d):
    rows, unmatched = merchant_table(d)
    rows.sort(key=lambda r: (-r["visits"], r["name"]))
    return {"rows": rows, "unmatched": unmatched.most_common(20),
            "bound": sum(r["line_bound"] for r in rows), "total": len(rows)}

def stats_map(d):
    rows, _ = merchant_table(d)
    return {"merchants": [{k: r[k] for k in ("name", "cat", "cat_name", "color", "bookings", "checkins", "visits")}
                          for r in rows]}

def stats_impact(d):
    rows, _ = merchant_table(d)
    bks = enrich_bookings(d["bookings"])
    total_visits = sum(r["visits"] for r in rows)
    supported = sum(1 for r in rows if r["visits"] > 0)
    ranked = sorted((r["visits"] for r in rows), reverse=True)
    top3 = sum(ranked[:3])

    asc = sorted(r["visits"] for r in rows)
    lorenz, acc = [[0, 0]], 0
    for i, v in enumerate(asc, 1):
        acc += v
        lorenz.append([round(i / len(asc) * 100, 1), round(acc / total_visits * 100, 1) if total_visits else 0])
    gini = None
    if total_visits:
        n = len(asc)
        gini = round(sum((2 * i - n - 1) * v for i, v in enumerate(asc, 1)) / (n * total_visits), 2)

    town = Counter()
    for r in rows:
        town[r["township"]] += r["visits"]
    indigenous = sum(v for k, v in town.items() if k in INDIGENOUS_TOWNSHIPS)
    cat = Counter()
    for r in rows:
        cat[r["cat"]] += r["visits"]

    month = Counter()
    for b in bks:
        if b["_created"]:
            month[b["_created"].strftime("%Y-%m")] += 1
    for c in d["checkins"]:
        dt = to_tw(c.get("created_at"))
        if dt:
            month[dt.strftime("%Y-%m")] += 1
    months = sorted(month)[-12:]

    return {
        "people_served": sum(b["_pax"] for b in bks if b["_status"] != "已婉拒"),
        "people_confirmed": sum(b["_pax"] for b in bks if b["_status"] == "已確認"),
        "supported": supported, "total_merchants": len(rows),
        "total_visits": total_visits,
        "top3_share": round(top3 / total_visits * 100) if total_visits else None,
        "gini": gini, "lorenz": lorenz,
        "townships": town.most_common(),
        "indigenous_share": round(indigenous / total_visits * 100) if total_visits else None,
        "indigenous_townships": sorted(INDIGENOUS_TOWNSHIPS),
        "categories": [{"name": CATEGORIES[s]["name"], "emoji": CATEGORIES[s]["emoji"],
                        "color": CATEGORIES[s]["color"], "visits": cat.get(s, 0)} for s in CATEGORY_ORDER],
        "monthly": {"labels": months, "visits": [month[m] for m in months]},
        "cold": [r["name"] for r in sorted(rows, key=lambda r: r["visits"]) if r["visits"] == 0],
    }

def stats_game(d):
    users = d["users"]
    by_level = Counter(level_info(u.get("xp") or 0)["level"] for u in users)
    level_rows = [{"level": lv, "title": title, "emoji": emoji, "min_xp": xp, "count": by_level.get(lv, 0),
                   "reached": sum(c for l, c in by_level.items() if l >= lv)} for xp, lv, title, emoji in levels()]
    labels, reg = daily_series([to_tw(u.get("created_at")) for u in users])
    _, ck = daily_series([to_tw(c.get("created_at")) for c in d["checkins"]])
    _, posts = daily_series([to_tw(p.get("created_at")) for p in d["posts"]])
    _, cmts = daily_series([to_tw(c.get("created_at")) for c in d["comments"]])

    rows, _ = merchant_table(d)
    top_ck = sorted(rows, key=lambda r: -r["checkins"])[:10]
    top_users = sorted(users, key=lambda u: -(u.get("xp") or 0))[:10]
    played = sum(u.get("quiz_played") or 0 for u in users)
    correct = sum(u.get("quiz_correct") or 0 for u in users)
    return {
        "totals": {"players": len(users), "certificates": sum(1 for u in users if u.get("certificate_sent")),
                   "checkins": len(d["checkins"]), "posts": len(d["posts"]), "comments": len(d["comments"]),
                   "likes": sum(p.get("likes") or 0 for p in d["posts"]),
                   "quiz_rounds": played, "quiz_correct": correct},
        "levels": level_rows,
        "trend": {"labels": labels, "players": reg, "checkins": ck, "posts": posts, "comments": cmts},
        "checkin_vs_booking": [{"name": r["name"], "checkins": r["checkins"], "bookings": r["bookings"]} for r in top_ck],
        "top_players": [{"name": u.get("name"), "avatar": u.get("avatar_url"), "xp": u.get("xp") or 0,
                         "level": level_info(u.get("xp") or 0)["level"], "checkins": u.get("checkin_count") or 0,
                         "posts": u.get("post_count") or 0} for u in top_users],
        "recent_posts": [{k: p.get(k) for k in ("user_name", "content", "merchant_name", "image_url", "likes",
                                                "comment_count", "created_at")}
                         for p in sorted(d["posts"], key=lambda p: p.get("id") or 0, reverse=True)[:8]],
    }

# ───────── 系統健康 ─────────
def n8n_get(path, params=None):
    r = requests.get(f"{N8N_BASE_URL}/api/v1/{path}", params=params,
                     headers={"X-N8N-API-KEY": N8N_API_KEY, "Accept": "application/json"}, timeout=12)
    r.raise_for_status()
    return r.json()

def stats_n8n():
    if not N8N_API_KEY:
        return {"configured": False}
    try:
        workflows = n8n_get("workflows", {"limit": 100}).get("data", [])
        names = {w["id"]: w.get("name") for w in workflows}
        since = datetime.now(TW) - timedelta(hours=24)
        execs, cursor = [], None
        for _ in range(5):
            params = {"limit": 100, "includeData": "false"}
            if cursor:
                params["cursor"] = cursor
            page = n8n_get("executions", params)
            batch = page.get("data", [])
            execs.extend(batch)
            cursor = page.get("nextCursor")
            last = to_tw(batch[-1].get("startedAt")) if batch else None
            if not cursor or (last and last < since):
                break
        recent = [e for e in execs if (to_tw(e.get("startedAt")) or since) >= since]
        per = defaultdict(lambda: {"success": 0, "error": 0, "other": 0, "durations": []})
        for e in recent:
            s = per[e.get("workflowId")]
            st = e.get("status")
            s["success" if st == "success" else "error" if st in ("error", "crashed") else "other"] += 1
            a, b = to_tw(e.get("startedAt")), to_tw(e.get("stoppedAt"))
            if a and b:
                s["durations"].append((b - a).total_seconds())
        wf = []
        for w in workflows:
            s = per.get(w["id"], {"success": 0, "error": 0, "other": 0, "durations": []})
            dur = s["durations"]
            wf.append({"id": w["id"], "name": w.get("name"), "active": w.get("active"),
                       "updated": w.get("updatedAt"), "success": s["success"], "error": s["error"],
                       "other": s["other"], "avg_seconds": round(sum(dur) / len(dur), 2) if dur else None})
        failures = []
        for e in [e for e in execs if e.get("status") in ("error", "crashed")][:5]:
            detail = {}
            try:
                full = n8n_get(f"executions/{e['id']}", {"includeData": "true"})
                rd = (full.get("data") or {}).get("resultData") or {}
                detail = {"node": rd.get("lastNodeExecuted"),
                          "message": ((rd.get("error") or {}).get("message") or "")[:200]}
            except Exception:
                pass
            failures.append({"id": e["id"], "workflow": names.get(e.get("workflowId"), e.get("workflowId")),
                             "started": e.get("startedAt"), **detail})
        total = sum(w["success"] + w["error"] for w in wf)
        ok = sum(w["success"] for w in wf)
        return {"configured": True, "workflows": wf, "failures": failures,
                "success_rate_24h": round(ok / total * 100) if total else None, "runs_24h": total}
    except Exception as e:
        return {"configured": True, "error": str(e)[:200]}

def stats_system():
    t0 = time.time()
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/bookings", params={"select": "id", "limit": "1"},
                         headers=_h(), timeout=8)
        supa = {"ok": r.status_code == 200, "status": r.status_code, "ms": round((time.time() - t0) * 1000)}
    except Exception as e:
        supa = {"ok": False, "error": str(e)[:120], "ms": None}
    env = {k: bool(os.getenv(k)) for k in ("GEMINI_API_KEY", "SUPABASE_KEY", "GOOGLE_CLIENT_ID",
                                           "FLASK_SECRET_KEY", "N8N_API_KEY", "ADMIN_EMAILS")}
    return {"supabase": supa, "n8n": stats_n8n(), "env": env}

# ───────── 路由 ─────────
@admin_bp.route('/admin')
def admin_page():
    return render_template('admin.html', google_client_id=GOOGLE_CLIENT_ID, admin=current_admin())

@admin_bp.route('/admin/api/login', methods=['POST'])
def admin_login():
    info = verify_google_token((request.json or {}).get('id_token', ''))
    if not info or not info.get("email_verified"):
        return jsonify({"error": "Google 驗證失敗"}), 401
    role = role_for(info["email"])
    if not role:
        audit(info["email"], "login_denied")
        return jsonify({"error": f"{info['email']} 沒有後台權限"}), 403
    session["admin"] = {"email": info["email"], "name": info.get("name"), "avatar": info.get("avatar_url"), "role": role}
    session.permanent = True
    audit(info["email"], "login", {"role": role})
    return jsonify(session["admin"])

@admin_bp.route('/admin/api/logout', methods=['POST'])
def admin_logout():
    a = session.pop("admin", None)
    if a:
        audit(a.get("email"), "logout")
    return jsonify({"ok": True})

def _endpoint(fn):
    return lambda: jsonify({**fn(load_data()), "fetched_at": load_data()["fetched_at"]})

admin_bp.add_url_rule('/admin/api/overview', 'overview', require_role("admin")(_endpoint(stats_overview)))
admin_bp.add_url_rule('/admin/api/bookings', 'bookings', require_role("admin")(_endpoint(stats_bookings)))
admin_bp.add_url_rule('/admin/api/merchants', 'merchants', require_role("admin")(_endpoint(stats_merchants)))
admin_bp.add_url_rule('/admin/api/map', 'map', require_role("admin")(_endpoint(stats_map)))
admin_bp.add_url_rule('/admin/api/impact', 'impact', require_role("admin")(_endpoint(stats_impact)))
admin_bp.add_url_rule('/admin/api/game', 'game', require_role("admin", "moderator")(_endpoint(stats_game)))

@admin_bp.route('/admin/api/bookings/list')
@require_role("admin")
def admin_booking_list():
    bks = sorted(enrich_bookings(load_data()["bookings"]),
                 key=lambda b: b["_created"] or datetime.min.replace(tzinfo=TW), reverse=True)
    return jsonify({"rows": [{
        "row_id": b.get("id"),
        "id": b.get("booking_id"), "created": b["_created"].strftime("%Y-%m-%d %H:%M") if b["_created"] else "",
        "artisan": b.get("artisan"), "merchant": b["_m"]["name"] if b["_m"] else None,
        "date": b.get("booking_date"), "date_iso": b["_date"].isoformat() if b["_date"] else None,
        "pax": b["_pax"], "customer": b.get("customer_name"), "phone": b.get("phone"), "email": b.get("email"),
        "status": b["_status"], "rating": b.get("rating"), "comment": b.get("rating_comment"),
    } for b in bks]})

@admin_bp.route('/admin/api/system')
@require_role("admin")
def admin_system():
    return jsonify(cached("system", stats_system))

def who():
    return (current_admin() or {}).get("email")

def body():
    return request.get_json(silent=True) or {}

# ───────── 遊戲設定 ─────────
INT_FIELDS = {
    "xp_checkin": ("打卡 XP", 0, 100000), "xp_quiz_correct": ("答對 XP", 0, 100000),
    "xp_quiz_wrong": ("答錯 XP", 0, 100000), "xp_quiz_combo": ("連擊加成 XP", 0, 100000),
    "xp_quiz_perfect": ("全對加碼 XP", 0, 100000), "xp_post": ("發文 XP", 0, 100000),
    "post_daily_limit": ("每日發文給 XP 篇數", 0, 50), "quiz_round_size": ("每局題數", 1, 30),
    "quiz_seconds": ("每題秒數", 5, 120),
    "report_hide_threshold": ("被檢舉幾次自動隱藏", 1, 50),
}

def validate_settings(raw):
    errors, clean = [], {}
    lv = raw.get("levels")
    if not isinstance(lv, list) or not 2 <= len(lv) <= 10:
        errors.append("等級數量需介於 2–10 級")
    else:
        out, prev = [], -1
        for i, l in enumerate(lv):
            title = str(l.get("title") or "").strip()[:12]
            emoji = str(l.get("emoji") or "").strip()[:4] or "⭐"
            try:
                xp = int(l.get("xp"))
            except (TypeError, ValueError):
                errors.append(f"Lv.{i + 1} 的 XP 門檻不是數字")
                continue
            if not title:
                errors.append(f"Lv.{i + 1} 需要名稱")
            if i == 0 and xp != 0:
                errors.append("Lv.1 的 XP 門檻必須是 0")
            if i > 0 and xp <= prev:
                errors.append(f"Lv.{i + 1} 的 XP 門檻必須大於 Lv.{i}（{prev}）")
            prev = xp
            out.append({"xp": xp, "title": title, "emoji": emoji})
        clean["levels"] = out
    for k, (label, lo, hi) in INT_FIELDS.items():
        try:
            v = int(raw.get(k))
        except (TypeError, ValueError):
            errors.append(f"{label} 不是數字")
            continue
        if not lo <= v <= hi:
            errors.append(f"{label} 需介於 {lo}–{hi}")
        clean[k] = v
    clean["ai_moderation"] = bool(raw.get("ai_moderation", True))
    words = raw.get("blocked_words") or []
    if isinstance(words, str):
        words = re.split(r"[,，\n]", words)
    words = list(dict.fromkeys(str(w).strip() for w in words if str(w).strip()))
    if len(words) > 200 or any(len(w) > 20 for w in words):
        errors.append("禁用詞最多 200 個，每個最多 20 字")
    clean["blocked_words"] = words[:200]
    enabled = len(question_bank())
    if clean.get("quiz_round_size", 0) > enabled:
        errors.append(f"每局題數（{clean['quiz_round_size']}）不能多於目前啟用的題目數（{enabled}）")
    return clean, errors

def simulate(s):
    n, top = s["quiz_round_size"], s["levels"][-1]["xp"]
    perfect = n * s["xp_quiz_correct"] + s["xp_quiz_combo"] * n * (n - 1) // 2 + s["xp_quiz_perfect"]
    worst = n * s["xp_quiz_wrong"]
    total_merchants = len(MERCHANTS)
    need = lambda xp, per: -(-xp // per) if per else None
    per_level = [{"title": l["title"], "emoji": l["emoji"], "xp": l["xp"],
                  "checkins": need(l["xp"], s["xp_checkin"]), "perfect_rounds": need(l["xp"], perfect),
                  "worst_rounds": need(l["xp"], worst)} for l in s["levels"][1:]]
    top_row = per_level[-1]
    warnings = []
    if top_row["perfect_rounds"] is not None and top_row["perfect_rounds"] <= 1:
        warnings.append(f"一局問答全對（+{perfect:,} XP）就能直接升到最高級拿證書")
    if top_row["worst_rounds"] is not None and top_row["worst_rounds"] <= 5:
        warnings.append(f"就算每題都答錯，玩 {top_row['worst_rounds']} 局也能拿證書")
    if top_row["checkins"] is not None and top_row["checkins"] > total_merchants:
        warnings.append(f"只靠打卡，走完全部 {total_merchants} 家也到不了最高級")
    if top_row["checkins"] is not None and top_row["checkins"] <= 2:
        warnings.append(f"只要打卡 {top_row['checkins']} 家就能拿證書")
    return {"perfect_round": perfect, "worst_round": worst, "per_level": per_level,
            "warnings": warnings, "total_merchants": total_merchants}

def distribution(old, new):
    users = fetch_all("game_users", "xp,certificate_sent")
    old_c, new_c = Counter(), Counter()
    for u in users:
        old_c[level_info(u.get("xp") or 0, old)["level"]] += 1
        new_c[level_info(u.get("xp") or 0, new)["level"]] += 1
    top = new["levels"][-1]["xp"]
    rows = [{"level": i + 1, "title": l["title"], "old": old_c.get(i + 1, 0), "new": new_c.get(i + 1, 0)}
            for i, l in enumerate(new["levels"])]
    pending = sum(1 for u in users if (u.get("xp") or 0) >= top and not u.get("certificate_sent"))
    return {"rows": rows, "players": len(users), "pending_certificates": pending}

@admin_bp.route('/admin/api/game/settings')
@require_role("admin")
def get_game_settings():
    hist = sb_get("game_settings_history", {"select": "id,value,updated_by,created_at", "order": "id.desc", "limit": "15"})
    return jsonify({"settings": game_settings(), "defaults": DEFAULT_SETTINGS, "history": hist,
                    "enabled_questions": len(question_bank()), "fields": [[k, v[0]] for k, v in INT_FIELDS.items()]})

@admin_bp.route('/admin/api/game/settings/preview', methods=['POST'])
@require_role("admin")
def preview_game_settings():
    clean, errors = validate_settings(body().get("settings") or {})
    if errors:
        return jsonify({"errors": errors})
    return jsonify({"errors": [], "sim": simulate(clean), "dist": distribution(game_settings(), clean)})

@admin_bp.route('/admin/api/game/settings', methods=['POST'])
@require_role("admin")
def save_game_settings():
    data = body()
    clean, errors = validate_settings(data.get("settings") or {})
    if errors:
        return jsonify({"errors": errors}), 400
    before = game_settings()
    r = requests.post(f"{SUPABASE_URL}/rest/v1/game_settings",
                      json={"key": "game", "value": clean, "updated_by": who(), "updated_at": datetime.now(TW).isoformat()},
                      headers=_h({"Prefer": "resolution=merge-duplicates,return=minimal"}), timeout=8)
    if r.status_code not in (200, 201, 204):
        return jsonify({"errors": [f"儲存失敗（{r.status_code}）：{r.text[:160]}"]}), 500
    sb_insert("game_settings_history", {"value": clean, "updated_by": who()})
    invalidate_settings()
    changed = {k: [before.get(k), clean[k]] for k in clean if before.get(k) != clean[k]}
    audit(who(), "game_settings_update", {"changed": changed})
    sent = 0
    if data.get("send_certificates"):
        top = clean["levels"][-1]["xp"]
        targets = [u for u in fetch_all("game_users", "google_id,email,name,xp,checkin_count,certificate_sent")
                   if (u.get("xp") or 0) >= top and not u.get("certificate_sent")]
        for u in targets:
            sb_patch("game_users", {"google_id": f"eq.{u['google_id']}"}, {"certificate_sent": True})
        threading.Thread(target=lambda: [send_certificate(u.get("email"), u.get("name"), u.get("xp") or 0,
                                                          u.get("checkin_count") or 0) for u in targets],
                         daemon=True).start()
        sent = len(targets)
        audit(who(), "certificates_backfill", {"count": sent})
    _cache.clear()
    return jsonify({"ok": True, "settings": clean, "certificates_sent": sent})

# ───────── 題目管理 ─────────
QUESTION_CATEGORIES = ["地理", "歷史文化", "物產職人", "永續旅行", "商家知識", "一般"]
MIN_ANSWERS_FOR_FLAG = 10

def validate_question(d):
    errors = []
    q = str(d.get("question") or "").strip()
    opts = [str(o or "").strip() for o in (d.get("options") or [])]
    fact = str(d.get("fact") or "").strip()
    if not 5 <= len(q) <= 200:
        errors.append("題目需介於 5–200 字")
    if len(opts) != 4 or any(not o for o in opts):
        errors.append("需要 4 個選項，且都不能空白")
    elif len(set(opts)) != 4:
        errors.append("4 個選項不能重複")
    elif any(len(o) > 60 for o in opts):
        errors.append("每個選項最多 60 字")
    try:
        answer = int(d.get("answer"))
    except (TypeError, ValueError):
        answer = -1
    if answer not in range(4):
        errors.append("請選擇正確答案")
    if len(fact) > 300:
        errors.append("補充說明最多 300 字")
    cat = d.get("category") if d.get("category") in QUESTION_CATEGORIES else "一般"
    return {"question": q, "options": opts, "answer": answer, "fact": fact, "category": cat,
            "enabled": bool(d.get("enabled", True))}, errors

def question_stats():
    stats = defaultdict(lambda: {"n": 0, "correct": 0, "choices": [0, 0, 0, 0]})
    for a in fetch_all("quiz_answers", "question_id,chosen,correct"):
        s = stats[a.get("question_id")]
        s["n"] += 1
        s["correct"] += 1 if a.get("correct") else 0
        if a.get("chosen") in (0, 1, 2, 3):
            s["choices"][a["chosen"]] += 1
    return stats

def question_flags(row, s):
    if s["n"] < MIN_ANSWERS_FOR_FLAG:
        return []
    flags, rate = [], s["correct"] / s["n"]
    if rate > .95:
        flags.append("too_easy")
    if rate < .2:
        flags.append("too_hard")
    top = max(range(4), key=lambda i: s["choices"][i])
    if top != row["answer"] and s["choices"][top] / s["n"] >= .5:
        flags.append("suspect_answer")
    return flags

@admin_bp.route('/admin/api/questions')
@require_role("admin", "moderator")
def list_questions():
    rows = fetch_all("game_questions", "*", {"order": "id.asc"})
    source = "db" if rows else "builtin"
    if not rows:
        rows = [{"id": None, "question": q["q"], "options": q["options"], "answer": q["answer"], "fact": q["fact"],
                 "category": q.get("cat", "一般"), "enabled": True} for q in QUIZ_QUESTIONS]
    stats = question_stats() if source == "db" else {}
    for r in rows:
        s = stats.get(r["id"], {"n": 0, "correct": 0, "choices": [0, 0, 0, 0]})
        r["stats"] = {**s, "rate": round(s["correct"] / s["n"] * 100) if s["n"] else None}
        r["flags"] = question_flags(r, s)
    return jsonify({"source": source, "rows": rows, "categories": QUESTION_CATEGORIES, "merchant_groups": MERCHANT_GROUPS,
                    "round_size": game_settings()["quiz_round_size"], "min_answers": MIN_ANSWERS_FOR_FLAG})

@admin_bp.route('/admin/api/questions/import-builtin', methods=['POST'])
@require_role("admin", "moderator")
def import_builtin_questions():
    if sb_get("game_questions", {"select": "id", "limit": "1"}):
        return jsonify({"error": "資料庫已經有題目，不需要再匯入"}), 400
    rows = [{"question": q["q"], "options": q["options"], "answer": q["answer"], "fact": q["fact"],
             "category": q.get("cat", "一般"), "enabled": True, "created_by": who()} for q in QUIZ_QUESTIONS]
    r = requests.post(f"{SUPABASE_URL}/rest/v1/game_questions", json=rows, headers=_h(), timeout=15)
    if r.status_code not in (200, 201):
        return jsonify({"error": f"匯入失敗（{r.status_code}）：{r.text[:160]}"}), 500
    invalidate_questions()
    audit(who(), "questions_import_builtin", {"count": len(rows)})
    return jsonify({"ok": True, "count": len(rows)})

@admin_bp.route('/admin/api/questions', methods=['POST'])
@require_role("admin", "moderator")
def create_question():
    clean, errors = validate_question(body())
    if errors:
        return jsonify({"errors": errors}), 400
    row = sb_insert("game_questions", {**clean, "created_by": who()})
    if not row:
        return jsonify({"errors": ["儲存失敗，請確認已建立 game_questions 資料表"]}), 500
    invalidate_questions()
    audit(who(), "question_create", {"id": row.get("id"), "question": clean["question"][:60]})
    return jsonify(row)

@admin_bp.route('/admin/api/questions/bulk', methods=['POST'])
@require_role("admin", "moderator")
def bulk_questions():
    good, bad = [], []
    for i, d in enumerate((body().get("rows") or [])[:300], 1):
        clean, errors = validate_question(d)
        (bad.append({"row": i, "errors": errors}) if errors else good.append({**clean, "created_by": who()}))
    if good:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/game_questions", json=good, headers=_h(), timeout=15)
        if r.status_code not in (200, 201):
            return jsonify({"error": f"匯入失敗（{r.status_code}）：{r.text[:160]}"}), 500
        invalidate_questions()
        audit(who(), "questions_import_csv", {"count": len(good)})
    return jsonify({"inserted": len(good), "errors": bad})

@admin_bp.route('/admin/api/questions/<int:qid>', methods=['PUT'])
@require_role("admin", "moderator")
def update_question(qid):
    clean, errors = validate_question(body())
    if errors:
        return jsonify({"errors": errors}), 400
    sb_patch("game_questions", {"id": f"eq.{qid}"}, {**clean, "updated_at": datetime.now(TW).isoformat()})
    invalidate_questions()
    audit(who(), "question_update", {"id": qid, "question": clean["question"][:60]})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/questions/<int:qid>/toggle', methods=['POST'])
@require_role("admin", "moderator")
def toggle_question(qid):
    enabled = bool(body().get("enabled"))
    if not enabled and len(question_bank()) - 1 < game_settings()["quiz_round_size"]:
        return jsonify({"error": f"停用後啟用題目會少於每局題數（{game_settings()['quiz_round_size']}），請先到遊戲設定調低每局題數"}), 400
    sb_patch("game_questions", {"id": f"eq.{qid}"}, {"enabled": enabled})
    invalidate_questions()
    audit(who(), "question_enable" if enabled else "question_disable", {"id": qid})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/questions/<int:qid>', methods=['DELETE'])
@require_role("admin", "moderator")
def delete_question(qid):
    row = sb_get("game_questions", {"id": f"eq.{qid}", "select": "question,enabled"})
    if not row:
        return jsonify({"error": "找不到這題"}), 404
    if row[0].get("enabled") and len(question_bank()) - 1 < game_settings()["quiz_round_size"]:
        return jsonify({"error": "刪除後啟用題目會少於每局題數，請先調低每局題數"}), 400
    sb_delete("quiz_answers", {"question_id": f"eq.{qid}"})
    sb_delete("game_questions", {"id": f"eq.{qid}"})
    invalidate_questions()
    audit(who(), "question_delete", {"id": qid, "question": (row[0].get("question") or "")[:60]})
    return jsonify({"ok": True})

# ───────── 動態管理（含待審核與檢舉）─────────
PENDING_MODERATION = ("ai", "keyword", "reports")

def _public_post(p):
    return {k: p.get(k) for k in ("id", "user_name", "user_avatar", "user_level", "content", "merchant_name",
                                  "image_url", "likes", "comment_count", "hidden", "pinned", "created_at",
                                  "report_count", "moderation", "moderation_reason")}

def _resolve_reports(kind, tid, status):
    sb_patch("game_reports", {"target_type": f"eq.{kind}", "target_id": f"eq.{tid}", "status": "eq.open"}, {"status": status})

def _open_report_reasons():
    reasons = defaultdict(Counter)
    for r in fetch_all("game_reports", "target_type,target_id,reason", {"status": "eq.open"}):
        reasons[(r["target_type"], r["target_id"])][r.get("reason") or "其他"] += 1
    return reasons

@admin_bp.route('/admin/api/feed')
@require_role("admin", "moderator")
def admin_feed():
    status, q = request.args.get("status", ""), (request.args.get("q") or "").strip().lower()
    posts = fetch_all("game_posts", "*", {"order": "id.desc"}, max_rows=3000)
    counts = {"all": len(posts), "hidden": sum(1 for p in posts if p.get("hidden")),
              "pinned": sum(1 for p in posts if p.get("pinned")),
              "reported": sum(1 for p in posts if (p.get("report_count") or 0) > 0)}
    if status == "hidden":
        posts = [p for p in posts if p.get("hidden")]
    elif status == "pinned":
        posts = [p for p in posts if p.get("pinned")]
    elif status == "image":
        posts = [p for p in posts if p.get("image_url")]
    elif status == "reported":
        posts = [p for p in posts if (p.get("report_count") or 0) > 0]
    if q:
        posts = [p for p in posts if any(q in str(p.get(k) or "").lower() for k in ("content", "user_name", "merchant_name"))]
    return jsonify({"rows": [_public_post(p) for p in posts[:150]], "total": len(posts), "counts": counts})

@admin_bp.route('/admin/api/review')
@require_role("admin", "moderator")
def review_queue():
    flt = {"moderation": f"in.({','.join(PENDING_MODERATION)})", "order": "id.asc"}
    posts = fetch_all("game_posts", "*", flt)
    comments = fetch_all("game_comments", "id,post_id,user_name,user_avatar,content,created_at,report_count,moderation,moderation_reason", flt)
    reasons = _open_report_reasons()
    parents = {}
    if comments:
        ids = ",".join(sorted({str(c["post_id"]) for c in comments}))
        parents = {p["id"]: p.get("content") for p in sb_get("game_posts", {"id": f"in.({ids})", "select": "id,content"})}
    return jsonify({
        "posts": [{**_public_post(p), "reasons": reasons.get(("post", p["id"]), Counter()).most_common()} for p in posts],
        "comments": [{**{k: c.get(k) for k in ("id", "post_id", "user_name", "user_avatar", "content", "created_at",
                                                "report_count", "moderation", "moderation_reason")},
                      "post_content": (parents.get(c["post_id"]) or "")[:80],
                      "reasons": reasons.get(("comment", c["id"]), Counter()).most_common()} for c in comments],
        "count": len(posts) + len(comments)})

POST_ACTIONS = {"hide": {"hidden": True}, "unhide": {"hidden": False}, "pin": {"pinned": True}, "unpin": {"pinned": False},
                "approve": {"hidden": False, "moderation": "approved"}, "reject": {"hidden": True, "moderation": "removed"}}

@admin_bp.route('/admin/api/feed/<int:pid>', methods=['POST'])
@require_role("admin", "moderator")
def moderate_post(pid):
    action = body().get("action")
    patch = POST_ACTIONS.get(action)
    if not patch:
        return jsonify({"error": "未知的操作"}), 400
    sb_patch("game_posts", {"id": f"eq.{pid}"}, patch)
    if action in ("approve", "reject"):
        _resolve_reports("post", pid, "dismissed" if action == "approve" else "actioned")
    audit(who(), f"post_{action}", {"id": pid})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/feed/<int:pid>', methods=['DELETE'])
@require_role("admin", "moderator")
def admin_delete_post(pid):
    row = sb_get("game_posts", {"id": f"eq.{pid}", "select": "user_name,content"})
    if not row:
        return jsonify({"error": "找不到這篇貼文"}), 404
    delete_post_cascade(pid)
    _resolve_reports("post", pid, "actioned")
    audit(who(), "post_delete", {"id": pid, "user": row[0].get("user_name"), "content": (row[0].get("content") or "")[:80]})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/feed/<int:pid>/comments')
@require_role("admin", "moderator")
def admin_comments(pid):
    return jsonify(sb_get("game_comments", {"post_id": f"eq.{pid}", "order": "id.asc",
                                            "select": "id,user_name,user_avatar,content,hidden,created_at,report_count,moderation"}))

@admin_bp.route('/admin/api/comments/<int:cid>', methods=['POST'])
@require_role("admin", "moderator")
def moderate_comment(cid):
    action = body().get("action")
    if action not in ("hide", "unhide", "approve", "reject"):
        return jsonify({"error": "未知的操作"}), 400
    row = sb_get("game_comments", {"id": f"eq.{cid}", "select": "post_id,hidden"})
    if not row:
        return jsonify({"error": "找不到這則留言"}), 404
    hide = action in ("hide", "reject")
    patch = {"hidden": hide}
    if action in ("approve", "reject"):
        patch["moderation"] = "approved" if action == "approve" else "removed"
        _resolve_reports("comment", cid, "dismissed" if action == "approve" else "actioned")
    sb_patch("game_comments", {"id": f"eq.{cid}"}, patch)
    if bool(row[0].get("hidden")) != hide:
        adjust_comment_count(row[0]["post_id"], -1 if hide else 1)
    audit(who(), f"comment_{action}", {"id": cid})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/comments/<int:cid>', methods=['DELETE'])
@require_role("admin", "moderator")
def admin_delete_comment(cid):
    row = sb_get("game_comments", {"id": f"eq.{cid}", "select": "post_id,hidden,content"})
    if not row:
        return jsonify({"error": "找不到這則留言"}), 404
    sb_delete("game_comments", {"id": f"eq.{cid}"})
    if not row[0].get("hidden"):
        adjust_comment_count(row[0]["post_id"], -1)
    _resolve_reports("comment", cid, "actioned")
    audit(who(), "comment_delete", {"id": cid, "content": (row[0].get("content") or "")[:80]})
    return jsonify({"ok": True})

# ───────── AI 嚮導分析 ─────────
FUNNEL_TOOLS = [("search_local_merchants", "收到推薦"), ("check_availability", "查詢空檔"), ("book_experience", "完成預約")]

def stats_ai():
    logs = fetch_all("chat_logs", "session_id,lang,category,tools,booked,error,ms,created_at")
    for l in logs:
        l["_t"] = to_tw(l.get("created_at"))
    msg_dt = [l["_t"] for l in logs]
    first_seen = {}
    for l in sorted(logs, key=lambda l: l["_t"] or datetime.min.replace(tzinfo=TW)):
        first_seen.setdefault(l.get("session_id") or id(l), l["_t"])
    labels, msgs = daily_series(msg_dt)
    _, sess = daily_series(list(first_seen.values()))

    sessions = defaultdict(set)
    for l in logs:
        sessions[l.get("session_id") or id(l)].update(l.get("tools") or [])
    funnel = [["開始對話", len(sessions)]] + [[label, sum(1 for t in sessions.values() if tool in t)]
                                              for tool, label in FUNNEL_TOOLS]
    tools = Counter(t for l in logs for t in (l.get("tools") or []))
    ms = [l["ms"] for l in logs if l.get("ms")]
    m_cur, m_prev = window_counts(msg_dt, 7)
    s_cur, s_prev = window_counts(list(first_seen.values()), 7)
    return {
        "messages": {"value": m_cur, "prev": m_prev, "total": len(logs)},
        "sessions": {"value": s_cur, "prev": s_prev, "total": len(sessions)},
        "conversion": round(funnel[-1][1] / len(sessions) * 100, 1) if sessions else None,
        "error_rate": round(sum(1 for l in logs if l.get("error")) / len(logs) * 100, 1) if logs else None,
        "avg_seconds": round(sum(ms) / len(ms) / 1000, 1) if ms else None,
        "trend": {"labels": labels, "messages": msgs, "sessions": sess},
        "langs": Counter({"zh-TW": "繁中", "en": "English", "ja": "日本語"}.get(l.get("lang"), l.get("lang") or "其他")
                         for l in logs).most_common(),
        "categories": Counter(l.get("category") or "自由提問" for l in logs).most_common(10),
        "tools": tools.most_common(),
        "funnel": funnel,
    }

admin_bp.add_url_rule('/admin/api/ai', 'ai', require_role("admin")(lambda: jsonify({**cached("ai", stats_ai),
                                                                                     "fetched_at": datetime.now(TW).isoformat()})))

# ───────── 限時活動 ─────────
def _parse_local(s):
    """後台表單送來的台灣時間（YYYY-MM-DDTHH:MM）"""
    try:
        return datetime.strptime(str(s)[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=TW)
    except ValueError:
        return None

def validate_event(d):
    errors = []
    name = str(d.get("name") or "").strip()
    desc = str(d.get("description") or "").strip()
    start, end = _parse_local(d.get("start")), _parse_local(d.get("end"))
    if not 1 <= len(name) <= 30:
        errors.append("活動名稱需介於 1–30 字")
    if len(desc) > 100:
        errors.append("活動說明最多 100 字")
    if not start or not end:
        errors.append("請填寫開始與結束時間")
    elif end <= start:
        errors.append("結束時間必須晚於開始時間")
    try:
        mult = round(float(d.get("multiplier")), 2)
    except (TypeError, ValueError):
        mult = 0
    if not 1.1 <= mult <= 5:
        errors.append("XP 倍率需介於 1.1–5 倍")
    scope = d.get("scope") if d.get("scope") in EVENT_SCOPES else None
    if not scope:
        errors.append("請選擇活動適用範圍")
    slugs = {g["slug"] for g in MERCHANT_GROUPS}
    cat = d.get("category") or None
    if cat and cat not in slugs:
        errors.append("商家類別不正確")
    if cat and scope not in ("all", "checkin"):
        errors.append("指定商家類別的活動只能套用在打卡")
    return {"name": name, "description": desc, "multiplier": mult, "scope": scope, "category": cat,
            "enabled": bool(d.get("enabled", True)),
            "start_at": start.astimezone(timezone.utc).isoformat() if start else None,
            "end_at": end.astimezone(timezone.utc).isoformat() if end else None}, errors

def _event_row(e):
    now = datetime.now(TW)
    start, end = to_tw(e.get("start_at")), to_tw(e.get("end_at"))
    status = ("disabled" if not e.get("enabled") else "upcoming" if start and now < start
              else "ended" if end and now > end else "active")
    return {**e, "status": status,
            "start_local": start.strftime("%Y-%m-%dT%H:%M") if start else "",
            "end_local": end.strftime("%Y-%m-%dT%H:%M") if end else ""}

@admin_bp.route('/admin/api/events')
@require_role("admin")
def admin_events():
    rows = [_event_row(e) for e in fetch_all("game_events", "*", {"order": "start_at.desc"})]
    return jsonify({"rows": rows, "scopes": EVENT_SCOPES,
                    "categories": [{"slug": g["slug"], "name": g["name"], "emoji": g["emoji"]} for g in MERCHANT_GROUPS],
                    "base": {k: game_settings()[k] for k in ("xp_checkin", "xp_quiz_correct", "xp_post")}})

@admin_bp.route('/admin/api/events', methods=['POST'])
@require_role("admin")
def create_event():
    clean, errors = validate_event(body())
    if errors:
        return jsonify({"errors": errors}), 400
    row = sb_insert("game_events", {**clean, "created_by": who()})
    if not row:
        return jsonify({"errors": ["儲存失敗，請確認已建立 game_events 資料表"]}), 500
    invalidate_events()
    audit(who(), "event_create", {"id": row.get("id"), "name": clean["name"], "multiplier": clean["multiplier"]})
    return jsonify(row)

@admin_bp.route('/admin/api/events/<int:eid>', methods=['PUT'])
@require_role("admin")
def update_event(eid):
    clean, errors = validate_event(body())
    if errors:
        return jsonify({"errors": errors}), 400
    sb_patch("game_events", {"id": f"eq.{eid}"}, clean)
    invalidate_events()
    audit(who(), "event_update", {"id": eid, "name": clean["name"]})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/events/<int:eid>', methods=['DELETE'])
@require_role("admin")
def delete_event(eid):
    sb_delete("game_events", {"id": f"eq.{eid}"})
    invalidate_events()
    audit(who(), "event_delete", {"id": eid})
    return jsonify({"ok": True})

# ───────── AI 出題 ─────────
GEN_PROMPT = """你是南投在地旅遊問答遊戲的出題老師。請根據下面提供的資料，出 __N__ 題繁體中文四選一選擇題。
規則：
1. 只能根據提供的資料出題，不可以加入資料裡沒有的事實或數字。
2. 每題 4 個選項，只有 1 個正確；錯誤選項要合理，但必須明確是錯的。
3. 題目要有趣，讓旅客認識在地職人、文化與永續精神；避免考冷門數字或電話地址。
4. answer 是正確選項的索引（0–3），正確答案請平均分散在不同位置。
5. fact 是答題後顯示的 1–2 句補充說明（60 字以內）。
__HINT__
只回傳 JSON 陣列，格式：[{"question": "...", "options": ["...", "...", "...", "..."], "answer": 0, "fact": "..."}]

資料：
__CONTEXT__"""

def _merchant_context(m, cat):
    return (f"【{m['name']}】類別：{cat['name']}；地點：{m.get('address', '')}；"
            f"特色：{'、'.join(m.get('tags') or [])}\n{m.get('description', '')}")

def _norm_q(s):
    return re.sub(r"[\s？?。，,！!「」]", "", s or "")

@admin_bp.route('/admin/api/questions/generate', methods=['POST'])
@require_role("admin", "moderator")
def generate_questions():
    d = body()
    source, value = d.get("source"), d.get("value")
    try:
        n = max(1, min(8, int(d.get("count") or 5)))
    except (TypeError, ValueError):
        n = 5
    if source == "merchant":
        found = [(m, CATEGORIES[s]) for s in CATEGORY_ORDER for m in CATEGORIES[s]["merchants"] if m["name"] == value]
        if not found:
            return jsonify({"error": "找不到這家商家"}), 400
        context = _merchant_context(*found[0])
    elif source == "category":
        cat = CATEGORIES.get(value)
        if not cat:
            return jsonify({"error": "找不到這個類別"}), 400
        context = f"類別介紹：{cat.get('description', '')}\n\n" + "\n\n".join(_merchant_context(m, cat) for m in cat["merchants"])
    else:
        return jsonify({"error": "請選擇出題依據"}), 400
    hint = str(d.get("hint") or "").strip()[:100]
    prompt = (GEN_PROMPT.replace("__N__", str(n)).replace("__CONTEXT__", context)
              .replace("__HINT__", f"6. 出題方向：{hint}" if hint else ""))
    try:
        resp = _gemini_client().models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=genai_types.GenerateContentConfig(response_mime_type='application/json', temperature=0.7))
        items = json_mod.loads(resp.text or "[]")
        if isinstance(items, dict):
            items = items.get("questions") or []
    except Exception as e:
        print(f"❌ AI 出題失敗：{e}")
        return jsonify({"error": "AI 出題失敗，請稍後再試"}), 502
    existing = {_norm_q(r.get("question")) for r in fetch_all("game_questions", "question")}
    drafts, skipped = [], 0
    for it in items if isinstance(items, list) else []:
        clean, errors = validate_question({**(it if isinstance(it, dict) else {}), "category": "商家知識", "enabled": True})
        key = _norm_q(clean["question"])
        if errors or key in existing:
            skipped += 1
            continue
        existing.add(key)
        drafts.append(clean)
    audit(who(), "questions_ai_generate", {"source": source, "value": value, "drafts": len(drafts)})
    return jsonify({"drafts": drafts, "skipped": skipped})

# ───────── 玩家管理 ─────────
PLAYER_COLS = "google_id,name,email,avatar_url,xp,checkin_count,post_count,quiz_played,quiz_correct,muted,certificate_sent,created_at"

def _player_row(u, role):
    li = level_info(u.get("xp") or 0)
    row = {**u, "level": li["level"], "title": li["title"], "emoji": li["emoji"], "max_level": li["max_level"]}
    if role != "admin":
        row.pop("email", None)
    return row

@admin_bp.route('/admin/api/players')
@require_role("admin", "moderator")
def list_players():
    q = (request.args.get("q") or "").strip().lower()
    role = current_admin()["role"]
    users = fetch_all("game_users", PLAYER_COLS, {"order": "xp.desc"})
    if q:
        users = [u for u in users if q in (u.get("name") or "").lower() or (role == "admin" and q in (u.get("email") or "").lower())]
    if request.args.get("muted") == "1":
        users = [u for u in users if u.get("muted")]
    return jsonify({"rows": [_player_row(u, role) for u in users[:200]], "total": len(users)})

@admin_bp.route('/admin/api/players/<gid>')
@require_role("admin", "moderator")
def player_detail(gid):
    rows = sb_get("game_users", {"google_id": f"eq.{gid}", "select": PLAYER_COLS})
    if not rows:
        return jsonify({"error": "找不到這位玩家"}), 404
    role = current_admin()["role"]
    posts = sb_get("game_posts", {"google_id": f"eq.{gid}", "select": "*", "order": "id.desc", "limit": "10"})
    answers = fetch_all("quiz_answers", "correct", {"google_id": f"eq.{gid}"})
    return jsonify({
        "user": _player_row(rows[0], role),
        "checkins": sb_get("checkins", {"google_id": f"eq.{gid}", "select": "id,merchant_name,photo_url,xp_earned,created_at",
                                        "order": "created_at.desc"}),
        "posts": [_public_post(p) for p in posts],
        "reports_received": sum(p.get("report_count") or 0 for p in posts),
        "quiz": {"answers": len(answers), "correct": sum(1 for a in answers if a.get("correct"))},
        "adjustments": sb_get("xp_adjustments", {"google_id": f"eq.{gid}", "select": "*", "order": "id.desc", "limit": "20"}),
    })

def _log_adjustment(gid, delta, reason):
    sb_insert("xp_adjustments", {"google_id": gid, "delta": delta, "reason": reason, "admin_email": who()})

@admin_bp.route('/admin/api/players/<gid>/xp', methods=['POST'])
@require_role("admin")
def adjust_player_xp(gid):
    d = body()
    try:
        delta = int(d.get("delta"))
    except (TypeError, ValueError):
        delta = 0
    reason = str(d.get("reason") or "").strip()
    if not delta or abs(delta) > 100000:
        return jsonify({"error": "調整數值需介於 ±1–100000"}), 400
    if not 2 <= len(reason) <= 100:
        return jsonify({"error": "請填寫調整原因（2–100 字）"}), 400
    if not get_user(gid):
        return jsonify({"error": "找不到這位玩家"}), 404
    stats = award_xp(gid, delta)
    _log_adjustment(gid, delta, reason)
    audit(who(), "player_xp_adjust", {"google_id": gid, "delta": delta, "reason": reason})
    _cache.clear()
    return jsonify({"ok": True, "stats": stats})

@admin_bp.route('/admin/api/players/<gid>/checkins/<int:cid>/revoke', methods=['POST'])
@require_role("admin")
def revoke_checkin(gid, cid):
    reason = str(body().get("reason") or "").strip()
    if not 2 <= len(reason) <= 100:
        return jsonify({"error": "請填寫撤銷原因（2–100 字）"}), 400
    row = sb_get("checkins", {"id": f"eq.{cid}", "google_id": f"eq.{gid}", "select": "merchant_name,xp_earned"})
    if not row:
        return jsonify({"error": "找不到這筆打卡"}), 404
    xp = row[0].get("xp_earned") or 0
    sb_delete("checkins", {"id": f"eq.{cid}"})
    stats = award_xp(gid, -xp, {"checkin_count": -1})
    _log_adjustment(gid, -xp, f"撤銷打卡「{row[0].get('merchant_name')}」：{reason}")
    audit(who(), "checkin_revoke", {"google_id": gid, "id": cid, "merchant": row[0].get("merchant_name"), "xp": -xp, "reason": reason})
    _cache.clear()
    return jsonify({"ok": True, "stats": stats})

@admin_bp.route('/admin/api/players/<gid>/mute', methods=['POST'])
@require_role("admin", "moderator")
def mute_player(gid):
    d = body()
    muted = bool(d.get("muted"))
    reason = str(d.get("reason") or "").strip()[:100]
    if muted and len(reason) < 2:
        return jsonify({"error": "請填寫暫停發言的原因"}), 400
    sb_patch("game_users", {"google_id": f"eq.{gid}"}, {"muted": muted})
    audit(who(), "player_mute" if muted else "player_unmute", {"google_id": gid, "reason": reason})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/players/<gid>/certificate', methods=['POST'])
@require_role("admin")
def resend_certificate(gid):
    u = get_user(gid)
    if not u:
        return jsonify({"error": "找不到這位玩家"}), 404
    li = level_info(u.get("xp") or 0)
    if li["level"] < li["max_level"]:
        return jsonify({"error": "這位玩家還沒到最高等級"}), 400
    sb_patch("game_users", {"google_id": f"eq.{gid}"}, {"certificate_sent": True})
    threading.Thread(target=send_certificate, args=(u.get("email"), u.get("name"), u.get("xp") or 0,
                                                    u.get("checkin_count") or 0), daemon=True).start()
    audit(who(), "certificate_resend", {"google_id": gid})
    return jsonify({"ok": True})

# ───────── 營運資料編輯：預約 ─────────
BOOKING_STATUSES = ("待確認", "已確認", "已婉拒")
PHONE_RE = re.compile(r"^[0-9+\-() ]{8,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
N8N_BOOKING_URL = os.getenv("N8N_WEBHOOK_URL", "https://nantoutravel.app.n8n.cloud/webhook/nantou-booking")

def validate_booking(d):
    errors = []
    artisan = str(d.get("artisan") or "").strip()
    m = match_merchant(artisan)
    if not artisan:
        errors.append("請選擇商家")
    elif not m:
        errors.append(f"「{artisan}」不在商家名錄中")
    try:
        day = datetime.strptime(str(d.get("date") or ""), "%Y-%m-%d").date()
    except ValueError:
        day = None
        errors.append("請選擇體驗日期")
    try:
        pax = int(d.get("pax"))
    except (TypeError, ValueError):
        pax = 0
    if not 1 <= pax <= 50:
        errors.append("人數需介於 1–50")
    name = str(d.get("customer") or "").strip()
    phone = str(d.get("phone") or "").strip()
    email = str(d.get("email") or "").strip()
    if not 1 <= len(name) <= 30:
        errors.append("請填寫旅客姓名（30 字內）")
    if not PHONE_RE.match(phone):
        errors.append("電話格式不正確")
    if email and not EMAIL_RE.match(email):
        errors.append("Email 格式不正確")
    status = d.get("status") if d.get("status") in BOOKING_STATUSES else None
    if not status:
        errors.append("狀態不正確")
    return {"artisan": m["name"] if m else artisan, "booking_date": day.isoformat() if day else None,
            "people_count": pax, "customer_name": name, "phone": phone, "email": email, "status": status}, errors

def booking_email_html(b, status, note):
    ok = status == "已確認"
    title = "您的南投職人體驗預約已確認" if ok else "關於您的南投職人體驗預約"
    body = ("商家已確認您的預約，期待與您見面！" if ok else
            "很抱歉，商家這次無法接受您的預約。歡迎回到網站，請 AI 嚮導緣投幫您推薦其他職人體驗。")
    esc = html_escape
    rate_html = (f'<p>體驗結束後，歡迎<a href="https://nantoutravel.com/rate/{esc(b.get("booking_id") or "")}" style="color:#2d4a35;font-weight:bold">為這次體驗評分</a>，'
                 f'您的回饋能幫助更多旅人認識南投職人。</p>') if ok and b.get("booking_id") else ""
    note_html = f'<p style="background:#fbf5e8;border-left:3px solid #C8A96E;padding:10px 14px;border-radius:0 8px 8px 0">{esc(note)}</p>' if note else ""
    return f"""<div style="font-family:'Noto Sans TC',Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1f2a22">
  <h2 style="color:#2d4a35">{title}</h2>
  <p>親愛的 {esc(b.get('customer_name') or '旅客')} 您好，</p><p>{body}</p>{note_html}
  <table style="width:100%;border-collapse:collapse;background:#f7f5f0;border-radius:12px;margin:16px 0">
    <tr><td style="padding:10px 14px;color:#888">預約編號</td><td style="padding:10px 14px"><b>{esc(b.get('booking_id') or '')}</b></td></tr>
    <tr><td style="padding:10px 14px;color:#888">職人商家</td><td style="padding:10px 14px">{esc(b.get('artisan') or '')}</td></tr>
    <tr><td style="padding:10px 14px;color:#888">體驗日期</td><td style="padding:10px 14px">{esc(str(b.get('booking_date') or ''))}</td></tr>
    <tr><td style="padding:10px 14px;color:#888">人數</td><td style="padding:10px 14px">{esc(str(b.get('people_count') or ''))} 位</td></tr>
  </table>{rate_html}
  <p style="color:#888;font-size:13px">深度與美學旅遊研究室 · <a href="https://nantoutravel.com" style="color:#2d4a35">nantoutravel.com</a></p></div>"""

@admin_bp.route('/admin/api/bookings', methods=['POST'])
@require_role("admin")
def create_booking():
    d = body()
    clean, errors = validate_booking({**d, "status": d.get("status") or "待確認"})
    if errors:
        return jsonify({"errors": errors}), 400
    booking_id = uuid.uuid4().hex[:8].upper()
    if d.get("notify_merchant"):
        payload = {"artisan": clean["artisan"], "booking_date": clean["booking_date"], "people_count": clean["people_count"],
                   "customer_name": clean["customer_name"], "phone": clean["phone"], "email": clean["email"],
                   "booking_id": booking_id, "status": "new_booking"}
        try:
            r = requests.post(N8N_BOOKING_URL, json=payload, headers=n8n_headers(), timeout=15)
            if r.status_code >= 400:
                return jsonify({"errors": [f"n8n 回應錯誤（{r.status_code}），預約沒有建立"]}), 502
        except Exception as e:
            return jsonify({"errors": [f"無法連線 n8n：{str(e)[:80]}"]}), 502
    else:
        if not sb_insert("bookings", {**clean, "booking_id": booking_id}):
            return jsonify({"errors": ["儲存失敗"]}), 500
    audit(who(), "booking_create", {"booking_id": booking_id, "artisan": clean["artisan"], "notify": bool(d.get("notify_merchant"))})
    _cache.clear()
    return jsonify({"ok": True, "booking_id": booking_id})

@admin_bp.route('/admin/api/bookings/<int:rid>', methods=['PUT'])
@require_role("admin")
def update_booking(rid):
    d = body()
    clean, errors = validate_booking(d)
    if errors:
        return jsonify({"errors": errors}), 400
    old = sb_get("bookings", {"id": f"eq.{rid}", "select": "*"})
    if not old:
        return jsonify({"errors": ["找不到這筆預約"]}), 404
    old = old[0]
    sb_patch("bookings", {"id": f"eq.{rid}"}, clean)
    changed = {k: [old.get(k), v] for k, v in clean.items() if str(old.get(k) or "") != str(v or "")}
    mailed = False
    if d.get("notify_customer") and old.get("status") != clean["status"] and clean["status"] in ("已確認", "已婉拒") and clean["email"]:
        merged = {**old, **clean}
        subject = "您的南投職人體驗預約已確認" if clean["status"] == "已確認" else "關於您的南投職人體驗預約"
        threading.Thread(target=send_email, args=(clean["email"], subject,
                                                   booking_email_html(merged, clean["status"], str(d.get("note") or "")[:200]),
                                                   clean["customer_name"]), daemon=True).start()
        mailed = True
    audit(who(), "booking_update", {"id": rid, "booking_id": old.get("booking_id"), "changed": changed, "mailed": mailed})
    _cache.clear()
    return jsonify({"ok": True, "mailed": mailed})

@admin_bp.route('/admin/api/bookings/<int:rid>', methods=['DELETE'])
@require_role("admin")
def delete_booking(rid):
    old = sb_get("bookings", {"id": f"eq.{rid}", "select": "booking_id,artisan,customer_name,booking_date"})
    if not old:
        return jsonify({"error": "找不到這筆預約"}), 404
    sb_delete("bookings", {"id": f"eq.{rid}"})
    audit(who(), "booking_delete", {"id": rid, **old[0]})
    _cache.clear()
    return jsonify({"ok": True})

# ───────── 營運資料編輯：商家 LINE 綁定 ─────────
LINE_ID_RE = re.compile(r"^U[0-9a-f]{32}$")

@admin_bp.route('/admin/api/line-bindings')
@require_role("admin")
def list_line_bindings():
    rows = fetch_all("merchants", "id,artisan,line_id", {"order": "id.asc"})
    counts = Counter(r.get("artisan") for r in rows)
    out = []
    for r in rows:
        m = match_merchant(r.get("artisan"))
        out.append({**r, "matched": m["name"] if m else None, "exact": bool(m and m["name"] == r.get("artisan")),
                    "duplicate": counts[r.get("artisan")] > 1, "valid_line_id": bool(LINE_ID_RE.match(r.get("line_id") or ""))})
    bound = {o["matched"] for o in out if o["matched"] and o["valid_line_id"]}
    return jsonify({"rows": out, "unbound": [m["name"] for m in MERCHANTS if m["name"] not in bound],
                    "total": len(MERCHANTS), "merchant_groups": MERCHANT_GROUPS})

def validate_binding(d, rid=None):
    errors = []
    artisan = str(d.get("artisan") or "").strip()
    line_id = str(d.get("line_id") or "").strip()
    if artisan not in _BY_NAME:
        errors.append("請從商家名錄中選擇店名（店名必須完全一致，LINE 通知才找得到）")
    if not LINE_ID_RE.match(line_id):
        errors.append("LINE User ID 格式不正確（U 開頭，後面 32 個英數字；商家可在 LINE Bot 輸入「我的ID」取得）")
    dup = [r for r in sb_get("merchants", {"artisan": f"eq.{artisan}", "select": "id"}) if r["id"] != rid]
    if artisan and dup:
        errors.append("這家商家已經綁定過了，請直接修改原本那筆")
    return {"artisan": artisan, "line_id": line_id}, errors

@admin_bp.route('/admin/api/line-bindings', methods=['POST'])
@require_role("admin")
def create_binding():
    clean, errors = validate_binding(body())
    if errors:
        return jsonify({"errors": errors}), 400
    row = sb_insert("merchants", clean)
    if not row:
        return jsonify({"errors": ["儲存失敗"]}), 500
    audit(who(), "line_binding_create", {"artisan": clean["artisan"]})
    _cache.clear()
    return jsonify(row)

@admin_bp.route('/admin/api/line-bindings/<int:rid>', methods=['PUT'])
@require_role("admin")
def update_binding(rid):
    clean, errors = validate_binding(body(), rid)
    if errors:
        return jsonify({"errors": errors}), 400
    sb_patch("merchants", {"id": f"eq.{rid}"}, clean)
    audit(who(), "line_binding_update", {"id": rid, "artisan": clean["artisan"]})
    _cache.clear()
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/line-bindings/<int:rid>', methods=['DELETE'])
@require_role("admin")
def delete_binding(rid):
    old = sb_get("merchants", {"id": f"eq.{rid}", "select": "artisan"})
    sb_delete("merchants", {"id": f"eq.{rid}"})
    audit(who(), "line_binding_delete", {"id": rid, "artisan": old[0].get("artisan") if old else None})
    _cache.clear()
    return jsonify({"ok": True})

# ───────── 營運資料編輯：首頁輪播活動 ─────────
def validate_carousel(d):
    errors = []
    title = str(d.get("title") or "").strip()
    artisan = str(d.get("artisan") or "").strip()
    if not 1 <= len(title) <= 40:
        errors.append("活動標題需介於 1–40 字")
    if not 1 <= len(artisan) <= 40:
        errors.append("請填寫商家名稱")
    def opt_int(key, label, hi):
        v = d.get(key)
        if v in (None, ""):
            return None
        try:
            v = int(v)
        except (TypeError, ValueError):
            errors.append(f"{label}必須是數字")
            return None
        if not 0 <= v <= hi:
            errors.append(f"{label}需介於 0–{hi}")
        return v
    desc = str(d.get("description") or "").strip()
    if len(desc) > 300:
        errors.append("活動說明最多 300 字")
    return {"title": title, "artisan": artisan, "event_date": str(d.get("event_date") or "").strip()[:30],
            "capacity": opt_int("capacity", "名額", 1000), "price": opt_int("price", "費用", 100000),
            "description": desc, "active": bool(d.get("active", True))}, errors

@admin_bp.route('/admin/api/carousel')
@require_role("admin")
def list_carousel():
    return jsonify({"rows": fetch_all("events", "*", {"order": "id.desc"}), "merchant_groups": MERCHANT_GROUPS})

def _carousel_image(d, rid):
    if d.get("image"):
        return upload_image(d["image"], "carousel", f"admin{rid or ''}")
    if d.get("remove_image"):
        return ""
    return None

@admin_bp.route('/admin/api/carousel', methods=['POST'])
@require_role("admin")
def create_carousel():
    d = body()
    clean, errors = validate_carousel(d)
    if errors:
        return jsonify({"errors": errors}), 400
    img = _carousel_image(d, None)
    row = sb_insert("events", {**clean, **({"image_url": img} if img else {})})
    if not row:
        return jsonify({"errors": ["儲存失敗"]}), 500
    audit(who(), "carousel_create", {"id": row.get("id"), "title": clean["title"]})
    return jsonify(row)

@admin_bp.route('/admin/api/carousel/<int:rid>', methods=['PUT'])
@require_role("admin")
def update_carousel(rid):
    d = body()
    clean, errors = validate_carousel(d)
    if errors:
        return jsonify({"errors": errors}), 400
    img = _carousel_image(d, rid)
    if img is not None:
        clean["image_url"] = img or None
    sb_patch("events", {"id": f"eq.{rid}"}, clean)
    audit(who(), "carousel_update", {"id": rid, "title": clean["title"], "active": clean["active"]})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/carousel/<int:rid>', methods=['DELETE'])
@require_role("admin")
def delete_carousel(rid):
    old = sb_get("events", {"id": f"eq.{rid}", "select": "title,artisan"})
    sb_delete("events", {"id": f"eq.{rid}"})
    audit(who(), "carousel_delete", {"id": rid, **(old[0] if old else {})})
    return jsonify({"ok": True})

# ───────── 網站設定：公告列、AI 補充指示 ─────────
@admin_bp.route('/admin/api/site')
@require_role("admin")
def get_site():
    return jsonify(site_settings())

@admin_bp.route('/admin/api/site', methods=['POST'])
@require_role("admin")
def save_site():
    d = body()
    ann = d.get("announcement") or {}
    errors = []
    text = str(ann.get("text") or "").strip()
    link = str(ann.get("link") or "").strip()
    style = ann.get("style") if ann.get("style") in ("info", "promo", "warning") else "info"
    enabled = bool(ann.get("enabled"))
    if enabled and not text:
        errors.append("公告開啟時要填寫內容")
    if len(text) > 80:
        errors.append("公告最多 80 字")
    if link and not re.match(r"^(https?://|/)[^\s<>\"']*$", link):
        errors.append("連結必須是 https:// 開頭的網址，或 / 開頭的站內路徑")
    extra = str(d.get("ai_extra") or "").strip()
    if len(extra) > 800:
        errors.append("AI 補充指示最多 800 字")
    if errors:
        return jsonify({"errors": errors}), 400
    value = {"announcement": {"enabled": enabled, "text": text, "link": link, "style": style}, "ai_extra": extra}
    before = site_settings()
    r = requests.post(f"{SUPABASE_URL}/rest/v1/game_settings",
                      json={"key": "site", "value": value, "updated_by": who(), "updated_at": datetime.now(TW).isoformat()},
                      headers=_h({"Prefer": "resolution=merge-duplicates,return=minimal"}), timeout=8)
    if r.status_code not in (200, 201, 204):
        return jsonify({"errors": [f"儲存失敗（{r.status_code}）"]}), 500
    invalidate_site()
    audit(who(), "site_update", {"announcement": [before.get("announcement"), value["announcement"]],
                                 "ai_extra_changed": before.get("ai_extra") != extra})
    return jsonify({"ok": True, **value})

# ───────── 操作紀錄 ─────────
@admin_bp.route('/admin/api/audit')
@require_role("admin")
def audit_log():
    params = {"order": "id.desc", "limit": "300", "select": "id,email,action,detail,created_at"}
    if request.args.get("email"):
        params["email"] = f"eq.{request.args['email']}"
    rows = sb_get("admin_audit_log", params)
    act = (request.args.get("action") or "").strip()
    if act:
        rows = [r for r in rows if (r.get("action") or "").startswith(act)]
    return jsonify({"rows": rows, "admins": sorted({r.get("email") for r in rows if r.get("email")})})

# ───────── 後台 AI 助理（唯讀工具 + Gemini）─────────
from merchant_directory import resolve_category

ASSISTANT_PROMPT = """你是「南投職人旅遊平台」管理後台的資料助理，替管理員解讀營運、商家、預約、遊戲、AI 嚮導與系統狀態的數據。
規則：
1. 一律先呼叫工具取得資料再回答，絕對不可以自己編造數字；工具沒有提供的資料，就直接說「後台目前沒有這項資料」。
2. 用繁體中文回答，先講結論，再用條列補充；重要數字用 **粗體**；比較時講清楚期間（例如「本週 vs 上週」）。
3. 你只能讀取資料，不能修改任何東西。需要處理時，告訴管理員要去後台哪一頁操作，例如「LINE 綁定」、「預約分析 → 全部預約」、「動態管理 → 待審核」、「題目管理」、「遊戲設定」。
4. 旅客個資已經遮蔽，不要推測或要求完整姓名、電話、Email。
5. 回答精簡，一般不超過 250 字；除非管理員要求，不要列出超過 10 筆明細。
6. 今天是 __TODAY__（台灣時間）。"""

def _jsonable(x):
    return json_mod.loads(json_mod.dumps(x, ensure_ascii=False, default=str))

def _mask_name(n):
    n = (n or "").strip()
    return (n[0] + "○" * max(1, len(n) - 1)) if n else ""

def tool_overview() -> dict:
    """營運總覽：本週預約數與上週比較、待確認與逾時未回覆的預約、商家確認率、平均評分、本週新玩家、今日打卡、近 14 天每日趨勢、未綁定 LINE 的商家。"""
    d = stats_overview(load_data())
    t, k = d["trend"], d["kpis"]
    kpis = {"bookings_last_7_days": k["bookings_7d"]["value"], "bookings_previous_7_days": k["bookings_7d"]["prev"],
            "pending_bookings_all_time": k["pending"]["value"], "pending_bookings_waiting_over_24h": k["pending"]["overdue"],
            "merchant_confirm_rate_percent_all_time": k["confirm_rate"]["value"], "average_rating": k["rating"]["value"],
            "rating_count": k["rating"]["count"], "new_players_last_7_days": k["new_players_7d"]["value"],
            "new_players_previous_7_days": k["new_players_7d"]["prev"], "checkins_today": k["checkins_today"]["value"]}
    return {"kpis": kpis, "last_14_days": {"dates": t["labels"][-14:], "bookings": t["bookings"][-14:],
                                                "checkins": t["checkins"][-14:], "new_players": t["players"][-14:]},
            "overdue_bookings": d["todo"]["overdue"], "unbound_line_count": len(d["todo"]["unbound"]),
            "unbound_line_merchants": d["todo"]["unbound"][:15]}

def tool_booking_stats() -> dict:
    """預約分析：總預約與人次、狀態分布、各類別預約、熱門商家、提前幾天預訂、團體人數、未來 30 天每天的預約數、旅客最常下單的星期與時段。"""
    d = stats_bookings(load_data())
    today = d["today"]
    upcoming = [c for c in d["calendar"] if c["date"] >= today and c["count"]][:30]
    days = ["一", "二", "三", "四", "五", "六", "日"]
    slots = sorted(((v, f"星期{days[r]} {h}:00") for r, row in enumerate(d["heatmap"]) for h, v in enumerate(row) if v), reverse=True)[:5]
    return {"total": d["total"], "people": d["people"], "status": d["status"],
            "categories": [{"name": c["name"], "bookings": c["bookings"], "people": c["people"]} for c in d["categories"]],
            "top_merchants": d["top_merchants"], "lead_time": d["lead_time"], "party_size": d["party_size"],
            "upcoming_days_with_bookings": upcoming, "busiest_order_times": [{"time": s, "count": v} for v, s in slots]}

def tool_merchant_performance(category: str = "", sort_by: str = "visits", limit: int = 15) -> dict:
    """商家表現表：每家的預約數、人次、確認率、待確認數、評分、打卡數、貼文數、是否綁定 LINE，以及 AI 送出但對不到名錄的店名。

    Args:
        category: 只看某個類別（例如：文化永續、農業永續、永續住宿），留空代表全部。
        sort_by: 排序依據：visits（預約+打卡）、bookings、confirm_rate、rating、pending、checkins。
        limit: 最多回傳幾家，預設 15。
    """
    rows, unmatched = merchant_table(load_data())
    slug = resolve_category(category) if category else None
    if slug:
        rows = [r for r in rows if r["cat"] == slug]
    key = sort_by if sort_by in ("visits", "bookings", "confirm_rate", "rating", "pending", "checkins") else "visits"
    rows.sort(key=lambda r: (r.get(key) is None, -(r.get(key) or 0)))
    fields = ("name", "cat_name", "township", "bookings", "people", "confirm_rate", "pending", "rating", "checkins", "posts", "line_bound")
    return {"count": len(rows), "sorted_by": key, "merchants": [{k: r[k] for k in fields} for r in rows[:max(1, min(len(MERCHANTS), limit))]],
            "unmatched_names": unmatched.most_common(10)}

def tool_search_bookings(merchant: str = "", status: str = "", date_from: str = "", date_to: str = "", limit: int = 20) -> dict:
    """查詢預約明細（旅客姓名已遮蔽，不含電話與 Email）。

    Args:
        merchant: 商家名稱，可以只打部分名稱，留空代表全部。
        status: 待確認、已確認或已婉拒，留空代表全部。
        date_from: 體驗日期起（YYYY-MM-DD），留空不限。
        date_to: 體驗日期迄（YYYY-MM-DD），留空不限。
        limit: 最多回傳幾筆，預設 20。
    """
    rows = enrich_bookings(load_data()["bookings"])
    m = match_merchant(merchant) if merchant else None
    if merchant:
        rows = [b for b in rows if (b["_m"] and m and b["_m"]["name"] == m["name"]) or merchant in (b.get("artisan") or "")]
    if status:
        rows = [b for b in rows if b["_status"] == status]
    if date_from:
        rows = [b for b in rows if b["_date"] and b["_date"].isoformat() >= date_from]
    if date_to:
        rows = [b for b in rows if b["_date"] and b["_date"].isoformat() <= date_to]
    rows.sort(key=lambda b: b["_created"] or datetime.min.replace(tzinfo=TW), reverse=True)
    return {"count": len(rows), "bookings": [{
        "booking_id": b.get("booking_id"), "merchant": b["_m"]["name"] if b["_m"] else b.get("artisan"),
        "date": b["_date"].isoformat() if b["_date"] else b.get("booking_date"), "pax": b["_pax"], "status": b["_status"],
        "customer": _mask_name(b.get("customer_name")), "rating": b.get("rating"),
        "ordered_at": b["_created"].strftime("%Y-%m-%d %H:%M") if b["_created"] else None} for b in rows[:max(1, min(50, limit))]]}

def tool_impact() -> dict:
    """永續影響力：服務旅客人次、支持過的職人數、前三名集中度、吉尼係數、各鄉鎮造訪、原鄉占比、類別分布、每月造訪、零造訪職人。"""
    d = stats_impact(load_data())
    d.pop("lorenz", None)
    return d

def tool_ai_guide_stats() -> dict:
    """前台 AI 嚮導的使用情況：本週對話與訊息數、對話到預約的轉換漏斗、使用語言、旅客點的類別、AI 呼叫的工具、平均回應秒數、錯誤率。"""
    d = dict(cached("ai", stats_ai))
    d["trend"] = {k: v[-14:] for k, v in d["trend"].items()}
    return d

def tool_game_stats() -> dict:
    """遊戲（緣投探索者）數據：玩家數、證書數、打卡、問答局數、貼文留言按讚、等級漏斗、近 14 天活動、打卡 vs 預約、前 10 名玩家。"""
    d = stats_game(load_data())
    d["trend"] = {k: v[-14:] for k, v in d["trend"].items()}
    for p in d["top_players"]:
        p.pop("avatar", None)
    d["recent_posts"] = [{"user": p.get("user_name"), "content": (p.get("content") or "")[:60], "merchant": p.get("merchant_name"),
                          "likes": p.get("likes")} for p in d["recent_posts"]]
    return d

def tool_review_queue() -> dict:
    """動態管理的待審核內容：被 AI、禁用詞或檢舉標記而隱藏的貼文與留言，含標記原因與檢舉原因。"""
    reasons = _open_report_reasons()
    flt = {"moderation": f"in.({','.join(PENDING_MODERATION)})", "order": "id.asc"}
    items = [{"type": "貼文", "user": p.get("user_name"), "content": (p.get("content") or "")[:80], "flag": p.get("moderation"),
              "reason": p.get("moderation_reason"), "reports": reasons.get(("post", p["id"]), Counter()).most_common()}
             for p in fetch_all("game_posts", "*", flt)]
    items += [{"type": "留言", "user": c.get("user_name"), "content": (c.get("content") or "")[:80], "flag": c.get("moderation"),
               "reason": c.get("moderation_reason"), "reports": reasons.get(("comment", c["id"]), Counter()).most_common()}
              for c in fetch_all("game_comments", "id,user_name,content,moderation,moderation_reason", flt)]
    return {"count": len(items), "items": items[:20]}

def tool_question_quality() -> dict:
    """問答題庫品質：題數、整體答對率、被標記（太簡單、太難、答案可能有誤）的題目、最難與最簡單的題目。"""
    rows = fetch_all("game_questions", "*", {"order": "id.asc"})
    if not rows:
        return {"note": "題庫尚未匯入資料庫，目前使用內建題目，沒有答對率資料"}
    stats = question_stats()
    out = []
    for r in rows:
        s = stats.get(r["id"], {"n": 0, "correct": 0, "choices": [0, 0, 0, 0]})
        out.append({"id": r["id"], "question": r["question"][:60], "category": r.get("category"), "enabled": r.get("enabled"),
                    "answers": s["n"], "correct_rate": round(s["correct"] / s["n"] * 100) if s["n"] else None,
                    "flags": question_flags(r, s)})
    rated = [q for q in out if q["correct_rate"] is not None and q["answers"] >= MIN_ANSWERS_FOR_FLAG]
    total_n = sum(q["answers"] for q in out)
    return {"questions": len(out), "enabled": sum(1 for q in out if q["enabled"]), "total_answers": total_n,
            "overall_correct_rate": round(sum(stats[q["id"]]["correct"] for q in out if q["id"] in stats) / total_n * 100) if total_n else None,
            "flagged": [q for q in out if q["flags"]][:15],
            "hardest": sorted(rated, key=lambda q: q["correct_rate"])[:5], "easiest": sorted(rated, key=lambda q: -q["correct_rate"])[:5]}

def tool_system_status() -> dict:
    """系統健康：Supabase 連線與延遲、n8n 各工作流是否啟用與 24 小時成功失敗次數、最近失敗、環境變數是否設定。"""
    return cached("system", stats_system)

def tool_game_config() -> dict:
    """目前的遊戲設定（等級門檻、各項 XP、每局題數）、進行中與即將開始的限時活動、首頁公告與 AI 嚮導補充指示。"""
    events = [_event_row(e) for e in fetch_all("game_events", "*", {"order": "start_at.desc"})]
    return {"game_settings": {k: v for k, v in game_settings().items() if k != "blocked_words"},
            "events": [{k: e.get(k) for k in ("name", "multiplier", "scope", "category", "status", "start_local", "end_local")}
                       for e in events if e["status"] in ("active", "upcoming")],
            "site": site_settings()}

def tool_recent_admin_actions(limit: int = 20) -> dict:
    """最近的後台操作紀錄（誰在什麼時候做了什麼修改）。

    Args:
        limit: 最多回傳幾筆，預設 20。
    """
    rows = sb_get("admin_audit_log", {"order": "id.desc", "limit": str(max(1, min(100, limit))), "select": "email,action,detail,created_at"})
    return {"actions": [{**r, "detail": json_mod.dumps(r.get("detail") or {}, ensure_ascii=False)[:160]} for r in rows]}

ASSISTANT_TOOLS = {
    "tool_overview": (tool_overview, "總覽", ("admin",)),
    "tool_booking_stats": (tool_booking_stats, "預約分析", ("admin",)),
    "tool_merchant_performance": (tool_merchant_performance, "商家表現", ("admin",)),
    "tool_search_bookings": (tool_search_bookings, "全部預約", ("admin",)),
    "tool_impact": (tool_impact, "永續影響力", ("admin",)),
    "tool_ai_guide_stats": (tool_ai_guide_stats, "AI 嚮導", ("admin",)),
    "tool_system_status": (tool_system_status, "系統健康", ("admin",)),
    "tool_game_config": (tool_game_config, "遊戲設定／限時活動", ("admin",)),
    "tool_recent_admin_actions": (tool_recent_admin_actions, "操作紀錄", ("admin",)),
    "tool_game_stats": (tool_game_stats, "遊戲分析", ("admin", "moderator")),
    "tool_review_queue": (tool_review_queue, "動態管理", ("admin", "moderator")),
    "tool_question_quality": (tool_question_quality, "題目管理", ("admin", "moderator")),
}
_assistant_rate = defaultdict(list)

@admin_bp.route('/admin/api/assistant', methods=['POST'])
@require_role("admin", "moderator")
def admin_assistant():
    a = current_admin()
    now = time.time()
    hits = [t for t in _assistant_rate[a["email"]] if now - t < 60]
    if len(hits) >= 15:
        return jsonify({"error": "提問太頻繁，請稍等一下再問"}), 429
    _assistant_rate[a["email"]] = hits + [now]

    d = body()
    question = str(d.get("message") or "").strip()[:500]
    if not question:
        return jsonify({"error": "請輸入問題"}), 400
    allowed = {k: v for k, v in ASSISTANT_TOOLS.items() if a["role"] in v[2]}
    contents = []
    for h in (d.get("history") or [])[-10:]:
        if h.get("role") in ("user", "model") and h.get("text"):
            contents.append(genai_types.Content(role=h["role"], parts=[genai_types.Part(text=str(h["text"])[:2000])]))
    contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=question)]))
    prompt = ASSISTANT_PROMPT.replace("__TODAY__", today().strftime("%Y-%m-%d（%a）"))
    if a["role"] != "admin":
        prompt += "\n7. 這位使用者是內容審核員，只能查詢遊戲、動態與題目相關資料；營運、預約、商家數據請回答「這需要管理員權限」。"
    config = genai_types.GenerateContentConfig(
        system_instruction=prompt, tools=[v[0] for v in allowed.values()], temperature=0.2,
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True))
    used = []
    try:
        for _ in range(6):
            resp = _gemini_client().models.generate_content(model="gemini-2.5-flash", contents=contents, config=config)
            cand = resp.candidates[0].content if resp.candidates else None
            calls = [p.function_call for p in (cand.parts if cand and cand.parts else []) if p.function_call]
            if not calls:
                reply = resp.text or "抱歉，我沒有產生回答，請換個方式再問一次。"
                break
            results = []
            for fc in calls:
                tool = allowed.get(fc.name)
                try:
                    out = _jsonable(tool[0](**dict(fc.args or {}))) if tool else {"error": "沒有權限使用這個資料來源"}
                except Exception as e:
                    out = {"error": f"讀取失敗：{str(e)[:120]}"}
                if tool and tool[1] not in used:
                    used.append(tool[1])
                results.append(genai_types.Part(function_response=genai_types.FunctionResponse(name=fc.name, response={"result": out})))
            contents += [cand, genai_types.Content(role="user", parts=results)]
        else:
            reply = "這個問題需要查太多資料了，請把問題拆小一點再問。"
    except Exception as e:
        print(f"❌ 後台 AI 助理錯誤：{e}")
        return jsonify({"error": "AI 助理暫時無法回應，請稍後再試"}), 502
    audit(a["email"], "assistant_query", {"q": question[:200], "sources": used})
    return jsonify({"reply": reply, "sources": used})
