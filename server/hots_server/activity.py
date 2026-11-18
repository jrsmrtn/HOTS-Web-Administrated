"""Activity journal and screen time received from the PCs (day summaries per child account).

The agent sends, every 5 minutes and on demand (`activity.sync`), one summary per child and per day
(see hosts_editor/core_activity.py). Each summary replaces the previous one for that day. Summaries
are kept KEEP_DAYS days.
"""

import json
import re
import time

from . import db

KEEP_DAYS = 90
MAX_ITEMS = 10
MAX_SUMMARY_BYTES = 400 * 1024
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SID_RE = re.compile(r"^S-1-(5-21|12-1)(-\d{1,10}){4,6}$")

# Table activity_days: see db.SCHEMA.



def _num(value, lo=0, hi=10 ** 9) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(max(lo, min(hi, value)))


def _txt(value, maxlen=200) -> str:
    return value[:maxlen] if isinstance(value, str) else ""


def _items(value, fields: dict, limit: int) -> list:
    out = []
    for item in (value if isinstance(value, list) else [])[:limit]:
        if not isinstance(item, dict):
            continue
        out.append({k: (_txt(item.get(k), n) if kind == "s" else bool(item.get(k)) if kind == "b"
                        else _num(item.get(k), hi=2 * 10 ** 10)) for k, (kind, n) in fields.items()})
    return out


def clean_summary(raw) -> dict:
    """Validated copy of one day summary (keeps the console safe from malformed agents)."""
    if not isinstance(raw, dict) or not DAY_RE.match(str(raw.get("day", ""))) or not SID_RE.match(str(raw.get("sid", ""))):
        raise ValueError("résumé invalide")
    hours = raw.get("hours") if isinstance(raw.get("hours"), list) else []
    hours = [_num(h, hi=3600) for h in hours[:24]] + [0] * (24 - min(24, len(hours)))
    s, n, b = ("s", 300), ("n", 0), ("b", 0)
    return {
        "day": raw["day"], "sid": raw["sid"], "user": _txt(raw.get("user"), 100),
        "screen_s": _num(raw.get("screen_s"), hi=86400), "active_s": _num(raw.get("active_s"), hi=86400),
        "hours": hours,
        "apps": _items(raw.get("apps"), {"name": ("s", 100), "exe": ("s", 120), "path": s, "s": n, "first": n,
                                          "last": n, "browser": b}, 150),
        "sites": _items(raw.get("sites"), {"site": ("s", 200), "n": n, "blocked": n, "by": ("s", 60), "s": n,
                                           "first": n, "last": n}, 300),
        "titles": _items(raw.get("titles"), {"title": ("s", 160), "app": ("s", 100), "s": n, "last": n}, 100),
        "blocked_apps": _items(raw.get("blocked_apps"), {"name": ("s", 100), "path": s, "n": n, "last": n,
                                                         "by": ("s", 60)}, 100),
        "events": _items(raw.get("events"), {"t": n, "kind": ("s", 20), "text": ("s", 200)}, 100),
        "helper": {"restarts": _num((raw.get("helper") or {}).get("restarts") if isinstance(raw.get("helper"), dict) else 0),
                   "last_seen": _num((raw.get("helper") or {}).get("last_seen") if isinstance(raw.get("helper"), dict) else 0,
                                     hi=2 * 10 ** 10)},
    }


def store(device_id: str, items) -> int:
    """Save the summaries sent by a PC. Returns how many were stored."""
    if not isinstance(items, list):
        return 0
    stored = 0
    now = time.time()
    for raw in items[:MAX_ITEMS]:
        try:
            item = clean_summary(raw)
        except ValueError:
            continue
        data = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if len(data.encode("utf-8")) > MAX_SUMMARY_BYTES:
            continue
        db.ex("INSERT INTO activity_days(device_id, day, sid, user_name, screen_s, data_json, updated_at) "
              "VALUES(?,?,?,?,?,?,?) ON CONFLICT(device_id, day, sid) DO UPDATE SET user_name = excluded.user_name, "
              "screen_s = excluded.screen_s, data_json = excluded.data_json, updated_at = excluded.updated_at",
              (device_id, item["day"], item["sid"], item["user"], item["screen_s"], data, now))
        stored += 1
    return stored


def for_device(device_id: str, day: str = "") -> dict:
    """Console view: the day's summaries (all children), the days available, a 7-day overview."""
    days = [r["day"] for r in db.q("SELECT DISTINCT day FROM activity_days WHERE device_id = ? ORDER BY day DESC LIMIT ?",
                                   (device_id, KEEP_DAYS))]
    if not day or not DAY_RE.match(day):
        day = days[0] if days else time.strftime("%Y-%m-%d")
    rows = db.q("SELECT data_json, updated_at FROM activity_days WHERE device_id = ? AND day = ? ORDER BY user_name",
                (device_id, day))
    users = []
    for r in rows:
        data = db.loads(r["data_json"], None)
        if isinstance(data, dict):
            data["updated_at"] = r["updated_at"]
            users.append(data)
    week = [dict(r) for r in db.q(
        "SELECT day, sid, user_name, screen_s FROM activity_days WHERE device_id = ? AND day <= ? ORDER BY day DESC "
        "LIMIT 70", (device_id, day))]
    return {"day": day, "days": days, "users": users, "week": week}


def purge(now: float = None) -> None:
    now = time.time() if now is None else now
    cutoff = time.strftime("%Y-%m-%d", time.localtime(now - KEEP_DAYS * 86400))
    db.ex("DELETE FROM activity_days WHERE day < ?", (cutoff,))
