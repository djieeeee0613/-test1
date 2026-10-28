"""商家名錄的單一資料來源：結構化索引、店名比對、檢索。AI 嚮導、預約、後台都用這裡。"""
import re
import random
import difflib

from merchants_data import CATEGORIES, CATEGORY_ORDER


def _norm(s):
    s = re.sub(r"[（(].*?[）)]", "", s or "")
    return re.sub(r"[\s&＆×Xx·・\-—_,，。｜|]", "", s).lower()


def _township(addr):
    m = re.search(r"南投縣?(.{1,3}?[鄉鎮市])", addr or "")
    return m.group(1) if m else "其他"


MERCHANTS = []
for _slug in CATEGORY_ORDER:
    _c = CATEGORIES[_slug]
    for _m in _c["merchants"]:
        MERCHANTS.append({
            "name": _m["name"], "norm": _norm(_m["name"]), "cat": _slug, "cat_name": _c["name"],
            "emoji": _c["emoji"], "color": _c["color"], "township": _township(_m.get("address")),
            "address": _m.get("address", ""), "phone": _m.get("phone", ""),
            "description": _m.get("description", ""), "tags": _m.get("tags") or [],
        })
BY_NAME = {m["name"]: m for m in MERCHANTS}
TOWNSHIPS = sorted({m["township"] for m in MERCHANTS})
CATEGORY_NAMES = {CATEGORIES[s]["name"]: s for s in CATEGORY_ORDER}


def match_merchant(name):
    """把 AI 或使用者輸入的店名對應回正式名稱：完全比對 → 包含比對 → 模糊比對"""
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


def resolve_category(text):
    """接受 slug、類別名稱或含 emoji 的類別名稱"""
    t = (text or "").strip()
    if t in CATEGORIES:
        return t
    t = re.sub(r"^[^\w]+", "", t)
    for name, slug in CATEGORY_NAMES.items():
        if t and (t == name or t in name or name in t):
            return slug
    aliases = {"購物": "shop", "伴手禮": "shop", "選物": "shop", "咖啡": "farm", "茶": "farm", "農": "farm",
               "工藝": "culture", "手作": "culture", "文化": "culture", "宗教": "culture", "寺": "culture", "廟": "culture",
               "溫泉": "stay", "泡湯": "stay", "住宿": "stay", "民宿": "stay", "飯店": "stay",
               "餐": "dining", "美食": "dining", "部落": "tribe", "原住民": "tribe"}
    return next((slug for k, slug in aliases.items() if k in t), None)


def search(category="", township="", keyword="", shuffle=True):
    rows = MERCHANTS
    slug = resolve_category(category) if category else None
    if category and slug:
        rows = [m for m in rows if m["cat"] == slug]
    if township:
        t = township.strip().rstrip("鄉鎮市")
        rows = [m for m in rows if t and t in m["township"]]
    if keyword:
        k = keyword.strip().lower()
        rows = [m for m in rows if k in " ".join([m["name"], m["description"], " ".join(m["tags"]),
                                                  m["address"], m["cat_name"]]).lower()]
    rows = list(rows)
    if shuffle:
        random.shuffle(rows)
    return rows, slug


def format_for_ai(rows, events_by_merchant=None):
    lines = []
    for m in rows:
        line = (f"【{m['name']}】{m['emoji']} {m['cat_name']}｜{m['township']}｜永續依據：{'、'.join(m['tags']) or '無'}\n"
                f"  介紹：{m['description']}\n  地址：{m['address']}｜電話：{m['phone']}")
        for e in (events_by_merchant or {}).get(m["name"], [])[:2]:
            bits = [e.get("event_date")] + ([f"NT${e['price']}"] if e.get("price") is not None else []) \
                   + ([f"名額 {e['capacity']} 人"] if e.get("capacity") is not None else [])
            line += f"\n  🎉 近期活動：{e.get('title')}（{'，'.join(b for b in bits if b)}）"
        lines.append(line)
    return "\n\n".join(lines)
