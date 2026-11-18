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

from hosts_editor import compliance  # noqa: E402
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

    def _adapters(self):
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
        checks = compliance.evaluate(data)
        score, todo = compliance.score(checks)
        accounts = [{"sid": a["sid"], "name": a["name"], "full_name": a["full_name"], "enabled": a["enabled"],
                     "admin": a["admin"], "child": a["sid"] in self.children, "builtin": a["sid"].endswith("-500"),
                     "source": a["source"], "last_logon": "2026-11-18T08:12:00"} for a in self.accounts]
        self.report = {"score": score, "todo": todo, "checks": checks, "accounts": accounts, "errors": {},
                       "checked_at": time.time(), "duration_s": 8.4}
        return self.report

    def info(self):
        return {"hostname": self.hostname, "os": "Windows 11 Famille 24H2 (build 26100.2314)",
                "app_version": "2.5.1", "agent_version": AGENT_VERSION, "machine_id": "demo-" + self.hostname,
                "last_boot": int(self.boot)}

    def state(self, lang="fr"):
        set_lang(lang)
        with self.lock:
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
                "browser_policies": {"level": self.browser_level, "safesites": self.safesites, "drift": 0,
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
    agent = Agent(cfg, executor=pc.execute, state_builder=pc.state, info_builder=pc.info)
    try:
        agent.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
