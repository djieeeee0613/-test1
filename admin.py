import os
import re
import time
import difflib
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta
from functools import wraps

import requests
from flask import Blueprint, request, jsonify, render_template, session, redirect

from merchants_data import CATEGORIES, CATEGORY_ORDER
import threading
from game import (SUPABASE_URL, SUPABASE_KEY, GOOGLE_CLIENT_ID, TW, DEFAULT_SETTINGS,
                  _h, sb_get, sb_insert, sb_patch, sb_delete, verify_google_token, level_info, levels,
                  settings as game_settings, invalidate_settings, invalidate_questions, question_bank,
                  delete_post_cascade, send_certificate)
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

# ───────── 商家名稱比對 ─────────
def _norm(s):
    s = re.sub(r"[（(].*?[）)]", "", s or "")
    return re.sub(r"[\s&＆×Xx·・\-—_,，。]", "", s).lower()

def _township(addr):
    m = re.search(r"南投縣?(.{1,3}?[鄉鎮市])", addr or "")
    return m.group(1) if m else "其他"

MERCHANTS = []
for _slug in CATEGORY_ORDER:
    _c = CATEGORIES[_slug]
    for _m in _c["merchants"]:
        MERCHANTS.append({"name": _m["name"], "norm": _norm(_m["name"]), "cat": _slug,
                          "cat_name": _c["name"], "emoji": _c["emoji"], "color": _c["color"],
                          "township": _township(_m.get("address"))})
_BY_NAME = {m["name"]: m for m in MERCHANTS}

def match_merchant(name):
    n = _norm(name)
    if not n:
        return None
    for m in MERCHANTS:
        if m["norm"] == n:
            return m
    for m in MERCHANTS:
        if len(n) >= 3 and (n in m["norm"] or m["norm"] in n):
            return m
    close = difflib.get_close_matches(n, [m["norm"] for m in MERCHANTS], n=1, cutoff=0.7)
    return next((m for m in MERCHANTS if close and m["norm"] == close[0]), None)

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
    return jsonify({"source": source, "rows": rows, "categories": QUESTION_CATEGORIES,
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

# ───────── 動態管理 ─────────
def _public_post(p):
    return {k: p.get(k) for k in ("id", "user_name", "user_avatar", "user_level", "content", "merchant_name",
                                  "image_url", "likes", "comment_count", "hidden", "pinned", "created_at")}

@admin_bp.route('/admin/api/feed')
@require_role("admin", "moderator")
def admin_feed():
    status, q = request.args.get("status", ""), (request.args.get("q") or "").strip().lower()
    posts = fetch_all("game_posts", "*", {"order": "id.desc"}, max_rows=3000)
    counts = {"all": len(posts), "hidden": sum(1 for p in posts if p.get("hidden")),
              "pinned": sum(1 for p in posts if p.get("pinned"))}
    if status == "hidden":
        posts = [p for p in posts if p.get("hidden")]
    elif status == "pinned":
        posts = [p for p in posts if p.get("pinned")]
    elif status == "image":
        posts = [p for p in posts if p.get("image_url")]
    if q:
        posts = [p for p in posts if any(q in str(p.get(k) or "").lower() for k in ("content", "user_name", "merchant_name"))]
    return jsonify({"rows": [_public_post(p) for p in posts[:150]], "total": len(posts), "counts": counts})

@admin_bp.route('/admin/api/feed/<int:pid>', methods=['POST'])
@require_role("admin", "moderator")
def moderate_post(pid):
    action = body().get("action")
    patch = {"hide": {"hidden": True}, "unhide": {"hidden": False},
             "pin": {"pinned": True}, "unpin": {"pinned": False}}.get(action)
    if not patch:
        return jsonify({"error": "未知的操作"}), 400
    sb_patch("game_posts", {"id": f"eq.{pid}"}, patch)
    audit(who(), f"post_{action}", {"id": pid})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/feed/<int:pid>', methods=['DELETE'])
@require_role("admin", "moderator")
def admin_delete_post(pid):
    row = sb_get("game_posts", {"id": f"eq.{pid}", "select": "user_name,content"})
    if not row:
        return jsonify({"error": "找不到這篇貼文"}), 404
    delete_post_cascade(pid)
    audit(who(), "post_delete", {"id": pid, "user": row[0].get("user_name"), "content": (row[0].get("content") or "")[:80]})
    return jsonify({"ok": True})

@admin_bp.route('/admin/api/feed/<int:pid>/comments')
@require_role("admin", "moderator")
def admin_comments(pid):
    return jsonify(sb_get("game_comments", {"post_id": f"eq.{pid}", "order": "id.asc",
                                            "select": "id,user_name,user_avatar,content,hidden,created_at"}))

def _adjust_comment_count(pid, delta):
    post = sb_get("game_posts", {"id": f"eq.{pid}", "select": "comment_count"})
    if post:
        sb_patch("game_posts", {"id": f"eq.{pid}"}, {"comment_count": max(0, (post[0].get("comment_count") or 0) + delta)})

@admin_bp.route('/admin/api/comments/<int:cid>', methods=['POST'])
@require_role("admin", "moderator")
def moderate_comment(cid):
    action = body().get("action")
    if action not in ("hide", "unhide"):
        return jsonify({"error": "未知的操作"}), 400
    row = sb_get("game_comments", {"id": f"eq.{cid}", "select": "post_id,hidden"})
    if not row:
        return jsonify({"error": "找不到這則留言"}), 404
    hide = action == "hide"
    if bool(row[0].get("hidden")) != hide:
        sb_patch("game_comments", {"id": f"eq.{cid}"}, {"hidden": hide})
        _adjust_comment_count(row[0]["post_id"], -1 if hide else 1)
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
        _adjust_comment_count(row[0]["post_id"], -1)
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
