"""Activity journal of child accounts, shown in the web console ("Activité" tab).

What is recorded, per child account and per day (nothing for the other accounts):
  - screen time (from core_screentime) and use per hour;
  - applications in the foreground and for how long, from a small helper started by the agent in the
    child's session (`--agent-user-helper`: foreground window, title, keyboard/mouse idle time);
  - web sites: the DNS lookups of the browsers, read from the Windows "DNS Client" event log, grouped
    by site, with an estimated time (minutes during which the site was used while a browser was in
    the foreground) and the result: allowed, or blocked (by which list, or by the family DNS filter);
  - blocked programs (anti-portable mode, AppLocker) and screen-time events (time up, lock);
  - optionally the titles of the windows (page titles in browsers), with their time.

The helper runs with the child's rights: closing it only stops the journal for a few seconds (the
agent restarts it and notes it). The agent sends the day summaries to the console every 5 minutes.

Pure parts (site grouping, DNS event parsing, aggregation) are unit-tested on Linux.
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime

from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.activity")

SETTINGS_FILE = os.path.join(PROGRAMDATA_DIR, "activity_settings.json")
DATA_DIR = os.path.join(PROGRAMDATA_DIR, "activity")
DNS_LOG = "Microsoft-Windows-DNS-Client/Operational"
DNS_LOG_SIZE = 32 * 1024 * 1024
SAMPLE_EVERY = 5            # seconds between two helper samples
MAX_SAMPLE_GAP = 15         # a longer gap between samples is not counted
IDLE_LIMIT = 5 * 60         # no input for 5 min: not "in use" (unless a video/game keeps the screen on)
KEEP_DAYS = 31
MAX_APPS, MAX_SITES, MAX_TITLES, MAX_EVENTS = 150, 300, 200, 200
_CREATE_NO_WINDOW = 0x08000000
_SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
WEVTUTIL = os.path.join(_SYSTEM32, "wevtutil.exe")

BROWSERS = {
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi", "opera.exe": "Opera", "iexplore.exe": "Internet Explorer", "chromium.exe": "Chromium",
    "librewolf.exe": "LibreWolf", "waterfox.exe": "Waterfox", "yandex.exe": "Yandex", "tor.exe": "Tor",
    "zen.exe": "Zen", "thorium.exe": "Thorium", "floorp.exe": "Floorp", "arc.exe": "Arc",
}
KNOWN_APPS = {
    "explorer.exe": "Explorateur de fichiers", "applicationframehost.exe": None, "winword.exe": "Word",
    "excel.exe": "Excel", "powerpnt.exe": "PowerPoint", "onenote.exe": "OneNote", "teams.exe": "Teams",
    "ms-teams.exe": "Teams", "discord.exe": "Discord", "steam.exe": "Steam", "steamwebhelper.exe": "Steam",
    "epicgameslauncher.exe": "Epic Games", "minecraft.exe": "Minecraft", "javaw.exe": "Java (Minecraft…)",
    "robloxplayerbeta.exe": "Roblox", "fortniteclient-win64-shipping.exe": "Fortnite", "spotify.exe": "Spotify",
    "vlc.exe": "VLC", "whatsapp.exe": "WhatsApp", "telegram.exe": "Telegram", "notepad.exe": "Bloc-notes",
    "code.exe": "Visual Studio Code", "acrord32.exe": "Adobe Reader", "lockapp.exe": None,
    "searchhost.exe": "Recherche Windows", "shellexperiencehost.exe": None, "startmenuexperiencehost.exe": None,
    "systemsettings.exe": "Paramètres", "taskmgr.exe": "Gestionnaire des tâches", "snippingtool.exe": "Capture",
}
# Domains that only serve content for another site (videos, images, scripts): counted for that site.
SITE_ALIASES = {
    "googlevideo.com": "youtube.com", "ytimg.com": "youtube.com", "youtu.be": "youtube.com",
    "youtube-nocookie.com": "youtube.com", "ggpht.com": "youtube.com",
    "fbcdn.net": "facebook.com", "fbsbx.com": "facebook.com", "facebook.net": "facebook.com",
    "cdninstagram.com": "instagram.com", "tiktokcdn.com": "tiktok.com", "tiktokv.com": "tiktok.com",
    "tiktokcdn-eu.com": "tiktok.com", "ttwstatic.com": "tiktok.com", "ibytedtos.com": "tiktok.com",
    "byteoversea.com": "tiktok.com", "musical.ly": "tiktok.com", "twimg.com": "x.com", "twitter.com": "x.com",
    "t.co": "x.com", "redd.it": "reddit.com", "redditmedia.com": "reddit.com", "redditstatic.com": "reddit.com",
    "jtvnw.net": "twitch.tv", "twitchcdn.net": "twitch.tv", "ttvnw.net": "twitch.tv", "pinimg.com": "pinterest.com",
    "sc-cdn.net": "snapchat.com", "snapkit.com": "snapchat.com", "discordapp.com": "discord.com",
    "discordapp.net": "discord.com", "discord.gg": "discord.com", "nflxvideo.net": "netflix.com",
    "nflximg.net": "netflix.com", "nflxext.com": "netflix.com", "nflxso.net": "netflix.com",
    "whatsapp.net": "whatsapp.com", "wa.me": "whatsapp.com", "roblox.com": "roblox.com", "rbxcdn.com": "roblox.com",
    "wikimedia.org": "wikipedia.org", "spotifycdn.com": "spotify.com", "scdn.co": "spotify.com",
}
# Infrastructure / ads / analytics: never shown as "visited sites".
NOISE_SITES = {
    "gstatic.com", "googleapis.com", "googleusercontent.com", "googletagmanager.com", "google-analytics.com",
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "gvt1.com", "gvt2.com", "1e100.net",
    "akamaihd.net", "akamaized.net", "akamai.net", "akamaiedge.net", "edgekey.net", "edgesuite.net",
    "cloudfront.net", "cloudflare.com", "cloudflare.net", "cloudflareinsights.com", "fastly.net", "fastly.com",
    "jsdelivr.net", "unpkg.com", "cdnjs.com", "bootstrapcdn.com", "azureedge.net", "msedge.net", "trafficmanager.net",
    "microsoft.com", "msn.com", "live.com", "office.net", "windows.com", "windowsupdate.com", "bing.net",
    "microsoftonline.com", "msftconnecttest.com", "msftncsi.com", "digicert.com", "lencr.org", "letsencrypt.org",
    "pki.goog", "sectigo.com", "globalsign.com", "mozilla.net", "mozilla.org", "mozilla.com", "firefox.com",
    "brave.com", "bravesoftware.com", "opera.com", "scorecardresearch.com", "criteo.com", "criteo.net",
    "adnxs.com", "taboola.com", "outbrain.com", "hotjar.com", "segment.io", "sentry.io", "newrelic.com",
    "amazon-adsystem.com", "adsrvr.org", "rubiconproject.com", "pubmatic.com", "casalemedia.com", "quantserve.com",
    "facebook.net", "branch.io", "appsflyer.com", "onetrust.com", "cookielaw.org", "didomi.io", "consensu.org",
    "local", "localhost", "arpa", "lan", "home", "internal", "localdomain",
}
TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "co.nz", "co.jp", "co.kr", "com.br", "com.cn",
    "com.tr", "co.in", "com.mx", "com.ar", "co.za", "gouv.fr", "asso.fr", "com.fr", "ac.be", "co.il", "com.tw",
}
BLOCK_LABELS = {
    "TWITTER": "Twitter / X", "INSTAGRAM": "Instagram", "YOUTUBE": "YouTube", "FACEBOOK": "Facebook",
    "WHATSAPP": "WhatsApp", "TIKTOK": "TikTok", "TWITCH": "Twitch", "SNAPCHAT": "Snapchat", "PINTEREST": "Pinterest",
    "REDDIT": "Reddit", "ADULT": "Contenu adulte", "GAMES": "Jeux", "TORRENT": "Torrents", "DATING": "Rencontres",
    "RANDOM_CHAT": "Chat vidéo aléatoire", "CUSTOM_DOMAINS": "Liste personnalisée", "DOH_BYPASS": "DNS chiffré",
}
FAMILY_FILTER = "Filtre familial (DNS)"
_DOMAIN_RE = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,62}\.)*[a-z0-9_-]{1,63}$")
_ip_like = re.compile(r"^[\d.]+$|:")

_lock = threading.RLock()


# --- settings -----------------------------------------------------------------------------------------

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"enabled": data.get("enabled") is True, "titles": data.get("titles", True) is not False,
            "originals": data.get("originals") if isinstance(data.get("originals"), dict) else {}}


def _save_settings(data: dict) -> None:
    atomic_write_json(SETTINGS_FILE, data)


# --- pure helpers -------------------------------------------------------------------------------------

def site_of(domain: str) -> str:
    """'www.m.youtube.com' -> 'youtube.com', 'r3---sn-a.googlevideo.com' -> 'youtube.com',
    'ent.ac-paris.fr'-> 'ent.ac-paris.fr' (registrable part). '' for things that are not sites."""
    d = (domain or "").strip().lower().rstrip(".")
    if not d or _ip_like.search(d) or "." not in d or not _DOMAIN_RE.match(d):
        return ""
    labels = d.split(".")
    if labels[-1] in NOISE_SITES:
        return ""
    base = ".".join(labels[-2:])
    if base in TWO_LEVEL_SUFFIXES and len(labels) >= 3:
        base = ".".join(labels[-3:])
    base = SITE_ALIASES.get(base, base)
    return "" if base in NOISE_SITES else base


def app_name(exe_path: str, description: str = "") -> str:
    exe = os.path.basename((exe_path or "").replace("\\", "/")).lower()
    if exe in BROWSERS:
        return BROWSERS[exe]
    if exe in KNOWN_APPS and KNOWN_APPS[exe]:
        return KNOWN_APPS[exe]
    if description:
        return description.strip()[:80]
    return os.path.basename((exe_path or "").replace("\\", "/"))[:80] or "?"


_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


def parse_dns_events(xml_text: str) -> list:
    """Events 3008 ("DNS query is completed") from `wevtutil qe /f:xml`, oldest first:
    [{"rid", "pid", "t", "name", "status", "results"}]."""
    text = (xml_text or "").strip()
    if not text:
        return []
    try:
        root = ET.fromstring("<Events>" + text + "</Events>")
    except ET.ParseError:
        return []
    out = []
    for ev in root.findall(f"{_NS}Event"):
        system = ev.find(f"{_NS}System")
        if system is None:
            continue
        try:
            rid = int(system.findtext(f"{_NS}EventRecordID") or 0)
            pid = int((system.find(f"{_NS}Execution").get("ProcessID")) or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        created = system.find(f"{_NS}TimeCreated")
        ts = _parse_time(created.get("SystemTime") if created is not None else "")
        data = {d.get("Name"): (d.text or "") for d in ev.iter(f"{_NS}Data")}
        name = data.get("QueryName", "").strip().lower().rstrip(".")
        if not name:
            continue
        try:
            status = int(data.get("QueryStatus") or 0)
        except ValueError:
            status = 0
        out.append({"rid": rid, "pid": pid, "t": ts, "name": name, "status": status,
                    "results": data.get("QueryResults", "")})
    out.sort(key=lambda e: e["rid"])
    return out


def _parse_time(text: str) -> float:
    text = (text or "").strip()
    if not text:
        return time.time()
    text = text.replace("Z", "+00:00")
    if "." in text:  # 2026-11-18T10:00:00.1234567+00:00: Python accepts 6 digits at most
        head, rest = text.split(".", 1)
        digits = re.match(r"\d+", rest)
        frac = digits.group(0)[:6] if digits else "0"
        text = f"{head}.{frac}{rest[len(digits.group(0)) if digits else 0:]}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return time.time()


def answer_blocked(results: str, status: int) -> bool:
    """0.0.0.0 / :: answers are blocks (hosts file or the family DNS filter)."""
    ips = [x.strip() for x in re.split(r"[;\s]+", results or "") if x.strip()]
    ips = [x.replace("::ffff:", "") for x in ips if not x.startswith("type:")]
    return bool(ips) and all(x in ("0.0.0.0", "::", "0:0:0:0:0:0:0:0") for x in ips)


def hosts_block_index(hosts_text: str) -> dict:
    """{domain: label} of every 0.0.0.0 entry of the hosts file. Label "" = a block that is not about
    the child (telemetry list of the privacy module...): such lookups are not reported."""
    index = {}
    current = None
    for line in hosts_text.splitlines():
        s = line.strip()
        m = re.match(r"# === HOSTS_EDITOR_PARENTAL_(.+?)_(START|END) ===", s)
        if m:
            current = m.group(1) if m.group(2) == "START" else None
            continue
        if not s or s.startswith("#"):
            continue
        parts = s.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1", "::", "::1"):
            label = BLOCK_LABELS.get(current, "") if current else "Fichier hosts"
            for host in parts[1:]:
                index.setdefault(host.lower(), label)
    return index


def block_label(index: dict, name: str):
    """Label of the hosts block covering `name` (or a parent domain), None if not in the hosts file."""
    parts = name.split(".")
    for i in range(0, len(parts) - 1):
        label = index.get(".".join(parts[i:]))
        if label is not None:
            return label
    return None


def _empty_day(day: str, sid: str, name: str) -> dict:
    return {"day": day, "sid": sid, "user": name, "screen_s": 0, "active_s": 0, "hours": [0] * 24,
            "apps": {}, "sites": {}, "titles": {}, "blocked_apps": {}, "events": [],
            "helper": {"restarts": 0, "last_seen": 0}}


class Journal:
    """Day logs per child, fed by helper samples, DNS events, AppLocker events and screen-time events."""

    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        self.days = {}            # (day, sid) -> dict
        self.dirty = set()        # (day, sid) changed since the last send
        self._fg = {}             # (day, sid) -> set of minute-of-day where a browser was in front
        self._last_sample = {}    # session id -> (t, sid)
        self.names = {}           # sid -> account name
        self._lock = threading.RLock()

    # storage
    def _path(self, day: str, sid: str) -> str:
        return os.path.join(self.data_dir, f"{day}_{sid}.json")

    def get(self, day: str, sid: str) -> dict:
        key = (day, sid)
        log = self.days.get(key)
        if log is None:
            log = None
            try:
                with open(self._path(day, sid), "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and loaded.get("day") == day:
                    log = loaded
            except Exception:
                pass
            log = log or _empty_day(day, sid, self.names.get(sid, ""))
            self._fg[key] = set(log.pop("_fg", []))
            self.days[key] = log
        if self.names.get(sid):
            log["user"] = self.names[sid]
        return log

    def save(self) -> None:
        with self._lock:
            os.makedirs(self.data_dir, exist_ok=True)
            today = datetime.now().strftime("%Y-%m-%d")
            for (day, sid), log in list(self.days.items()):
                data = dict(log, _fg=sorted(self._fg.get((day, sid), ())))
                try:
                    atomic_write_json(self._path(day, sid), data)
                except Exception:
                    logger.exception("activity log could not be saved")
                if day < today:  # yesterday is finished: keep it on disk only
                    self.days.pop((day, sid), None)
                    self._fg.pop((day, sid), None)
            cutoff = datetime.fromtimestamp(time.time() - KEEP_DAYS * 86400).strftime("%Y-%m-%d")
            try:
                for f in os.listdir(self.data_dir):
                    if f[:10] < cutoff and f.endswith(".json"):
                        os.remove(os.path.join(self.data_dir, f))
            except OSError:
                pass

    # inputs
    def add_sample(self, session_id: int, sid: str, sample: dict, titles: bool = True) -> None:
        """One helper sample: {"t", "exe", "title", "idle", "desc"?}. The time since the previous
        sample of the session is credited to the foreground application."""
        t = float(sample.get("t") or time.time())
        with self._lock:
            prev = self._last_sample.get(session_id)
            self._last_sample[session_id] = (t, sid)
            if not prev or prev[1] != sid:
                return
            gap = t - prev[0]
            if gap <= 0 or gap > MAX_SAMPLE_GAP:
                return
            idle = sample.get("idle")
            exe_path = str(sample.get("exe") or "")[:300]
            exe = os.path.basename(exe_path.replace("\\", "/")).lower()
            title = str(sample.get("title") or "")[:200]
            media = bool(sample.get("fullscreen"))
            if isinstance(idle, (int, float)) and idle >= IDLE_LIMIT and not media:
                return
            when = datetime.fromtimestamp(t)
            day = when.strftime("%Y-%m-%d")
            log = self.get(day, sid)
            key = (day, sid)
            log["active_s"] += gap
            log["hours"][when.hour] += gap
            log["helper"]["last_seen"] = t
            if not exe or KNOWN_APPS.get(exe, "") is None and exe != "applicationframehost.exe":
                self.dirty.add(key)
                return
            if exe == "applicationframehost.exe":  # Store apps: the window title is the app name
                app_key, name = "store:" + title.lower()[:60], title[:80] or "Application du Store"
            else:
                app_key, name = exe, app_name(exe_path, str(sample.get("desc") or ""))
            app = log["apps"].get(app_key)
            if app is None:
                if len(log["apps"]) >= MAX_APPS:
                    self.dirty.add(key)
                    return
                app = log["apps"][app_key] = {"name": name, "exe": exe, "path": exe_path, "s": 0, "first": t, "last": t,
                                              "browser": exe in BROWSERS}
            app["s"] += gap
            app["last"] = t
            if exe in BROWSERS:
                self._fg.setdefault(key, set()).add(when.hour * 60 + when.minute)
            if titles and title:
                clean = re.sub(r"\s+", " ", title).strip()
                for suffix in (" - Google Chrome", " - Microsoft​ Edge", " - Microsoft Edge", " — Mozilla Firefox",
                               " - Mozilla Firefox", " - Brave", " - Vivaldi", " - Opera"):
                    if clean.endswith(suffix):
                        clean = clean[: -len(suffix)]
                        break
                tkey = f"{exe}|{clean.lower()[:120]}"
                entry = log["titles"].get(tkey)
                if entry is None and len(log["titles"]) < MAX_TITLES:
                    entry = log["titles"][tkey] = {"title": clean[:160], "app": name, "s": 0, "last": t}
                if entry is not None:
                    entry["s"] += gap
                    entry["last"] = t
            self.dirty.add(key)

    def add_dns(self, sid: str, event: dict, exe: str, block_index: dict) -> None:
        """One DNS lookup of the child (event from parse_dns_events, exe = process name)."""
        name = event["name"]
        site = site_of(name)
        if not site:
            return
        exe = (exe or "").lower()
        is_browser = exe in BROWSERS
        blocked = answer_blocked(event.get("results", ""), event.get("status", 0))
        by = ""
        if blocked:
            # In the hosts file: which list. Elsewhere: the family DNS filter answered 0.0.0.0.
            by = block_label(block_index, name)
            if by is None:
                by = FAMILY_FILTER
            if not by:
                return  # e.g. telemetry blocked by the privacy module: not the child's doing
            if by == "DNS chiffré" and not is_browser:
                return
        elif not is_browser:
            return  # background traffic of other programs is not "visiting a site"
        t = event.get("t") or time.time()
        when = datetime.fromtimestamp(t)
        day = when.strftime("%Y-%m-%d")
        with self._lock:
            log = self.get(day, sid)
            key = (day, sid)
            entry = log["sites"].get(site)
            if entry is None:
                if len(log["sites"]) >= MAX_SITES:
                    return
                entry = log["sites"][site] = {"site": site, "n": 0, "blocked": 0, "by": "", "s": 0, "first": t,
                                              "last": t, "_m": []}
            minute = when.hour * 60 + when.minute
            entry["last"] = max(entry["last"], t)
            if blocked:
                # One attempt per site and per minute (a page asks the same name several times).
                if entry.get("_bm") != minute:
                    entry["blocked"] += 1
                    entry["_bm"] = minute
                entry["by"] = by
            else:
                entry["n"] += 1
                minutes = entry.setdefault("_m", [])
                fg = self._fg.get(key)
                if minute not in minutes and (not fg or minute in fg or (minute - 1) in fg):
                    minutes.append(minute)
                    entry["s"] = 60 * len(minutes)
            self.dirty.add(key)

    def add_blocked_program(self, sid: str, path: str, t: float, by: str = "Mode anti-portable") -> None:
        when = datetime.fromtimestamp(t)
        day = when.strftime("%Y-%m-%d")
        with self._lock:
            log = self.get(day, sid)
            exe = os.path.basename(path.replace("\\", "/")).lower() or "?"
            entry = log["blocked_apps"].get(exe)
            if entry is None:
                entry = log["blocked_apps"][exe] = {"name": app_name(path), "path": path[:300], "n": 0, "last": t,
                                                    "by": by}
            entry["n"] += 1
            entry["last"] = max(entry["last"], t)
            self.dirty.add((day, sid))

    def add_event(self, sid: str, t: float, kind: str, text: str) -> None:
        day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
        with self._lock:
            log = self.get(day, sid)
            log["events"].append({"t": t, "kind": kind, "text": text[:200]})
            log["events"] = log["events"][-MAX_EVENTS:]
            self.dirty.add((day, sid))

    def set_screen(self, sid: str, day: str, seconds: float) -> None:
        with self._lock:
            log = self.get(day, sid)
            if abs(log["screen_s"] - seconds) >= 30:
                log["screen_s"] = int(seconds)
                self.dirty.add((day, sid))

    # output
    @staticmethod
    def summary(log: dict) -> dict:
        """What the console receives: lists sorted by time, internal fields removed, rounded."""
        def strip(d):
            return {k: (round(v) if isinstance(v, float) else v) for k, v in d.items() if not k.startswith("_")}
        sites = sorted((strip(s) for s in log["sites"].values()), key=lambda s: (-s["s"], -s["blocked"], -s["n"]))
        return {
            "day": log["day"], "sid": log["sid"], "user": log.get("user") or "",
            "screen_s": int(log.get("screen_s") or 0), "active_s": int(log.get("active_s") or 0),
            "hours": [int(h) for h in log["hours"]],
            "apps": sorted((strip(a) for a in log["apps"].values()), key=lambda a: -a["s"])[:MAX_APPS],
            "sites": sites[:MAX_SITES],
            "titles": sorted((strip(x) for x in log["titles"].values()), key=lambda x: -x["s"])[:100],
            "blocked_apps": sorted((strip(b) for b in log["blocked_apps"].values()), key=lambda b: -b["last"]),
            "events": log["events"][-100:],
            "helper": dict(log.get("helper") or {}),
        }

    def outbox(self, limit: int = 6) -> list:
        with self._lock:
            keys = sorted(self.dirty)[-limit:]
            return [self.summary(self.get(day, sid)) for day, sid in keys]

    def ack(self, sent: list) -> None:
        with self._lock:
            for item in sent:
                self.dirty.discard((item["day"], item["sid"]))


# --- helper (runs in the child's session, with the child's rights) ---------------------------------------

def run_user_helper() -> int:  # pragma: no cover - Windows only
    """`--agent-user-helper`: every SAMPLE_EVERY s, write one JSON line on stdout (a pipe read by the
    agent): foreground program, window title, seconds without keyboard/mouse, full screen."""
    if sys.platform != "win32":
        return 1
    import ctypes
    from ctypes import wintypes
    user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                              ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    out = k32.GetStdHandle(wintypes.DWORD(-11 & 0xFFFFFFFF))
    if not out or out == wintypes.HANDLE(-1).value:
        return 1

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    user32.GetForegroundWindow.restype = wintypes.HWND
    descriptions = {}
    while True:
        hwnd = user32.GetForegroundWindow()
        exe, title, fullscreen = "", "", False
        if hwnd:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                size = wintypes.DWORD(1024)
                path = ctypes.create_unicode_buffer(1024)
                if k32.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                    exe = path.value
                k32.CloseHandle(h)
            r, screen = RECT(), (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
            if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                fullscreen = r.left <= 0 and r.top <= 0 and r.right >= screen[0] and r.bottom >= screen[1]
        lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        idle = 0
        if user32.GetLastInputInfo(ctypes.byref(lii)):
            idle = max(0, (k32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) // 1000
        if exe and exe not in descriptions:
            descriptions[exe] = _file_description(exe)
        line = json.dumps({"t": time.time(), "exe": exe, "title": title, "idle": idle, "fullscreen": fullscreen,
                           "desc": descriptions.get(exe, "")}, ensure_ascii=True) + "\n"
        data = line.encode("ascii")
        written = wintypes.DWORD()
        if not k32.WriteFile(out, data, len(data), ctypes.byref(written), None):
            return 0  # the agent is gone: stop
        time.sleep(SAMPLE_EVERY)


def _file_description(path: str) -> str:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(length)) \
                or length.value < 4:
            return ""
        lang, cp = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * 2)).contents
        key = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
        if ver.VerQueryValueW(buf, key, ctypes.byref(ptr), ctypes.byref(length)) and length.value:
            return ctypes.wstring_at(ptr, length.value).rstrip("\x00").strip()[:80]
    except Exception:
        pass
    return ""


# --- DNS Client log (Windows) ---------------------------------------------------------------------------------

def _wevtutil(args: list, timeout: int = 60):
    try:
        return subprocess.run([WEVTUTIL] + args, capture_output=True, text=True, errors="replace", timeout=timeout,
                              creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except Exception:
        return None


def dns_log_enabled():
    r = _wevtutil(["gl", DNS_LOG])
    if r is None or r.returncode != 0:
        return None
    m = re.search(r"^enabled:\s*(\w+)", r.stdout or "", re.MULTILINE | re.IGNORECASE)
    return bool(m and m.group(1).lower() == "true")


def set_dns_log(enabled: bool) -> bool:
    args = ["sl", DNS_LOG, f"/e:{'true' if enabled else 'false'}"]
    if enabled:
        args.append(f"/ms:{DNS_LOG_SIZE}")
    r = _wevtutil(args)
    return r is not None and r.returncode == 0


def last_dns_record() -> int:
    r = _wevtutil(["qe", DNS_LOG, "/c:1", "/rd:true", "/f:xml"])
    events = parse_dns_events(r.stdout if r is not None else "")
    if events:
        return events[-1]["rid"]
    m = re.search(r"<EventRecordID>(\d+)</EventRecordID>", (r.stdout if r is not None else "") or "")
    return int(m.group(1)) if m else 0


def read_dns_events(after_rid: int, limit: int = 3000) -> list:
    query = f"*[System[(EventID=3008) and (EventRecordID>{int(after_rid)})]]"
    r = _wevtutil(["qe", DNS_LOG, f"/q:{query}", f"/c:{limit}", "/f:xml"], timeout=120)
    if r is None or r.returncode != 0:
        return []
    return parse_dns_events(r.stdout)


# Chromium uses its own DNS client unless told otherwise: lookups would not reach the Windows log.
_CHROMIUM_POLICY_KEYS = (r"SOFTWARE\Policies\Google\Chrome", r"SOFTWARE\Policies\Microsoft\Edge",
                         r"SOFTWARE\Policies\BraveSoftware\Brave")


def enable(titles: bool = True) -> bool:
    """Turn the journal on: DNS Client log on, Chromium browsers use the Windows resolver."""
    from .core_browser_policy import REG_DWORD, reg_read, reg_write
    with _lock:
        data = load_settings()
        originals = data["originals"]
        if "dns_log" not in originals:
            originals["dns_log"] = dns_log_enabled()
        ok = set_dns_log(True)
        for path in _CHROMIUM_POLICY_KEYS:
            key = f"{path}|BuiltInDnsClientEnabled"
            if key not in originals:
                exists, vtype, value = reg_read(path, "BuiltInDnsClientEnabled")
                originals[key] = {"exists": exists, "type": vtype, "data": value}
            try:
                reg_write(path, "BuiltInDnsClientEnabled", REG_DWORD, 0)
            except OSError:
                ok = False
        _save_settings({"enabled": True, "titles": bool(titles), "originals": originals})
        return ok


def disable() -> bool:
    from .core_browser_policy import REG_SZ, reg_delete, reg_write
    with _lock:
        data = load_settings()
        originals = data["originals"]
        ok = True
        if "dns_log" in originals:
            ok = set_dns_log(bool(originals.pop("dns_log"))) and ok
        for path in _CHROMIUM_POLICY_KEYS:
            orig = originals.pop(f"{path}|BuiltInDnsClientEnabled", None)
            if orig is None:
                continue
            try:
                if orig.get("exists"):
                    reg_write(path, "BuiltInDnsClientEnabled", orig.get("type") or REG_SZ, orig.get("data"))
                else:
                    reg_delete(path, "BuiltInDnsClientEnabled")
            except OSError:
                ok = False
        _save_settings({"enabled": False, "titles": data["titles"], "originals": originals})
        return ok


def set_titles(titles: bool) -> None:
    with _lock:
        data = load_settings()
        _save_settings(dict(data, titles=bool(titles)))
