"""Simulated Windows PC for demos and UI tests (no Windows needed).

Uses the real agent loop (hosts_editor/remote/agent.py) and the real catalogs (categories,
privacy settings, browsers), but every action only changes an in-memory state.

    python server/tools/demo_agent.py --server http://127.0.0.1:8080 --code ABCD-EFGH --name PC-LEA
"""

import argparse
import hashlib
import os
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.modules.setdefault("winreg", types.ModuleType("winreg"))  # catalogs only, nothing touches the registry

from hosts_editor import compliance, core_activity, core_networks, core_screentime  # noqa: E402
from hosts_editor.core_applocker import clean_exception, summarize_events  # noqa: E402
from hosts_editor.core import _parse_line  # noqa: E402
from hosts_editor.core_antispy import ITEMS  # noqa: E402
from hosts_editor.core_doh import BROWSERS  # noqa: E402
from hosts_editor.i18n import T, set_lang  # noqa: E402
from hosts_editor.parental_catalog import CATEGORIES, TELEMETRY_CATEGORY, comment_for_category  # noqa: E402
from hosts_editor.remote import AGENT_VERSION  # noqa: E402
from hosts_editor.remote.actions import normalize_domains  # noqa: E402
from hosts_editor.remote.agent import Agent, enroll  # noqa: E402

HOSTS_HEADER = """# Copyright (c) 1993-2009 Microsoft Corp.
#
# This is a sample HOSTS file used by Microsoft TCP/IP for Windows.
#
127.0.0.1 localhost
::1 localhost
192.168.1.20 nas.maison # NAS du salon
# 10.0.0.5 imprimante.local
"""


