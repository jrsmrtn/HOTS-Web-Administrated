"""SafeSearch / YouTube restricted mode / Bing strict, enforced through the hosts file.

Works for every browser and app, including those that ignore browser policies: the search domains
are pointed to the "safe" addresses published by Google and Microsoft (forcesafesearch.google.com,
restrict(moderate).youtube.com, strict.bing.com). Their IPs are resolved through Cloudflare Family
and refreshed by the agent; if a resolution fails, the previous IPs are kept (never a guessed IP).
If the YouTube category is blocked, YouTube is left blocked (never "un-blocked" to restricted).
"""

import json
import logging
import os
import random
import socket
import struct

from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.safesearch")

STATE_FILE = os.path.join(PROGRAMDATA_DIR, "safesearch_state.json")
TAG = "safesearch.txt"
YOUTUBE_LEVELS = ("off", "moderate", "strict")

GOOGLE_TLDS = ("com", "fr", "be", "ch", "ca", "lu", "de", "es", "it", "pt", "nl", "at", "pl", "co.uk", "ie",
               "com.br", "com.mx", "ma", "dz", "tn", "sn", "ci", "cm", "mu", "ht", "re", "gp", "mq", "ru", "com.tr")
GOOGLE_HOSTS = tuple(f"www.google.{t}" for t in GOOGLE_TLDS) + tuple(f"google.{t}" for t in GOOGLE_TLDS)
YOUTUBE_HOSTS = ("www.youtube.com", "m.youtube.com", "youtubei.googleapis.com", "youtube.googleapis.com",
                 "www.youtube-nocookie.com")
BING_HOSTS = ("www.bing.com", "bing.com")
TARGETS = {"google": "forcesafesearch.google.com", "youtube_strict": "restrict.youtube.com",
           "youtube_moderate": "restrictmoderate.youtube.com", "bing": "strict.bing.com"}


def clean_options(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    yt = raw.get("youtube") if raw.get("youtube") in YOUTUBE_LEVELS else "off"
    return {"google": raw.get("google") is True, "youtube": yt, "bing": raw.get("bing") is True}


def build_mapping(opts: dict, ips: dict, youtube_blocked: bool = False) -> dict:
    """Pure: {domain: ip} for the chosen options. `ips`: {target key: ip}."""
    mapping = {}
    if opts.get("google") and ips.get("google"):
        mapping.update({h: ips["google"] for h in GOOGLE_HOSTS})
    yt = opts.get("youtube", "off")
    key = {"strict": "youtube_strict", "moderate": "youtube_moderate"}.get(yt)
    if key and ips.get(key) and not youtube_blocked:
        mapping.update({h: ips[key] for h in YOUTUBE_HOSTS})
    if opts.get("bing") and ips.get("bing"):
        mapping.update({h: ips["bing"] for h in BING_HOSTS})
    return mapping


def resolve_a(name: str, server: str = "1.1.1.3", timeout: float = 3.0) -> list:
    """Minimal DNS A query (follows the answer section, ignores CNAME records)."""
    tid = random.randint(0, 65535)
    query = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for label in name.rstrip(".").split("."):
        query += bytes([len(label)]) + label.encode("ascii")
    query += b"\x00" + struct.pack(">HH", 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(query, (server, 53))
            resp, _ = sock.recvfrom(2048)
    except OSError:
        return []
    return parse_a_answers(resp, tid)


def _skip_name(data: bytes, pos: int) -> int:
    while pos < len(data):
        length = data[pos]
        if length == 0:
            return pos + 1
        if length & 0xC0 == 0xC0:
            return pos + 2
        pos += 1 + length
    return pos


def parse_a_answers(resp: bytes, tid: int = None) -> list:
    if len(resp) < 12:
        return []
    rid, flags, qd, an = struct.unpack(">HHHH", resp[:8])
    if (tid is not None and rid != tid) or flags & 0x000F:
        return []
    pos = 12
    for _ in range(qd):
        pos = _skip_name(resp, pos) + 4
    ips = []
    for _ in range(an):
        pos = _skip_name(resp, pos)
        if pos + 10 > len(resp):
            break
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", resp[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(resp[pos:pos + 4]))
        pos += rdlen
    return ips


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _youtube_blocked() -> bool:
    from .core import is_parental_active
    return is_parental_active("youtube.txt")


class SafeSearchManager:
    last_error: str = ""

    @staticmethod
    def current() -> dict:
        state = _load_state()
        out = clean_options(state.get("options"))
        out["ips"] = state.get("ips") or {}
        return out

    @staticmethod
    def _resolve(opts: dict, previous: dict) -> dict:
        ips = dict(previous or {})
        wanted = []
        if opts["google"]:
            wanted.append("google")
        if opts["youtube"] != "off":
            wanted.append("youtube_" + opts["youtube"])
        if opts["bing"]:
            wanted.append("bing")
        for key in wanted:
            found = resolve_a(TARGETS[key]) or resolve_a(TARGETS[key], "1.0.0.3")
            if found:
                ips[key] = sorted(found)[0]
        return ips

    @staticmethod
    def apply(raw_opts) -> bool:
        """Write the hosts block (raises HostsLockError if the hosts file is locked: the caller unlocks)."""
        from .core import toggle_parental_control
        SafeSearchManager.last_error = ""
        opts = clean_options(raw_opts)
        state = _load_state()
        ips = SafeSearchManager._resolve(opts, state.get("ips"))
        mapping = build_mapping(opts, ips, _youtube_blocked())
        missing = [k for k in ("google", "bing") if opts[k] and not ips.get(k)]
        if opts["youtube"] != "off" and not ips.get("youtube_" + opts["youtube"]):
            missing.append("youtube")
        if missing:
            SafeSearchManager.last_error = ("Adresses SafeSearch introuvables (" + ", ".join(missing) +
                                            ") : le PC n'arrive pas à joindre le DNS familial.")
            return False
        ok = toggle_parental_control(bool(mapping), tag_suffix=TAG, comment="SafeSearch", mapping=mapping or None)
        if ok:
            atomic_write_json(STATE_FILE, {"options": opts, "ips": ips})
        return ok

    @staticmethod
    def refresh() -> bool:
        """Re-resolve the safe IPs (and the YouTube category state); rewrite only if needed."""
        cur = SafeSearchManager.current()
        opts = {k: cur[k] for k in ("google", "youtube", "bing")}
        if not (opts["google"] or opts["bing"] or opts["youtube"] != "off"):
            return False
        ips = SafeSearchManager._resolve(opts, cur["ips"])
        state = _load_state()
        if ips == cur["ips"] and state.get("youtube_blocked") == _youtube_blocked():
            return False
        result = SafeSearchManager.apply(opts)
        state = _load_state()
        state["youtube_blocked"] = _youtube_blocked()
        atomic_write_json(STATE_FILE, state)
        return result
