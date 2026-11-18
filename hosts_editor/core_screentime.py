"""Screen time for child accounts: allowed time slots and daily quotas, set from the web console.

Policy per child account (Windows SID):
    {"enabled": true,
     "days": [{"ranges": [["07:30", "21:00"]], "quota": 120}, ... 7 items, Monday first],
     "action": "lock" | "logoff",          what happens when time is up
     "bonus": {"day": "2026-11-18", "minutes": 30, "until": 1763500000},   extra time given today
     "pause_until": 0}                      "block now" from the console (timestamp, 0 = none)

- ranges: when the PC may be used that day ([] = not at all, [["00:00", "24:00"]] = all day);
- quota: minutes of use per day (None = no limit);
- bonus: extra minutes for today; if the child was outside the allowed slots when it was given,
  it also opens a window of that length ("until").

Usage is counted while the child's Windows session is open, unlocked and not idle for more than
IDLE_LIMIT seconds. Enforcement (warnings, then lock or log off) is done by the agent (SYSTEM)
through core_sessions; the decisions here are pure and unit-tested.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.screentime")

POLICY_FILE = os.path.join(PROGRAMDATA_DIR, "screentime_policy.json")
USAGE_FILE = os.path.join(PROGRAMDATA_DIR, "screentime_usage.json")
SID_RE = re.compile(r"^S-1-(5-21|12-1)(-\d{1,10}){4,6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")
WARN_AT = (10, 5, 1)          # minutes left: one message each
LOCK_GRACE = 60               # seconds between "time is up" and the lock
RELOCK_GRACE = 30             # child signs in again while not allowed
IDLE_LIMIT = 30 * 60          # no keyboard/mouse for 30 min: not counted (screen probably off)
MAX_TICK = 90                 # a longer gap (sleep, agent restart) is not counted as use
KEEP_DAYS = 14
DAY_NAMES = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

_lock = threading.RLock()


# --- policy -------------------------------------------------------------------------------------

def _minutes(text: str) -> int:
    m = TIME_RE.match(text or "")
    if not m:
        raise ValueError(f"heure invalide : {text!r} (format HH:MM)")
    value = int(m.group(1)) * 60 + int(m.group(2))
    if value > 24 * 60:
        raise ValueError(f"heure invalide : {text!r}")
    return value


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def clean_day(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("jour invalide")
    ranges = raw.get("ranges", [["00:00", "24:00"]])
    if not isinstance(ranges, list) or len(ranges) > 6:
        raise ValueError("6 plages au maximum par jour")
    spans = []
    for r in ranges:
        if not isinstance(r, (list, tuple)) or len(r) != 2:
            raise ValueError("plage invalide")
        start, end = _minutes(r[0]), _minutes(r[1])
        if start >= end:
            raise ValueError(f"plage {r[0]}–{r[1]} : le début doit précéder la fin")
        spans.append([start, end])
    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    quota = raw.get("quota")
    if quota is not None and (isinstance(quota, bool) or not isinstance(quota, int) or not 0 <= quota <= 1440):
        raise ValueError("durée quotidienne invalide (0 à 1440 minutes)")
    return {"ranges": [[_hhmm(s), _hhmm(e)] for s, e in merged], "quota": quota}


def clean_policy(raw) -> dict:
    """Validated copy of a policy sent by the console (ValueError with a French message)."""
    if not isinstance(raw, dict):
        raise ValueError("réglage invalide")
    days = raw.get("days")
    if not isinstance(days, list) or len(days) != 7:
        raise ValueError("il faut 7 jours (lundi → dimanche)")
    action = raw.get("action", "lock")
    if action not in ("lock", "logoff"):
        raise ValueError("action inconnue")
    return {"enabled": raw.get("enabled") is True, "days": [clean_day(d) for d in days], "action": action}


def default_policy() -> dict:
    return {"enabled": False, "days": [{"ranges": [["00:00", "24:00"]], "quota": None} for _ in range(7)],
            "action": "lock", "bonus": {}, "pause_until": 0}


def load_policies() -> dict:
    try:
        with open(POLICY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out = {}
    for sid, pol in (data.items() if isinstance(data, dict) else []):
        if not (isinstance(sid, str) and SID_RE.match(sid) and isinstance(pol, dict)):
            continue
        try:
            clean = clean_policy(pol)
        except ValueError:
            continue
        bonus = pol.get("bonus") if isinstance(pol.get("bonus"), dict) else {}
        pause = pol.get("pause_until") if isinstance(pol.get("pause_until"), (int, float)) else 0
        out[sid] = dict(clean, bonus=bonus, pause_until=pause)
    return out


def _save_policies(policies: dict) -> None:
    atomic_write_json(POLICY_FILE, policies)


def set_policy(sid: str, raw) -> dict:
    with _lock:
        policies = load_policies()
        current = policies.get(sid) or default_policy()
        policies[sid] = dict(current, **clean_policy(raw))
        _save_policies(policies)
        return policies[sid]


def add_bonus(sid: str, minutes: int, now: float = None) -> dict:
    """+minutes for today (negative: remove). Opens a window when the child is outside the slots."""
    now = time.time() if now is None else now
    with _lock:
        policies = load_policies()
        pol = policies.get(sid) or default_policy()
        today = _day_key(now)
        bonus = pol.get("bonus") if (pol.get("bonus") or {}).get("day") == today else {"day": today, "minutes": 0}
        total = max(-1440, min(1440, int(bonus.get("minutes", 0)) + minutes))
        bonus = {"day": today, "minutes": total, "until": bonus.get("until", 0)}
        if minutes > 0:
            ev = evaluate(dict(pol, bonus={}), usage_seconds(sid, now), datetime.fromtimestamp(now))
            if ev["reason"] in ("outside", "no_time_today") or bonus.get("until", 0) > now:
                bonus["until"] = max(now, bonus.get("until", 0)) + minutes * 60
        pol["bonus"] = bonus
        pol["pause_until"] = 0 if minutes > 0 else pol.get("pause_until", 0)
        policies[sid] = pol
        _save_policies(policies)
        return pol


def set_pause(sid: str, minutes: int, now: float = None) -> dict:
    """Block now: for `minutes`, -1 = until tomorrow 00:00, 0 = lift the pause."""
    now = time.time() if now is None else now
    with _lock:
        policies = load_policies()
        pol = policies.get(sid) or default_policy()
        if minutes == 0:
            pol["pause_until"] = 0
        elif minutes < 0:
            tomorrow = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            pol["pause_until"] = tomorrow.timestamp()
        else:
            pol["pause_until"] = now + minutes * 60
        if minutes != 0 and isinstance(pol.get("bonus"), dict):
            pol["bonus"] = dict(pol["bonus"], until=0)
        policies[sid] = pol
        _save_policies(policies)
        return pol


# --- decision (pure) ------------------------------------------------------------------------------

def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _next_start(policy: dict, now: datetime):
    """First allowed minute after `now` within the next 7 days: (datetime) or None."""
    minute = now.hour * 60 + now.minute
    for offset in range(0, 8):
        day = policy["days"][(now.weekday() + offset) % 7]
        if day.get("quota") == 0:
            continue
        for start, _end in ([_minutes(a), _minutes(b)] for a, b in day["ranges"]):
            if offset == 0 and start <= minute:
                continue
            base = (now + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
            return base + timedelta(minutes=start)
    return None


def describe_when(target, now: datetime) -> str:
    if target is None:
        return ""
    days = (target.date() - now.date()).days
    clock = target.strftime("%H:%M")
    if days == 0:
        return f"à {clock}"
    if days == 1:
        return f"demain à {clock}"
    return f"{DAY_NAMES[target.weekday()]} à {clock}"


def evaluate(policy: dict, used_seconds: float, now: datetime) -> dict:
    """Is the child allowed to use the PC right now, and for how long?

    Returns allowed, reason (None | paused | outside | no_time_today | quota), remaining_min (None =
    unlimited), until (HH:MM end of the current allowance), next_allowed (text), quota_min, used_min.
    """
    used_min = int(used_seconds // 60)
    out = {"allowed": True, "reason": None, "remaining_min": None, "until": "", "next_allowed": "",
           "quota_min": None, "used_min": used_min, "bonus_min": 0}
    if not policy or not policy.get("enabled"):
        return out
    ts = now.timestamp()
    day = policy["days"][now.weekday()]
    bonus = policy.get("bonus") or {}
    bonus_min = int(bonus.get("minutes", 0)) if bonus.get("day") == now.strftime("%Y-%m-%d") else 0
    window_end = bonus.get("until", 0) if bonus_min > 0 and bonus.get("until", 0) > ts else 0
    out["bonus_min"] = bonus_min
    quota = day.get("quota")
    if quota is not None:
        quota = max(0, quota + bonus_min)
        out["quota_min"] = quota

    pause = policy.get("pause_until") or 0
    if pause > ts:
        out.update(allowed=False, reason="paused", remaining_min=0,
                   next_allowed=describe_when(datetime.fromtimestamp(pause), now))
        return out

    minute = now.hour * 60 + now.minute + now.second / 60
    end = None
    for a, b in day["ranges"]:
        start, stop = _minutes(a), _minutes(b)
        if start <= minute < stop:
            end = stop
            break
    left_slot = None
    if end is not None:
        left_slot = end - minute
    if window_end:
        left_window = (window_end - ts) / 60
        left_slot = max(left_slot or 0, left_window)
    if left_slot is None:
        reason = "no_time_today" if not day["ranges"] or quota == 0 else "outside"
        out.update(allowed=False, reason=reason, remaining_min=0,
                   next_allowed=describe_when(_next_start(policy, now), now))
        return out

    left_quota = None if quota is None else quota - used_seconds / 60
    if left_quota is not None and left_quota <= 0:
        out.update(allowed=False, reason="quota", remaining_min=0,
                   next_allowed=describe_when(_next_start(dict(policy, days=[
                       d if i != now.weekday() else {"ranges": [], "quota": d.get("quota")}
                       for i, d in enumerate(policy["days"])]), now), now))
        return out
    remaining = left_slot if left_quota is None else min(left_slot, left_quota)
    full_day = end is not None and end >= 24 * 60 and not window_end and quota is None
    if full_day:
        # Allowed until midnight: is tomorrow free too? Then there is no limit to show.
        nxt = policy["days"][(now.weekday() + 1) % 7]
        if nxt["ranges"] and nxt["ranges"][0][0] == "00:00" and nxt.get("quota") is None:
            return out
    out["remaining_min"] = max(0, int(remaining))
    out["until"] = (now + timedelta(minutes=remaining)).strftime("%H:%M")
    return out


REASON_TEXT = {
    "paused": "Le PC a été bloqué depuis la console.",
    "outside": "Ce n'est pas une heure autorisée.",
    "no_time_today": "Pas d'ordinateur aujourd'hui.",
    "quota": "Le temps d'écran du jour est épuisé.",
}


def lock_message(ev: dict, action: str = "lock") -> str:
    when = f" Prochaine utilisation possible {ev['next_allowed']}." if ev.get("next_allowed") else ""
    verb = "va être fermée" if action == "logoff" else "va être verrouillée"
    return f"{REASON_TEXT.get(ev.get('reason'), 'Temps écoulé.')}{when}\n\nLa session {verb} dans une minute : " \
           f"enregistrez votre travail."


# --- usage store ----------------------------------------------------------------------------------

def _load_usage() -> dict:
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def usage_seconds(sid: str, now: float = None) -> float:
    now = time.time() if now is None else now
    day = _load_usage().get(_day_key(now)) or {}
    value = day.get(sid, 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def usage_history(sid: str, days: int = 7, now: float = None, data: dict = None) -> list:
    now = time.time() if now is None else now
    data = _load_usage() if data is None else data
    out = []
    for i in range(days - 1, -1, -1):
        key = _day_key(now - i * 86400)
        out.append({"day": key, "minutes": int(((data.get(key) or {}).get(sid, 0) or 0) // 60)})
    return out


class Enforcer:
    """Counts use and enforces the policies. tick() is called every few seconds by the agent with the
    open Windows sessions; it returns the actions to perform (messages, locks), so it can be tested
    without Windows."""

    def __init__(self):
        self._last_tick = None
        self._usage = _load_usage()
        self._dirty = False
        self._last_save = time.monotonic()
        self._warned = {}       # sid -> set of WARN_AT values already shown today (key: day)
        self._lock_at = {}      # session id -> timestamp of the planned lock
        self.events = []        # [{"t", "sid", "kind", "text"}] picked up by the activity journal

    def used(self, sid: str, now: float) -> float:
        return float((self._usage.get(_day_key(now)) or {}).get(sid, 0))

    def _add(self, sid: str, now: float, seconds: float) -> None:
        day = self._usage.setdefault(_day_key(now), {})
        day[sid] = float(day.get(sid, 0)) + seconds
        self._dirty = True

    def save(self, force: bool = False) -> None:
        if not self._dirty or (not force and time.monotonic() - self._last_save < 60):
            return
        cutoff = _day_key(time.time() - KEEP_DAYS * 86400)
        self._usage = {k: v for k, v in self._usage.items() if k >= cutoff}
        try:
            atomic_write_json(USAGE_FILE, self._usage)
            self._dirty = False
            self._last_save = time.monotonic()
        except Exception:
            logger.exception("screen time usage could not be saved")

    def tick(self, sessions: list, policies: dict, now: float = None) -> list:
        """sessions: [{"id", "sid", "name", "active", "locked", "idle"}]. Returns actions:
        ("message", session_id, title, text) | ("lock", session_id, action) and records events."""
        now = time.time() if now is None else now
        elapsed = 0.0 if self._last_tick is None else max(0.0, min(MAX_TICK, now - self._last_tick))
        self._last_tick = now
        actions = []
        today = _day_key(now)
        seen_sessions = set()
        for s in sessions:
            sid = s.get("sid")
            pol = policies.get(sid)
            if not pol or not pol.get("enabled"):
                continue
            usable = s.get("active") and not s.get("locked")
            idle = s.get("idle")
            if usable and (idle is None or idle < IDLE_LIMIT) and elapsed:
                self._add(sid, now, elapsed)
            ev = evaluate(pol, self.used(sid, now), datetime.fromtimestamp(now))
            sess = s.get("id")
            seen_sessions.add(sess)
            warned = self._warned.setdefault((sid, today), set())
            if ev["allowed"]:
                self._lock_at.pop(sess, None)
                left = ev["remaining_min"]
                if left is not None:
                    due = [w for w in WARN_AT if left <= w and w not in warned and left > 0]
                    if due and usable:
                        warned.update(w for w in WARN_AT if left <= w)
                        actions.append(("message", sess, "Temps d'écran",
                                        f"Il reste {left} minute{'s' if left > 1 else ''} d'ordinateur "
                                        f"(jusqu'à {ev['until']})."))
                    # time added again: warnings can be shown again
                    for w in list(warned):
                        if left > w + 1:
                            warned.discard(w)
                continue
            if not usable:
                self._lock_at.pop(sess, None)
                continue
            if sess not in self._lock_at:
                grace = RELOCK_GRACE if self._was_locked_recently(sid, now) else LOCK_GRACE
                self._lock_at[sess] = now + grace
                actions.append(("message", sess, "Temps d'écran", lock_message(ev, pol.get("action", "lock"))))
                self.events.append({"t": now, "sid": sid, "kind": "time_up", "reason": ev["reason"],
                                    "text": REASON_TEXT.get(ev["reason"], "Temps écoulé")})
            elif now >= self._lock_at[sess]:
                self._lock_at[sess] = now + RELOCK_GRACE + LOCK_GRACE
                actions.append(("lock", sess, pol.get("action", "lock")))
                self.events.append({"t": now, "sid": sid, "kind": "locked", "reason": ev["reason"],
                                    "text": "Session fermée" if pol.get("action") == "logoff" else "Session verrouillée"})
        for sess in list(self._lock_at):
            if sess not in seen_sessions:
                del self._lock_at[sess]
        for key in [k for k in self._warned if k[1] != today]:
            del self._warned[key]
        self.events = self.events[-200:]
        self.save()
        return actions

    def _was_locked_recently(self, sid: str, now: float) -> bool:
        return any(e["sid"] == sid and e["kind"] == "locked" and now - e["t"] < 6 * 3600 for e in self.events)


def status(children: list, enforcer: "Enforcer" = None, now: float = None) -> list:
    """State section for the console: one item per child account [{sid, name, policy, today...}]."""
    now = time.time() if now is None else now
    policies = load_policies()
    out = []
    for child in children:
        sid = child["sid"]
        pol = policies.get(sid) or default_policy()
        used = enforcer.used(sid, now) if enforcer else usage_seconds(sid, now)
        ev = evaluate(pol, used, datetime.fromtimestamp(now))
        out.append({"sid": sid, "name": child.get("name") or sid, "policy": pol, "now": ev,
                    "history": usage_history(sid, 7, now, enforcer._usage if enforcer else None)})
    return out