class DemoPC:
    def __init__(self, hostname: str):
        self.hostname = hostname
        self.lock = threading.RLock()
        self.cats = {c["file"]: False for c in CATEGORIES}
        self.cats["youtube.txt"] = True
        self.telemetry = False
        self.custom = ["jeux-flash.example", "casino.example"]
        self.custom_active = True
        self.cf = False
        self.prot = {"hosts_lock": True, "netblock": False, "vpn": False, "rstrui": False}
        self.doh = {b["id"]: b["id"] in ("chrome", "edge") for b in BROWSERS}
        self.apps = [{"exe_name": "fortnite.exe", "display_name": "Fortnite", "category": "custom", "enabled": True,
                      "locked_path": "", "added_at": time.time() - 86400}]
        self.privacy = {it["id"]: it["level"] == "basic" and it["kind"] != "hkcu_reg" for it in ITEMS}
        self.hosts = HOSTS_HEADER.splitlines()
        self.backups = [("hosts.bak_20261115_201500", "2026-11-15T20:15:00", 1210)]
        self.browser_level, self.safesites = "off", False
        self.dns_lock = False
        self.auto_repair = True
        self.children = []
        self.accounts = [
            {"name": "Julie", "full_name": "Julie Martin", "sid": "S-1-5-21-10-20-30-1001", "enabled": True,
             "source": "MicrosoftAccount", "admin": True},
            {"name": "Lea", "full_name": "Léa", "sid": "S-1-5-21-10-20-30-1002", "enabled": True,
             "source": "Local", "admin": True, "password_last_set": "2026-09-01T10:00:00"},
            {"name": "Administrateur", "full_name": "", "sid": "S-1-5-21-10-20-30-500", "enabled": False,
             "source": "Local", "admin": True},
        ]
        self.hosts_lock_undone = True
        self.report = None
        self.boot = time.time() - 3 * 3600
        # Phase 2
        self.wifi = {"name": "Wi-Fi", "ssid": "College-Pedago", "gateway": "10.20.0.1",
                     "gateway_mac": "00:1a:2b:3c:4d:5e", "dns": ["10.20.0.10", "10.20.0.11"], "domain": "college.lan"}
        self.net_policy = core_networks.clean_policy({})
        self.net_state = {"seen": [{"key": "ssid:Gare-SNCF|gw:aa:bb:cc:00:11:22", "ssid": "Gare-SNCF",
                                    "gateway": "172.16.0.1", "gateway_mac": "aa:bb:cc:00:11:22", "domain": "",
                                    "kind": "captive", "first": time.time() - 7200, "last": time.time() - 7000, "count": 1}],
                          "events": [{"id": 1, "at": time.time() - 7200, "kind": "captive_open", "key": "gare",
                                      "message": "Portail de connexion probable sur « Gare-SNCF » : filtre DNS suspendu 10 minutes sur ce réseau."},
                                     {"id": 2, "at": time.time() - 7000, "kind": "captive_closed", "key": "gare",
                                      "message": "Filtre DNS rétabli sur « Gare-SNCF » (connexion au portail réussie)"}],
                          "next_event_id": 3}
        self.applocker = {"mode": "off", "exceptions": [], "method": None}
        child = "S-1-5-21-10-20-30-1002"
        self.applocker_events = summarize_events([
            {"id": 8003, "time": "2026-11-18T09:10:00Z", "user": child,
             "path": r"%OSDRIVE%\USERS\LEA\APPDATA\LOCAL\ROBLOX\VERSIONS\ROBLOXPLAYERBETA.EXE", "hash": "0x" + "CD" * 32,
             "fqbn": r"O=ROBLOX CORPORATION, L=SAN MATEO, S=CALIFORNIA, C=US\ROBLOX\ROBLOXPLAYERBETA.EXE\1.0"},
            {"id": 8003, "time": "2026-11-18T09:40:00Z", "user": child,
             "path": r"%OSDRIVE%\USERS\LEA\DOWNLOADS\TOR BROWSER\BROWSER\FIREFOX.EXE", "hash": "0x" + "EF" * 32, "fqbn": "-"},
            {"id": 8003, "time": "2026-11-18T08:00:00Z", "user": child,
             "path": r"%OSDRIVE%\USERS\LEA\APPDATA\LOCAL\DISCORD\APP-1.0.9\DISCORD.EXE", "hash": "0x" + "AA" * 32,
             "fqbn": r"O=DISCORD INC., L=SAN FRANCISCO, S=CALIFORNIA, C=US\DISCORD\DISCORD.EXE\1.0.9"}], [child])
        self.hardening = {"hide_settings": False, "machine_proxy": False, "block_protocols": False, "drift": []}
        self.safesearch = {"google": False, "youtube": "off", "bing": False, "ips": {}}
        self.extensions = {"mode": "proxy", "chromium": [], "firefox": []}
        self.child_private_off = False
        # Phase 3: screen time (real decision engine) and activity journal (real aggregator, simulated input)
        data_dir = tempfile.mkdtemp(prefix=f"hots-demo-activity-{hostname}-")
        core_screentime.POLICY_FILE = os.path.join(data_dir, "screentime_policy.json")
        core_screentime.USAGE_FILE = os.path.join(data_dir, "screentime_usage.json")
        self.enforcer = core_screentime.Enforcer()
        self.journal = core_activity.Journal(os.path.join(data_dir, "activity"))
        self.activity = {"enabled": False, "titles": True}
        self.simulated = set()
        self.session_locked = False

    def kids(self):
        return [{"sid": a["sid"], "name": a["full_name"] or a["name"]} for a in self.accounts if a["sid"] in self.children]

    def simulate(self, sid: str, now: float):
        """A believable day of use for a child account (the journal itself is the real one)."""
        j = self.journal
        j.names[sid] = next((k["name"] for k in self.kids() if k["sid"] == sid), sid)
        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        plan = [  # (start, minutes, exe, title, sites)
            (-26 * 3600, 35, chrome, "Pronote - Espace Élèves - Google Chrome", ["index-education.net"]),
            (-25.3 * 3600, 50, r"C:\Users\Lea\AppData\Roaming\.minecraft\javaw.exe", "Minecraft 1.21", []),
            (-5 * 3600, 25, chrome, "Pronote - Cahier de textes - Google Chrome", ["index-education.net"]),
            (-4.5 * 3600, 20, r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
             "Exposé histoire.docx - Word", []),
            (-4.1 * 3600, 30, chrome, "Révolution française — Wikipédia - Google Chrome", ["wikipedia.org"]),
            (-2 * 3600, 45, chrome, "Minecraft : les meilleures maps - YouTube - Google Chrome", ["youtube.com"]),
            (-1.2 * 3600, 25, r"C:\Users\Lea\AppData\Local\Discord\app-1.0.9\Discord.exe", "#général - Discord",
             ["discord.com"]),
            (-0.7 * 3600, 30, r"C:\Users\Lea\AppData\Roaming\.minecraft\javaw.exe", "Minecraft 1.21", []),
            (-0.2 * 3600, 10, chrome, "Mathématiques - Kartable - Google Chrome", ["kartable.fr"]),
        ]
        domains = {"index-education.net": "0123456b.index-education.net", "wikipedia.org": "fr.wikipedia.org",
                   "youtube.com": "rr4---sn-25ge7nsd.googlevideo.com", "discord.com": "gateway.discord.gg",
                   "kartable.fr": "www.kartable.fr"}
        for start, minutes, exe, title, sites in plan:
            t0 = now + start
            for i in range(int(minutes * 60 / 5) + 1):
                t = t0 + i * 5
                if t > now:
                    break
                j.add_sample(77, sid, {"t": t, "exe": exe, "title": title, "idle": 3})
                if sites and i % 12 == 0:
                    for site in sites:
                        j.add_dns(sid, {"name": domains[site], "t": t, "results": "93.184.216.34;", "status": 0},
                                  "chrome.exe", {})
            j._last_sample.pop(77, None)
        index = {"www.tiktok.com": "TikTok", "www.instagram.com": "Instagram"}
        for offset, name, res in ((-2.2, "www.tiktok.com", "0.0.0.0;"), (-2.15, "www.tiktok.com", "0.0.0.0;"),
                                  (-1.9, "www.instagram.com", "0.0.0.0;"), (-0.6, "casino-en-ligne.example", "0.0.0.0;"),
                                  (-0.5, "www.tiktok.com", "0.0.0.0;")):
            j.add_dns(sid, {"name": name, "t": now + offset * 3600, "results": res, "status": 0}, "chrome.exe", index)
        j.add_blocked_program(sid, r"C:\Users\Lea\Downloads\FirefoxPortable\FirefoxPortable.exe", now - 3 * 3600)
        j.add_blocked_program(sid, r"E:\psiphon3.exe", now - 1.5 * 3600)
        j.add_event(sid, now - 1.5 * 3600, "helper", "Le suivi d'activité a été fermé (redémarré)")
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        self.enforcer._add(sid, now, max(0, j.get(day, sid)["active_s"] - self.enforcer.used(sid, now)))
        yday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
        self.enforcer._add(sid, now - 86400, j.get(yday, sid)["active_s"])
        j.set_screen(sid, yday, j.get(yday, sid)["active_s"])

    def tick(self):
        """Every few seconds: the child is "using" the PC; screen time is enforced (messages printed)."""
        now = time.time()
        with self.lock:
            kids = self.kids()
            if self.activity["enabled"]:
                for k in kids:
                    if k["sid"] not in self.simulated:
                        self.simulated.add(k["sid"])
                        self.simulate(k["sid"], now)
            sessions = [{"id": 2, "sid": k["sid"], "active": True, "locked": self.session_locked, "idle": 4}
                        for k in kids[:1]]
            for action in self.enforcer.tick(sessions, core_screentime.load_policies(), now):
                if action[0] == "lock":
                    self.session_locked = True
                print("[temps d'écran]", action[0], *action[2:], flush=True)
            if self.session_locked and sessions:
                ev = core_screentime.evaluate(core_screentime.load_policies().get(sessions[0]["sid"]) or {},
                                              self.enforcer.used(sessions[0]["sid"], now),
                                              core_screentime.datetime.fromtimestamp(now))
                if ev["allowed"]:
                    self.session_locked = False  # the child signs in again once allowed
            for ev in self.enforcer.events:
                if not ev.get("_logged"):
                    ev["_logged"] = True
                    self.journal.add_event(ev["sid"], ev["t"], ev["kind"], ev["text"])
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            for k in kids:
                if self.activity["enabled"] or (core_screentime.load_policies().get(k["sid"]) or {}).get("enabled"):
                    self.journal.set_screen(k["sid"], day, self.enforcer.used(k["sid"], now))

    def _screentime(self, sid):
        return next((x for x in core_screentime.status(self.kids(), self.enforcer) if x["sid"] == sid), {})

    def _networks(self):
        plan = core_networks.decide([self.wifi], cf_ok=False, policy=self.net_policy, state=self.net_state,
                                    now=time.time(), dns_enforced=self.cf or self.dns_lock)
        self.net_state = plan["state"]
        return plan

    def _adapters(self):
        relax = (self.net_state.get("relax") or {}).get("Wi-Fi")
        if relax:
            return [{"name": "Wi-Fi", "dhcp": True, "v4": list(self.wifi["dns"]), "v6": [], "ok": relax["reason"] == "trusted",
                     "relaxed": relax["reason"], "network": relax["network"], "until": relax.get("until")}]
        cf = self.cf or self.dns_lock
        wifi_v4 = ["1.1.1.3", "1.0.0.3"] if cf else ["192.168.1.1"]
        wifi_v6 = ["2606:4700:4700::1113", "2606:4700:4700::1003"] if self.dns_lock else ["2a01:cb00:12::1"]
        phone_v4 = ["1.1.1.3", "1.0.0.3"] if self.dns_lock else ["192.168.42.129"]
        return [
            {"name": "Wi-Fi", "dhcp": not cf, "v4": wifi_v4, "v6": wifi_v6, "ok": cf and self.dns_lock},
            {"name": "Ethernet 2 (partage de connexion du téléphone)", "dhcp": not self.dns_lock, "v4": phone_v4,
             "v6": [], "ok": self.dns_lock},
        ]

    def _compliance(self):
        users = [dict(a, password_last_set=a.get("password_last_set") or ("2026-01-01T00:00:00" if a["name"] == "Julie" else None))
                 for a in self.accounts]
        adapters = self._adapters()
        data = {
            "children": list(self.children),
            "windows": {"admins": [a["sid"] for a in self.accounts if a["admin"]], "users": users, "bitlocker": 1,
                        "features": {"Microsoft-Windows-Subsystem-Linux": 0}},
            "registry": {"EnableLUA": 1, "PromptOnSecureDesktop": 1, "SecureBoot": 1,
                         "DataBasePath": r"%SystemRoot%\System32\drivers\etc"},
            "observed": {
                "hosts_lock": {"wanted": True, "real": not self.hosts_lock_undone, "drift": self.hosts_lock_undone},
                "browsers": {"wanted": self.browser_level, "drift": False},
                "dns": {"wanted": self.dns_lock, "all_ok": all(a["ok"] for a in adapters),
                        "any_cf": self.cf or self.dns_lock, "adapters": adapters},
            },
            "unmanaged": [] if any(a["exe_name"] == "opera.exe" for a in self.apps) else [
                {"profile": "Lea", "name": "Opera / Opera GX (VPN intégré)", "kind": "browser", "exe": "opera.exe",
                 "path": r"C:\Users\Lea\AppData\Local\Programs\Opera\opera.exe"}],
            "proxy_extensions": [{"profile": "Lea", "browser": "Google Chrome", "id": "bihmplhobchoageeokmgbdihknkjbknd",
                                  "name": "Touch VPN", "required": True}],
        }
        data["observed"]["applocker"] = {"wanted": self.applocker["mode"], "drift": False,
                                         "would_block": len(self.applocker_events), "blocked": 0}
        data["observed"]["hardening"] = dict(self.hardening, drift=False)
        data["observed"]["safesearch"] = {"google": self.safesearch["google"], "drift": False}
        checks = compliance.evaluate(data)
        score, todo = compliance.score(checks)
        accounts = [{"sid": a["sid"], "name": a["name"], "full_name": a["full_name"], "enabled": a["enabled"],
                     "admin": a["admin"], "child": a["sid"] in self.children, "builtin": a["sid"].endswith("-500"),
                     "source": a["source"], "last_logon": "2026-11-18T08:12:00"} for a in self.accounts]
        self.report = {"score": score, "todo": todo, "checks": checks, "accounts": accounts, "errors": {},
                       "checked_at": time.time(), "duration_s": 8.4, "extensions": [
                           {"browser": "Google Chrome", "family": "chromium", "id": "bihmplhobchoageeokmgbdihknkjbknd",
                            "name": "Touch VPN", "proxy": True, "profile": "Lea"},
                           {"browser": "Google Chrome", "family": "chromium", "id": "cjpalhdlnbpafiamejdnhcphjbkeiagm",
                            "name": "uBlock Origin", "proxy": False, "profile": "Lea"},
                           {"browser": "Mozilla Firefox", "family": "firefox", "id": "{d10d0bf8-f5b5-c8b4-a8b2-2b9879e08c5d}",
                            "name": "Adblock Plus", "proxy": False, "profile": "Lea"}]}
        return self.report

    def info(self):
        return {"hostname": self.hostname, "os": "Windows 11 Famille 24H2 (build 26100.2314)",
                "app_version": "2.5.1", "agent_version": AGENT_VERSION, "machine_id": "demo-" + self.hostname,
                "last_boot": int(self.boot)}

    def state(self, lang="fr"):
        set_lang(lang)
        with self.lock:
            self._networks()
            entries = [_parse_line(line) for line in self.hosts]
            return {
                "collected_at": time.time(), "errors": {},
                "system": dict(self.info(), uptime_s=3 * 3600 + 1200, is_admin=True),
                "parental": {
                    "categories": [{"file": c["file"], "label": comment_for_category(c), "icon": c["icon"],
                                    "color": c["color"], "active": self.cats[c["file"]], "available": True}
                                   for c in CATEGORIES],
                    "telemetry": {"file": "telemetry.txt", "label": comment_for_category(TELEMETRY_CATEGORY),
                                  "active": self.telemetry},
                    "custom": {"file": "custom_domains.txt", "active": self.custom_active, "domains": list(self.custom)},
                },
                "cf_family": {"active": self.cf or self.dns_lock, "complete": all(a["ok"] for a in self._adapters()),
                              "details": " · ".join(f"{a['name']}: {', '.join(a['v4'] + a['v6'])}" for a in self._adapters()),
                              "lock": {"active": self.dns_lock, "policy": self.dns_lock, "adapters": self._adapters()}},
                "networks": {"policy": self.net_policy, "relax": self.net_state.get("relax") or {},
                             "seen": self.net_state.get("seen") or [], "events": (self.net_state.get("events") or [])[-20:]},
                "applocker": dict(self.applocker, events=self.applocker_events, events_at=time.time()),
                "hardening": self.hardening,
                "safesearch": self.safesearch,
                "browser_policies": {"level": self.browser_level, "safesites": self.safesites, "drift": 0,
                                     "extensions": self.extensions, "child_private_off": self.child_private_off,
                                     "browsers": [{"id": b, "name": n, "ok": True, "drift": []} for b, n in (
                                         ("chrome", "Google Chrome"), ("edge", "Microsoft Edge"), ("brave", "Brave"),
                                         ("vivaldi", "Vivaldi"), ("chromium", "Chromium"), ("firefox", "Mozilla Firefox"))]},
                "compliance": {"report": self.report, "options": {"auto_repair": self.auto_repair}},
                "protections": dict(self.prot, doh=[{"id": b["id"], "name": b["name"], "installed": b["id"] != "vivaldi",
                                                     "blocked": self.doh[b["id"]]} for b in BROWSERS]),
                "apps": [dict(a) for a in self.apps],
                "privacy": {"levels": [{"id": lvl, "label": T(f"par_antispy_{lvl}_btn"), "items": [
                    {"id": it["id"], "label": T(it["label_key"]), "desc": T(it["desc_key"]), "kind": it["kind"],
                     "user_scope": it["kind"] == "hkcu_reg",
                     "active": None if it["kind"] == "hkcu_reg" else self.privacy[it["id"]],
                     "missing": it["id"] == "medium_task_usbceip"}
                    for it in ITEMS if it["level"] == lvl]} for lvl in ("basic", "medium", "advanced", "extra")]},
                "screentime": core_screentime.status(self.kids(), self.enforcer),
                "activity": dict(self.activity, helpers=1 if self.activity["enabled"] else 0, children=self.kids()),
                "hosts": {"path": r"C:\Windows\System32\drivers\etc\hosts", "size": len("\n".join(self.hosts)),
                          "lines": len(entries), "active_entries": sum(1 for e in entries if e["enabled"] is True),
                          "disabled_entries": sum(1 for e in entries if e["enabled"] is False), "backups": len(self.backups)},
            }

    def _hosts_payload(self):
        text = "\n".join(self.hosts) + "\n"
        entries = [_parse_line(line) for line in self.hosts]
        return {"path": r"C:\Windows\System32\drivers\etc\hosts", "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "size": len(text), "locked": self.prot["hosts_lock"], "text": text,
                "entries": [{"line": i, "enabled": e["enabled"], "ip": e["ip"], "hostname": e["hostname"],
                             "comment": e["comment"], "raw": e["raw"]} for i, e in enumerate(entries) if e["enabled"] is not None]}

    def execute(self, action, p, lang):
        time.sleep(0.4)  # a real PC takes a moment
        with self.lock:
            try:
                return self._execute(action, p, lang)
            except KeyError as e:
                return False, None, f"Paramètre manquant : {e}", True

    def _execute(self, action, p, lang):
        ok = lambda result=None, mutating=True: (True, result or {}, "", mutating)  # noqa: E731
        if action == "agent.ping":
            return ok({"pong": True}, False)
        if action.startswith("screentime."):
            if p.get("sid") not in self.children:
                return False, None, "Ce compte n'est pas marqué comme compte enfant (onglet Sécurité).", True
            try:
                if action == "screentime.set":
                    core_screentime.set_policy(p["sid"], p.get("policy"))
                elif action == "screentime.bonus":
                    core_screentime.add_bonus(p["sid"], int(p["minutes"]))
                else:
                    core_screentime.set_pause(p["sid"], int(p["minutes"]))
            except ValueError as e:
                return False, None, f"Réglage refusé : {e}", True
            self.tick()
            return ok(self._screentime(p["sid"]))
        if action == "activity.set":
            self.activity = {"enabled": bool(p["enabled"]),
                             "titles": self.activity["titles"] if p.get("titles") is None else bool(p["titles"])}
            self.tick()
            return ok(dict(self.activity, helpers=1, children=self.kids()))
        if action == "activity.sync":
            day = time.strftime("%Y-%m-%d")
            days = [self.journal.summary(self.journal.get(day, k["sid"])) for k in self.kids()]
            days += [x for x in self.journal.outbox(limit=10) if x["day"] != day]
            return ok({"days": days,
                       "status": dict(self.activity, children=self.kids())}, False)
        if action == "get_state":
            return ok(self.state(lang), False)
        if action == "parental.set":
            if p["category"] == "custom_domains.txt":
                self.custom_active = p["enabled"]
            elif p["category"] == "telemetry.txt":
                self.telemetry = p["enabled"]
            else:
                self.cats[p["category"]] = p["enabled"]
            return ok()
        if action == "custom_domains.set":
            valid, invalid = normalize_domains(p["domains"])
            if invalid:
                return False, None, "Domaines invalides : " + ", ".join(invalid[:15]), True
            self.custom = valid
            self.custom_active = bool(valid) and (self.custom_active or p.get("apply") is True)
            return ok({"count": len(valid)})
        if action == "cf_family.set":
            self.cf = p["enabled"]
            if not self.cf:
                self.dns_lock = False
            return ok()
        if action == "dns_lock.set":
            self.dns_lock = p["enabled"]
            self.cf = self.cf or self.dns_lock
            return ok()
        if action == "browser_policies.set":
            if p["level"] not in ("off", "standard", "strict"):
                return False, None, f"Niveau inconnu : {p['level']}", True
            self.browser_level, self.safesites = p["level"], p.get("safesites") is True
            return ok()
        if action == "compliance.check":
            time.sleep(1.5)
            return ok(self._compliance())
        if action == "accounts.set_child":
            sid = p["sid"]
            self.children = [c for c in self.children if c != sid] + ([sid] if p["is_child"] else [])
            return ok({"children": self.children})
        if action in ("accounts.make_standard", "accounts.make_admin"):
            if action == "accounts.make_standard" and p["sid"] not in self.children:
                return False, None, "Marquez d'abord ce compte comme « compte enfant ».", True
            for a in self.accounts:
                if a["sid"] == p["sid"]:
                    a["admin"] = action == "accounts.make_admin"
            return ok({"done": True})
        if action == "protections.repair":
            repaired = ["verrou du fichier hosts"] if self.hosts_lock_undone else []
            self.hosts_lock_undone = False
            return ok({"repaired": repaired})
        if action == "applocker.set":
            if p["mode"] != "off" and not self.children:
                return False, None, "Déclarez d'abord le compte Windows de l'enfant (onglet Sécurité).", True
            self.applocker["mode"] = p["mode"]
            self.applocker["method"] = None if p["mode"] == "off" else "local"
            return ok({"mode": p["mode"]})
        if action == "applocker.allow":
            try:
                exc = clean_exception(p.get("exception"))
                if exc["kind"] == "file":  # a real PC reads the file; the demo uses a fake fingerprint
                    exc = clean_exception({"kind": "hash", "hash": hashlib.sha256(exc["path"].encode()).hexdigest(),
                                           "file_name": exc["label"], "label": exc["label"]})
            except ValueError as e:
                return False, None, f"Exception invalide : {e}", True
            self.applocker["exceptions"] = [e for e in self.applocker["exceptions"] if e["id"] != exc["id"]] + [exc]
            return ok()
        if action == "applocker.remove":
            self.applocker["exceptions"] = [e for e in self.applocker["exceptions"] if e["id"] != p["id"]]
            return ok()
        if action == "applocker.refresh":
            return ok()
        if action == "extensions.set":
            self.extensions = {"mode": p["mode"], "chromium": p.get("chromium") or [], "firefox": p.get("firefox") or []}
            return ok(self.extensions)
        if action == "child_browsing.set":
            self.child_private_off = p["private_off"]
            return ok()
        if action == "network_hardening.set":
            self.hardening.update({k: p[k] for k in ("hide_settings", "machine_proxy", "block_protocols")})
            return ok()
        if action == "safesearch.set":
            self.safesearch.update({"google": p.get("google") is True, "youtube": p.get("youtube", "off"),
                                    "bing": p.get("bing") is True})
            return ok(self.safesearch)
        if action == "agent.set_options":
            self.auto_repair = p["auto_repair"]
            return ok({"auto_repair": self.auto_repair})
        if action in ("hosts_lock.set", "netblock.set", "vpn.set", "rstrui_lock.set"):
            key = {"hosts_lock.set": "hosts_lock", "netblock.set": "netblock", "vpn.set": "vpn", "rstrui_lock.set": "rstrui"}[action]
            self.prot[key] = p["enabled"]
            return ok()
        if action == "doh.set":
            self.doh[p["browser"]] = p["enabled"]
            return ok()
        if action == "apps.add":
            name = p["exe_name"].lower()
            name = name if name.endswith(".exe") else name + ".exe"
            if name in ("explorer.exe", "cmd.exe"):
                return False, None, "Ce programme système ne peut pas être bloqué.", True
            self.apps = [a for a in self.apps if a["exe_name"] != name]
            self.apps.append({"exe_name": name, "display_name": p.get("display_name") or name, "category": "custom",
                              "enabled": True, "locked_path": p.get("path") or "", "added_at": time.time()})
            return ok()
        if action == "apps.set_enabled":
            for a in self.apps:
                if a["exe_name"] == p["exe_name"]:
                    a["enabled"] = p["enabled"]
            return ok()
        if action == "apps.remove":
            self.apps = [a for a in self.apps if a["exe_name"] != p["exe_name"]]
            return ok()
        if action == "apps.list_installed":
            progs = [("Minecraft Launcher", r"C:\Program Files (x86)\Minecraft Launcher\MinecraftLauncher.exe"),
                     ("Roblox Player", r"C:\Users\lea\AppData\Local\Roblox\Versions\RobloxPlayerBeta.exe"),
                     ("Discord", r"C:\Users\lea\AppData\Local\Discord\app-1.0.9\Discord.exe"),
                     ("Steam", r"C:\Program Files (x86)\Steam\steam.exe"),
                     ("VLC media player", r"C:\Program Files\VideoLAN\VLC\vlc.exe")]
            return ok({"programs": [{"name": n, "path": path, "exe": path.rsplit("\\", 1)[-1]} for n, path in progs]}, False)
        if action == "privacy.set_item":
            self.privacy[p["id"]] = p["enabled"]
            return ok()
        if action == "privacy.set_level":
            for it in ITEMS:
                if it["level"] == p["level"] and it["kind"] != "hkcu_reg":
                    self.privacy[it["id"]] = p["enabled"]
            return ok()
        if action == "privacy.check_drift":
            return ok({"regressed": [], "restored": []}, False)
        if action == "system.restore_point":
            return ok({"status": "created"})
        if action == "dns.flush":
            return ok({"flushed": True})
        if action == "hosts.read":
            return ok(self._hosts_payload(), False)
        if action == "hosts.backups":
            return ok({"backups": [{"name": n, "date": d, "size": s} for n, d, s in self.backups]}, False)
        if action == "hosts.add_entry":
            line = f"{p['ip']} {p['hostname']}" + (f" # {p['comment']}" if p.get("comment") else "")
            self.hosts.append(line if p.get("enabled", True) else "# " + line)
            return ok()
        if action in ("hosts.update_entry", "hosts.delete_entry"):
            i = p["line"]
            if i >= len(self.hosts) or self.hosts[i] != p["expect"]:
                return False, None, "Le fichier hosts a été modifié entre-temps : rechargez la page et réessayez.", True
            if action == "hosts.delete_entry":
                del self.hosts[i]
                return ok()
            e = _parse_line(self.hosts[i])
            ip, host, comment = p.get("ip", e["ip"]), p.get("hostname", e["hostname"]), p.get("comment", e["comment"])
            enabled = p.get("enabled", e["enabled"])
            line = f"{ip} {host}" + (f" # {comment}" if comment else "")
            self.hosts[i] = line if enabled else "# " + line
            return ok()
        if action == "hosts.save_text":
            self.hosts = p["text"].rstrip("\n").split("\n")
            return ok()
        if action == "hosts.restore_backup":
            self.hosts = HOSTS_HEADER.splitlines()
            return ok()
        return False, None, f"Action inconnue : {action}", False


def main():
    ap = argparse.ArgumentParser(description="PC Windows simulé pour la démo de la console HOTS")
    ap.add_argument("--server", required=True)
    ap.add_argument("--code", required=True)
    ap.add_argument("--name", default="PC-DEMO")
    args = ap.parse_args()
    os.environ.setdefault("HOTS_AGENT_DIR", tempfile.mkdtemp(prefix=f"hots-demo-{args.name}-"))
    pc = DemoPC(args.name)
    cfg = enroll(args.server, args.code, pc.info())
    print(f"{args.name} connecté ({cfg['device_id']}) — Ctrl+C pour arrêter", flush=True)
    def on_config(agent, resp):
        if "network_policy" in resp:
            policy = core_networks.clean_policy(resp["network_policy"])
            if policy != pc.net_policy:
                pc.net_policy = policy
                agent._state_due = True

    agent = Agent(cfg, executor=pc.execute, state_builder=pc.state, info_builder=pc.info, on_config=on_config,
                  periodic_fast=lambda a: pc.tick(), outbox=pc.journal.outbox, outbox_ack=pc.journal.ack)
    try:
        agent.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
