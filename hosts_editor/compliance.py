"""Compliance report ("Sécurité" tab of the web console): is parental control really enforceable?

Read-only checks (accounts, UAC, boot/encryption, virtualisation, hosts redirection, real state of
every protection, unmanaged browsers/VPNs, proxy extensions, native VPN profiles, user proxies),
a score out of 100 and the 3 most useful fixes. Also: child-account bookkeeping, "make standard"
with safety guards, and the auto-repair of protections that were undone.

No Qt here: used by the remote agent (SYSTEM). Texts are in French (the console language).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time

from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.compliance")

CHILDREN_FILE = os.path.join(PROGRAMDATA_DIR, "children.json")
ACCOUNTS_SNAPSHOT = os.path.join(PROGRAMDATA_DIR, "accounts_snapshot.json")
ACCESSIBILITY_EXES = ("sethc.exe", "utilman.exe", "osk.exe", "Magnify.exe", "Narrator.exe", "DisplaySwitch.exe",
                      "AtBroker.exe")
OPTIONS_FILE = os.path.join(PROGRAMDATA_DIR, "agent_options.json")
REPORT_FILE = os.path.join(PROGRAMDATA_DIR, "compliance_report.json")

ADMINS_SID = "S-1-5-32-544"
USERS_SID = "S-1-5-32-545"
SID_RE = re.compile(r"^S-1-(5-21|12-1)(-\d{1,10}){4,6}$")
_CREATE_NO_WINDOW = 0x08000000
_SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
POWERSHELL = os.path.join(_SYSTEM32, "WindowsPowerShell", "v1.0", "powershell.exe")

PENALTY = {"critical": 30, "high": 15, "medium": 8, "low": 3, "info": 0}

# Executables that ignore our browser policies or tunnel traffic (lower-case file names).
UNMANAGED_BROWSERS = {
    "opera.exe": "Opera / Opera GX (VPN intégré)", "tor.exe": "Tor Browser", "librewolf.exe": "LibreWolf",
    "waterfox.exe": "Waterfox", "floorp.exe": "Floorp", "palemoon.exe": "Pale Moon", "basilisk.exe": "Basilisk",
    "zen.exe": "Zen Browser", "thorium.exe": "Thorium", "mullvadbrowser.exe": "Mullvad Browser",
    "firefoxportable.exe": "Firefox portable", "chromeportable.exe": "Chrome portable",
    "yandex.exe": "Yandex Browser", "maxthon.exe": "Maxthon", "slimjet.exe": "Slimjet", "ur.exe": "UR Browser",
    "epic.exe": "Epic Privacy Browser", "avastbrowser.exe": "Avast Secure Browser",
}
TUNNEL_APPS = {
    "psiphon3.exe": "Psiphon", "psiphon.exe": "Psiphon", "lantern.exe": "Lantern", "ultrasurf.exe": "UltraSurf",
    "u.exe": None, "windscribe.exe": "Windscribe", "protonvpn.exe": "Proton VPN", "nordvpn.exe": "NordVPN",
    "expressvpn.exe": "ExpressVPN", "surfshark.exe": "Surfshark", "hotspotshield.exe": "Hotspot Shield",
    "tunnelbear.exe": "TunnelBear", "cyberghost8.exe": "CyberGhost", "pia_manager.exe": "Private Internet Access",
    "openvpn-gui.exe": "OpenVPN", "openvpn.exe": "OpenVPN", "wireguard.exe": "WireGuard", "outline.exe": "Outline",
    "v2rayn.exe": "v2rayN", "nekoray.exe": "NekoRay", "hiddify.exe": "Hiddify", "clash.exe": "Clash",
    "clash-verge.exe": "Clash Verge", "shadowsocks.exe": "Shadowsocks", "vpncmgr.exe": "SoftEther / VPN Gate",
    "planetvpn.exe": "Planet VPN", "urbanvpn.exe": "Urban VPN", "hide.me.exe": "hide.me", "warp-svc.exe": "Cloudflare WARP",
    "cloudflare warp.exe": "Cloudflare WARP", "betternet.exe": "Betternet", "hola.exe": "Hola",
}
_SCAN_SUBDIRS = ("Downloads", "Desktop", "Documents", os.path.join("AppData", "Local"),
                 os.path.join("AppData", "Roaming"))
_SKIP_PROFILES = {"public", "default", "default user", "all users", "defaultapppool", "wdagutilityaccount"}
_MAX_SCAN_FILES = 60000
_MAX_DEPTH = 5

CHROMIUM_DATA = (
    ("Google Chrome", os.path.join("AppData", "Local", "Google", "Chrome", "User Data")),
    ("Microsoft Edge", os.path.join("AppData", "Local", "Microsoft", "Edge", "User Data")),
    ("Brave", os.path.join("AppData", "Local", "BraveSoftware", "Brave-Browser", "User Data")),
    ("Vivaldi", os.path.join("AppData", "Local", "Vivaldi", "User Data")),
    ("Opera", os.path.join("AppData", "Roaming", "Opera Software", "Opera Stable")),
    ("Opera GX", os.path.join("AppData", "Roaming", "Opera Software", "Opera GX Stable")),
)


# --- small stores ----------------------------------------------------------------------------------

def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def child_sids() -> list:
    sids = _read_json(CHILDREN_FILE, {}).get("sids")
    return [s for s in sids if isinstance(s, str) and SID_RE.match(s)] if isinstance(sids, list) else []


def set_child(sid: str, is_child: bool) -> list:
    sids = set(child_sids())
    (sids.add if is_child else sids.discard)(sid)
    atomic_write_json(CHILDREN_FILE, {"sids": sorted(sids)})
    return sorted(sids)


def options() -> dict:
    data = _read_json(OPTIONS_FILE, {})
    return {"auto_repair": data.get("auto_repair", True) is not False,
            "auto_update": data.get("auto_update", True) is not False}


def set_options(**changes) -> dict:
    data = options()
    data.update({k: bool(v) for k, v in changes.items() if k in ("auto_repair", "auto_update") and v is not None})
    atomic_write_json(OPTIONS_FILE, data)
    return data


def cached_report():
    data = _read_json(REPORT_FILE, {})
    return data or None


# --- Windows data collection ---------------------------------------------------------------------------

_PS_COLLECT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$r = [ordered]@{}
$admins = @()
try {
  $admins = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop | ForEach-Object { $_.SID.Value })
} catch {
  try {
    $name = (New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')).Translate([System.Security.Principal.NTAccount]).Value.Split('\')[-1]
    $g = [ADSI]("WinNT://./$name,group")
    $admins = @($g.Invoke('Members') | ForEach-Object {
      (New-Object System.Security.Principal.SecurityIdentifier([byte[]]$_.GetType().InvokeMember('objectSid', 'GetProperty', $null, $_, $null), 0)).Value })
  } catch {}
}
$r.admins = $admins
$r.users = @(Get-LocalUser | ForEach-Object {
  [ordered]@{ name = $_.Name; full_name = $_.FullName; sid = $_.SID.Value; enabled = [bool]$_.Enabled;
    source = [string]$_.PrincipalSource;
    password_last_set = $(if ($_.PasswordLastSet) { $_.PasswordLastSet.ToString('o') } else { $null });
    last_logon = $(if ($_.LastLogon) { $_.LastLogon.ToString('o') } else { $null }) } })
$bl = $null
try {
  $v = Get-CimInstance -Namespace 'root\cimv2\Security\MicrosoftVolumeEncryption' -ClassName Win32_EncryptableVolume -Filter "DriveLetter='$($env:SystemDrive)'" -ErrorAction Stop
  if ($v) { $bl = [int]$v.ProtectionStatus }
} catch {}
$r.bitlocker = $bl
$f = [ordered]@{}
foreach ($n in 'Microsoft-Windows-Subsystem-Linux','VirtualMachinePlatform','Containers-DisposableClientVM','Microsoft-Hyper-V-All') {
  $x = Get-CimInstance -ClassName Win32_OptionalFeature -Filter "Name='$n'"
  $f[$n] = $(if ($x) { [int]$x.InstallState } else { $null })
}
$r.features = $f
$acc = @()
foreach ($n in 'sethc.exe','utilman.exe','osk.exe','Magnify.exe','Narrator.exe','DisplaySwitch.exe','AtBroker.exe') {
  $p = Join-Path $env:windir "System32\$n"
  if (Test-Path -LiteralPath $p) {
    $sig = Get-AuthenticodeSignature -LiteralPath $p
    $acc += [ordered]@{ name = $n; status = [string]$sig.Status; signer = [string]$sig.SignerCertificate.Subject }
  }
}
$r.accessibility = $acc
$r | ConvertTo-Json -Depth 4 -Compress
"""


def _powershell(script: str, timeout: int = 90):
    try:
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                           creationflags=_CREATE_NO_WINDOW, timeout=timeout)
        return r
    except Exception as e:
        logger.warning("powershell failed: %s", e)
        return None


def collect_windows() -> dict:
    """Accounts, admins, BitLocker, optional features (one PowerShell call)."""
    r = _powershell("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + _PS_COLLECT)
    if r is None or not (r.stdout or "").strip():
        return {}
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except ValueError:
        return {}
    for key in ("admins", "users"):
        if isinstance(data.get(key), dict):
            data[key] = [data[key]]
        elif not isinstance(data.get(key), list):
            data[key] = []
    return data


def _reg(root_name: str, path: str, name: str):
    try:
        import winreg
        root = getattr(winreg, root_name)
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None
    except Exception:
        return None


def collect_registry() -> dict:
    pol = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
    wl = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
    return {
        "EnableLUA": _reg("HKEY_LOCAL_MACHINE", pol, "EnableLUA"),
        "PromptOnSecureDesktop": _reg("HKEY_LOCAL_MACHINE", pol, "PromptOnSecureDesktop"),
        "ConsentPromptBehaviorAdmin": _reg("HKEY_LOCAL_MACHINE", pol, "ConsentPromptBehaviorAdmin"),
        "AutoAdminLogon": _reg("HKEY_LOCAL_MACHINE", wl, "AutoAdminLogon"),
        "DefaultPassword": _reg("HKEY_LOCAL_MACHINE", wl, "DefaultPassword"),
        "SecureBoot": _reg("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\SecureBoot\State", "UEFISecureBootEnabled"),
        "DataBasePath": _reg("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters", "DataBasePath"),
    }


def _profiles_root() -> str:
    return os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users")


def user_profiles() -> list:
    root = _profiles_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [(n, os.path.join(root, n)) for n in names
            if n.lower() not in _SKIP_PROFILES and os.path.isdir(os.path.join(root, n))]


def scan_unmanaged(profiles=None) -> list:
    """Browsers that ignore our policies and VPN/proxy tools found in user folders."""
    found, seen = [], 0
    for profile, home in profiles if profiles is not None else user_profiles():
        for sub in _SCAN_SUBDIRS:
            base = os.path.join(home, sub)
            base_depth = base.rstrip(os.sep).count(os.sep)
            for dirpath, dirnames, filenames in os.walk(base):
                if dirpath.count(os.sep) - base_depth >= _MAX_DEPTH:
                    dirnames[:] = []
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in ("node_modules", "temp", "cache", "packages")]
                seen += len(filenames)
                if seen > _MAX_SCAN_FILES:
                    return found
                for fn in filenames:
                    low = fn.lower()
                    label = None
                    kind = None
                    if low in UNMANAGED_BROWSERS:
                        label, kind = UNMANAGED_BROWSERS[low], "browser"
                    elif low == "browser.exe" and "yandex" in dirpath.lower():
                        label, kind = "Yandex Browser", "browser"
                    elif low == "firefox.exe" and "tor browser" in dirpath.lower():
                        label, kind = "Tor Browser", "browser"
                    elif low in TUNNEL_APPS and TUNNEL_APPS[low]:
                        label, kind = TUNNEL_APPS[low], "vpn"
                    if label:
                        found.append({"profile": profile, "name": label, "kind": kind, "exe": low,
                                      "path": os.path.join(dirpath, fn)})
    return found


def _ext_name(manifest: dict, ext_dir: str) -> str:
    name = manifest.get("name") or "?"
    m = re.match(r"^__MSG_(\w+)__$", str(name))
    if m:
        locale = manifest.get("default_locale") or "en"
        try:
            with open(os.path.join(ext_dir, "_locales", locale, "messages.json"), "r", encoding="utf-8-sig") as f:
                msgs = json.load(f)
            for key, val in msgs.items():
                if key.lower() == m.group(1).lower():
                    return str(val.get("message") or name)
        except Exception:
            pass
    return str(name)[:80]


def scan_proxy_extensions(profiles=None) -> list:
    """Browser extensions that request the "proxy" permission (VPN/proxy add-ons)."""
    return [{k: v for k, v in e.items() if k != "proxy"} for e in scan_extensions(profiles) if e["proxy"]]


def scan_extensions(profiles=None) -> list:
    """Every installed browser extension (for the allow-list), with its "proxy" permission flag."""
    found = []
    for profile, home in profiles if profiles is not None else user_profiles():
        for browser, rel in CHROMIUM_DATA:
            data_dir = os.path.join(home, rel)
            try:
                entries = os.listdir(data_dir)
            except OSError:
                continue
            candidates = [data_dir] + [os.path.join(data_dir, e) for e in entries
                                       if e == "Default" or e.startswith("Profile ")]
            for prof_dir in candidates:
                ext_root = os.path.join(prof_dir, "Extensions")
                try:
                    ext_ids = os.listdir(ext_root)
                except OSError:
                    continue
                for ext_id in ext_ids:
                    try:
                        versions = sorted(os.listdir(os.path.join(ext_root, ext_id)))
                    except OSError:
                        continue
                    if not versions:
                        continue
                    ext_dir = os.path.join(ext_root, ext_id, versions[-1])
                    try:
                        with open(os.path.join(ext_dir, "manifest.json"), "r", encoding="utf-8-sig") as f:
                            manifest = json.load(f)
                    except Exception:
                        continue
                    perms = [p for p in manifest.get("permissions") or [] if isinstance(p, str)]
                    optional = [p for p in manifest.get("optional_permissions") or [] if isinstance(p, str)]
                    if ext_id.startswith("_") or manifest.get("theme"):
                        continue
                    found.append({"profile": profile, "browser": browser, "id": ext_id, "family": "chromium",
                                  "name": _ext_name(manifest, ext_dir), "required": "proxy" in perms,
                                  "proxy": "proxy" in perms or "proxy" in optional})
        ff_root = os.path.join(home, "AppData", "Roaming", "Mozilla", "Firefox", "Profiles")
        try:
            ff_profiles = os.listdir(ff_root)
        except OSError:
            ff_profiles = []
        for ffp in ff_profiles:
            try:
                with open(os.path.join(ff_root, ffp, "extensions.json"), "r", encoding="utf-8") as f:
                    addons = json.load(f).get("addons") or []
            except Exception:
                continue
            for addon in addons:
                if addon.get("type") not in (None, "extension") or addon.get("location") in ("app-builtin", "app-system-defaults"):
                    continue
                perms = ((addon.get("userPermissions") or {}).get("permissions") or [])
                name = (addon.get("defaultLocale") or {}).get("name") or addon.get("id") or "?"
                found.append({"profile": profile, "browser": "Mozilla Firefox", "id": addon.get("id", ""),
                              "family": "firefox", "name": str(name)[:80], "required": "proxy" in perms,
                              "proxy": "proxy" in perms and addon.get("active", True)})
    return found


def scan_native_vpn(profiles=None) -> list:
    """VPN connections created in Windows settings (a standard account can create them)."""
    files = [(None, os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Network",
                                 "Connections", "Pbk", "rasphone.pbk"))]
    for profile, home in profiles if profiles is not None else user_profiles():
        files.append((profile, os.path.join(home, "AppData", "Roaming", "Microsoft", "Network", "Connections",
                                            "Pbk", "rasphone.pbk")))
    found = []
    for profile, path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for name in re.findall(r"^\[(.+?)\]\s*$", text, re.MULTILINE):
            found.append({"profile": profile or "tous", "name": name.strip()[:80]})
    return found


def scan_user_proxies() -> list:
    """Proxy settings of the logged-in users (ProxyEnable / AutoConfigURL in HKU\\<sid>)."""
    found = []
    try:
        import winreg
    except ImportError:
        return found
    try:
        i = 0
        while True:
            sid = winreg.EnumKey(winreg.HKEY_USERS, i)
            i += 1
            if not sid.startswith("S-1-5-21-") or sid.endswith("_Classes"):
                continue
            path = sid + r"\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            try:
                with winreg.OpenKey(winreg.HKEY_USERS, path) as k:
                    def q(name):
                        try:
                            return winreg.QueryValueEx(k, name)[0]
                        except OSError:
                            return None
                    enabled, server, pac = q("ProxyEnable"), q("ProxyServer"), q("AutoConfigURL")
            except OSError:
                continue
            if enabled == 1 and server:
                found.append({"sid": sid, "kind": "proxy", "value": str(server)[:120]})
            if pac:
                found.append({"sid": sid, "kind": "pac", "value": str(pac)[:200]})
    except OSError:
        pass
    return found


IFEO_ROOT = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options"
NRPT_KEYS = (r"SOFTWARE\Policies\Microsoft\Windows NT\DNSClient\DnsPolicyConfig",
             r"SYSTEM\CurrentControlSet\Services\Dnscache\Parameters\DnsPolicyConfig")


def scan_ifeo_debuggers() -> list:
    """IFEO "Debugger" values not created by HOTS (they hijack the launch of a program)."""
    found = []
    try:
        import winreg
        from .core_appblock import BLOCK_DEBUGGER_CMD
    except ImportError:
        return found
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, IFEO_ROOT)
    except OSError:
        return found
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, name) as k:
                    value = winreg.QueryValueEx(k, "Debugger")[0]
            except OSError:
                continue
            if str(value).strip().strip('"').lower() != BLOCK_DEBUGGER_CMD.lower():
                found.append({"exe": name, "debugger": str(value)[:200]})
    return found


def scan_nrpt() -> int:
    """Name Resolution Policy Table rules: they can send some domains to another DNS server."""
    count = 0
    try:
        import winreg
    except ImportError:
        return 0
    for path in NRPT_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
                count += winreg.QueryInfoKey(k)[0]
        except OSError:
            continue
    return count


def account_changes(win: dict, now: float = None) -> list:
    """New local accounts / new administrators since the previous check (kept 7 days)."""
    now = now or time.time()
    users = {u.get("sid"): u.get("name") for u in win.get("users") or [] if u.get("sid")}
    admins = set(win.get("admins") or [])
    if not users:
        return _read_json(ACCOUNTS_SNAPSHOT, {}).get("changes") or []
    snap = _read_json(ACCOUNTS_SNAPSHOT, {})
    changes = [c for c in snap.get("changes") or [] if now - c.get("at", 0) < 7 * 86400]
    expected = set(snap.get("expected_admins") or [])
    if snap.get("users"):
        for sid, name in users.items():
            if sid not in snap["users"]:
                changes.append({"kind": "new_user", "sid": sid, "name": name, "at": now})
        for sid in admins:
            if sid not in (snap.get("admins") or []) and sid not in expected and sid in users:
                changes.append({"kind": "new_admin", "sid": sid, "name": users.get(sid), "at": now})
    try:
        atomic_write_json(ACCOUNTS_SNAPSHOT, {"users": sorted(users), "admins": sorted(admins), "changes": changes,
                                              "expected_admins": []})
    except Exception:
        logger.exception("cannot save the accounts snapshot")
    return changes


def _expect_admin(sid: str) -> None:
    snap = _read_json(ACCOUNTS_SNAPSHOT, {})
    snap["expected_admins"] = sorted(set(snap.get("expected_admins") or []) | {sid})
    try:
        atomic_write_json(ACCOUNTS_SNAPSHOT, snap)
    except Exception:
        pass


def observed_protections() -> dict:
    """Wanted (saved state) vs real state of each protection."""
    out = {}

    def safe(name, fn):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = {"error": str(e)}

    def hosts_lock():
        from .core_antispy import HostsLockManager
        wanted = HostsLockManager.is_active()
        real = HostsLockManager._real_locked() if wanted else None
        return {"wanted": wanted, "real": real, "drift": bool(wanted and real is False)}

    def netblock():
        from .core_netblock import NetBlockManager, _firewall_profiles_on
        wanted = NetBlockManager.is_active()
        real = (NetBlockManager.rules_present() and _firewall_profiles_on()) if wanted else None
        return {"wanted": wanted, "real": real, "drift": bool(wanted and real is False)}

    def apps():
        from .core_appblock import AppBlockManager
        regressed = [a.exe_name for a in AppBlockManager.list_blocked()
                     if a.enabled and AppBlockManager.is_ifeo_blocked(a.exe_name) is False]
        return {"drift": bool(regressed), "regressed": regressed}

    def doh():
        from .core_doh import DohBlockManager, _load_state
        regressed = [bid for bid, on in _load_state().items() if on and not DohBlockManager.is_blocked(bid)]
        return {"drift": bool(regressed), "regressed": regressed}

    def browsers():
        from .core_browser_policy import BrowserPolicyManager
        st = BrowserPolicyManager.status()
        return {"wanted": st["level"], "drift": bool(st["drift"]),
                "regressed": [b["name"] for b in st["browsers"] if not b["ok"]]}

    def dns():
        from .core_dnslock import DnsLockManager
        st = DnsLockManager.status(include_rules=True)
        return {"wanted": st["active"], "all_ok": st["all_ok"], "any_cf": st["any_cf"], "policy": st["policy"],
                "rules": st["rules"], "adapters": st["adapters"],
                "drift": bool(st["active"] and not (st["all_ok"] and st["rules"]))}

    def applocker():
        from .core_applocker import AppLockerManager
        cur = AppLockerManager.status()
        return {"wanted": cur["mode"], "drift": not AppLockerManager.is_effective(),
                "would_block": len([e for e in cur.get("events") or [] if not e.get("blocked")]),
                "blocked": sum(e.get("blocked", 0) for e in cur.get("events") or [])}

    def privacy():
        from .core_antispy import AntiSpyManager, ITEMS
        user_ids = {it["id"] for it in ITEMS if it["kind"] == "hkcu_reg"}
        regressed, _restored = AntiSpyManager.get_drifted_items()
        regressed = [i for i in regressed if i not in user_ids]
        return {"drift": bool(regressed), "regressed": regressed}

    def hardening():
        from .core_hardening import NetworkHardeningManager
        st = NetworkHardeningManager.status()
        return dict(st, drift=bool(st["drift"]))

    def safesearch():
        from .core_safesearch import SafeSearchManager
        cur = SafeSearchManager.current()
        return {"google": cur["google"], "youtube": cur["youtube"], "bing": cur["bing"], "drift": False}

    for name, fn in (("hosts_lock", hosts_lock), ("netblock", netblock), ("apps", apps), ("doh", doh),
                     ("browsers", browsers), ("dns", dns), ("applocker", applocker), ("privacy", privacy),
                     ("hardening", hardening), ("safesearch", safesearch)):
        safe(name, fn)
    return out


# --- checks ------------------------------------------------------------------------------------------

def _check(cid, category, severity, status, title, detail="", fix=None, items=None, hint=""):
    return {"id": cid, "category": category, "severity": severity, "status": status, "title": title,
            "detail": detail, "fix": fix, "items": items or [], "hint": hint}


def _has_password(user: dict) -> bool:
    return bool(user.get("password_last_set")) or user.get("source") in ("MicrosoftAccount", "AzureAD")


def evaluate(data: dict) -> list:
    """Pure: turns collected data into checks (unit-tested with fake data)."""
    checks = []
    win = data.get("windows") or {}
    reg = data.get("registry") or {}
    users = win.get("users") or []
    admins = set(win.get("admins") or [])
    children = set(data.get("children") or [])
    by_sid = {u.get("sid"): u for u in users}

    # Accounts --------------------------------------------------------------------------------------
    if not users:
        checks.append(_check("accounts.list", "comptes", "medium", "unknown",
                             "Comptes Windows non lus", "La liste des comptes n'a pas pu être obtenue."))
    elif not children:
        checks.append(_check("accounts.children", "comptes", "high", "warn",
                             "Indiquez quel compte Windows utilise votre enfant",
                             "Cochez « compte enfant » dans la liste des comptes ci-dessous : la console vérifiera "
                             "alors qu'il n'est pas administrateur."))
    for sid in sorted(children):
        u = by_sid.get(sid)
        if u is None:
            continue
        if sid in admins:
            checks.append(_check(f"accounts.child_admin.{sid}", "comptes", "critical", "fail",
                                 f"Le compte enfant « {u.get('name')} » est administrateur",
                                 "Un administrateur peut désactiver toutes les protections. Passez ce compte en "
                                 "« utilisateur standard » (effectif à sa prochaine connexion).",
                                 fix={"action": "accounts.make_standard", "params": {"sid": sid},
                                      "label": "Rendre standard"}))
        else:
            checks.append(_check(f"accounts.child_admin.{sid}", "comptes", "critical", "ok",
                                 f"Le compte enfant « {u.get('name')} » est un compte standard"))
    if users:
        guardians = [u for u in users if u.get("enabled") and u.get("sid") in admins
                     and u.get("sid") not in children and not str(u.get("sid", "")).endswith("-500")
                     and _has_password(u)]
        checks.append(_check("accounts.guardian", "comptes", "high", "ok" if guardians else "warn",
                             "Un compte administrateur protégé par mot de passe existe" if guardians
                             else "Aucun compte administrateur (parent) protégé par mot de passe",
                             "" if guardians else "Créez un compte administrateur avec mot de passe pour les "
                                                  "parents, et ne donnez pas son mot de passe à l'enfant."))
        for rid, label in (("-500", "Administrateur intégré"), ("-501", "Invité")):
            u = next((x for x in users if str(x.get("sid", "")).endswith(rid)), None)
            if u is not None:
                checks.append(_check(f"accounts.builtin{rid}", "comptes", "medium",
                                     "warn" if u.get("enabled") else "ok",
                                     f"Compte « {label} » {'activé' if u.get('enabled') else 'désactivé'}",
                                     "À désactiver (Gestion de l'ordinateur > Utilisateurs locaux)." if u.get("enabled") else ""))

    # UAC / logon --------------------------------------------------------------------------------------
    lua = reg.get("EnableLUA")
    checks.append(_check("system.uac", "système", "critical",
                         "unknown" if lua is None else ("ok" if lua == 1 else "fail"),
                         "Contrôle de compte d'utilisateur (UAC) actif" if lua == 1 else "UAC désactivé",
                         "" if lua == 1 else "Sans UAC, les protections sont beaucoup plus faciles à contourner."))
    if reg.get("PromptOnSecureDesktop") == 0:
        checks.append(_check("system.uac_secure_desktop", "système", "low", "warn",
                             "Les demandes UAC ne s'affichent pas sur le bureau sécurisé"))
    if str(reg.get("AutoAdminLogon") or "") == "1" and reg.get("DefaultPassword"):
        checks.append(_check("system.autologon", "système", "high", "fail",
                             "Ouverture de session automatique avec mot de passe en clair",
                             "Le mot de passe Windows est lisible dans le registre par tous les comptes. "
                             "Désactivez l'ouverture de session automatique."))
    # Boot / disk ---------------------------------------------------------------------------------------
    sb = reg.get("SecureBoot")
    checks.append(_check("system.secureboot", "démarrage", "medium",
                         "unknown" if sb is None else ("ok" if sb == 1 else "warn"),
                         "Démarrage sécurisé (Secure Boot) actif" if sb == 1 else "Démarrage sécurisé (Secure Boot) inactif",
                         "" if sb == 1 else "Démarrer sur une clé USB permet de contourner Windows. "
                                            "Activez Secure Boot et un mot de passe dans le BIOS/UEFI."))
    bl = win.get("bitlocker")
    checks.append(_check("system.encryption", "démarrage", "medium",
                         "unknown" if bl is None else ("ok" if bl == 1 else "warn"),
                         "Disque chiffré (BitLocker / chiffrement de l'appareil)" if bl == 1 else
                         ("Chiffrement du disque : état inconnu" if bl is None else "Disque non chiffré"),
                         "" if bl == 1 else "Le chiffrement empêche de modifier Windows depuis un autre système."))
    feats = win.get("features") or {}
    enabled_feats = [n for n, v in feats.items() if v == 1]
    names = {"Microsoft-Windows-Subsystem-Linux": "WSL", "VirtualMachinePlatform": "Plateforme de machine virtuelle",
             "Containers-DisposableClientVM": "Windows Sandbox", "Microsoft-Hyper-V-All": "Hyper-V"}
    checks.append(_check("system.virtualization", "système", "medium", "warn" if enabled_feats else "ok",
                         "Virtualisation activée : " + ", ".join(names.get(n, n) for n in enabled_feats)
                         if enabled_feats else "Pas de WSL / Sandbox / machine virtuelle activés",
                         "Un système virtuel échappe aux filtres de ce PC. Désactivez ces fonctionnalités "
                         "si l'enfant n'en a pas besoin." if enabled_feats else ""))
    db = reg.get("DataBasePath")
    expected = os.path.normcase(os.path.expandvars(r"%SystemRoot%\System32\drivers\etc"))
    if db is not None:
        ok = os.path.normcase(os.path.expandvars(str(db)).rstrip("\\")) == expected.rstrip("\\")
        checks.append(_check("hosts.databasepath", "blocages", "critical", "ok" if ok else "fail",
                             "Emplacement du fichier hosts normal" if ok else "Windows lit un autre fichier hosts !",
                             "" if ok else f"DataBasePath = {db} : les blocages du fichier hosts sont ignorés."))

    # Real state of protections ------------------------------------------------------------------------------
    obs = data.get("observed") or {}
    labels = {"hosts_lock": "Verrou du fichier hosts", "netblock": "Coupure d'Internet", "apps": "Blocage d'applications",
              "doh": "Blocage du DNS sécurisé des navigateurs", "browsers": "Navigateurs verrouillés",
              "dns": "DNS verrouillé", "privacy": "Réglages de confidentialité",
              "applocker": "Mode anti-portable", "hardening": "VPN et proxy de Windows", "safesearch": "SafeSearch"}
    drifted = [labels[k] for k, v in obs.items() if isinstance(v, dict) and v.get("drift")]
    if obs:
        checks.append(_check("protections.real", "blocages", "high", "fail" if drifted else "ok",
                             "Protections annulées : " + ", ".join(drifted) if drifted
                             else "Toutes les protections activées sont réellement en place",
                             "Quelque chose (mise à jour Windows, manipulation) les a désactivées." if drifted else "",
                             fix={"action": "protections.repair", "params": {}, "label": "Réparer"} if drifted else None))
    dns = obs.get("dns") if isinstance(obs.get("dns"), dict) else {}
    if dns and "all_ok" in dns:
        if not dns.get("any_cf"):
            checks.append(_check("dns.filter", "réseau", "medium", "warn", "Filtre DNS familial désactivé",
                                 "Il bloque les sites adultes et malveillants pour tout le PC.",
                                 fix={"action": "cf_family.set", "params": {"enabled": True}, "label": "Activer"}))
        elif not dns.get("all_ok"):
            bad = [a["name"] for a in dns.get("adapters") or [] if not a.get("ok")]
            checks.append(_check("dns.coverage", "réseau", "high", "fail",
                                 "Filtre DNS incomplet : " + ", ".join(bad),
                                 "Une carte réseau (IPv6, partage de connexion du téléphone…) n'utilise pas le filtre.",
                                 fix={"action": "dns_lock.set", "params": {"enabled": True}, "label": "Verrouiller le DNS"}))
        elif not dns.get("wanted"):
            checks.append(_check("dns.lock", "réseau", "medium", "warn", "DNS non verrouillé",
                                 "Le filtre est actif, mais un autre serveur DNS ou un DNS chiffré public reste utilisable.",
                                 fix={"action": "dns_lock.set", "params": {"enabled": True}, "label": "Verrouiller le DNS"}))
        else:
            checks.append(_check("dns.lock", "réseau", "high", "ok", "DNS familial verrouillé sur toutes les cartes"))
    br = obs.get("browsers") if isinstance(obs.get("browsers"), dict) else {}
    if br and "wanted" in br:
        off = br.get("wanted") == "off"
        checks.append(_check("browsers.policies", "navigateurs", "medium", "warn" if off else "ok",
                             "Navigateurs non verrouillés" if off else f"Navigateurs verrouillés (niveau {br.get('wanted')})",
                             "Bloque les extensions VPN/proxy, force SafeSearch et YouTube restreint." if off else "",
                             fix={"action": "browser_policies.set", "params": {"level": "standard", "safesites": False},
                                  "label": "Verrouiller"} if off else None))

    al = obs.get("applocker") if isinstance(obs.get("applocker"), dict) else {}
    if al and "wanted" in al and children:
        mode = al.get("wanted")
        if mode == "off":
            checks.append(_check("bypass.portable", "navigateurs", "high", "warn",
                                 "Programmes portables autorisés pour l'enfant",
                                 "Un navigateur ou un VPN copié dans Téléchargements ou sur une clé USB peut être lancé. "
                                 "Le mode anti-portable l'empêche pour le seul compte enfant (commencez par l'observation).",
                                 fix={"action": "applocker.set", "params": {"mode": "audit"}, "label": "Observer 7 jours"}))
        elif mode == "audit":
            n = al.get("would_block") or 0
            checks.append(_check("bypass.portable", "navigateurs", "medium", "warn",
                                 f"Mode anti-portable en observation : {n} programme(s) seraient bloqués",
                                 "Approuvez les programmes légitimes (onglet Applications), puis activez le blocage.",
                                 fix={"action": "applocker.set", "params": {"mode": "enforce"}, "label": "Activer le blocage"}))
        else:
            checks.append(_check("bypass.portable", "navigateurs", "high", "ok",
                                 "Mode anti-portable actif : seuls les programmes installés peuvent être lancés par l'enfant"))

    hard = obs.get("hardening") if isinstance(obs.get("hardening"), dict) else {}
    if hard and "hide_settings" in hard and not hard.get("hide_settings"):
        checks.append(_check("bypass.native_settings", "réseau", "low", "warn",
                             "Les réglages VPN et proxy de Windows sont accessibles",
                             "Un compte standard peut y créer un VPN ou un proxy pour contourner les filtres.",
                             fix={"action": "network_hardening.set",
                                  "params": {"hide_settings": True, "machine_proxy": True, "block_protocols": False},
                                  "label": "Masquer"}))
    ss = obs.get("safesearch") if isinstance(obs.get("safesearch"), dict) else {}
    if ss and "google" in ss and not ss.get("google"):
        checks.append(_check("bypass.safesearch", "navigateurs", "low", "warn",
                             "SafeSearch non imposé à toutes les applications",
                             "Le verrouillage des navigateurs couvre Chrome, Edge, Brave et Firefox ; le fichier hosts "
                             "l'impose à tous les navigateurs et applications.",
                             fix={"action": "safesearch.set", "params": {"google": True, "youtube": "moderate", "bing": True},
                                  "label": "Imposer SafeSearch"}))

    # Tampering ------------------------------------------------------------------------------------------------
    for acc in win.get("accessibility") or []:
        ok = acc.get("status") == "Valid" and "O=Microsoft Corporation" in (acc.get("signer") or "")
        if not ok:
            checks.append(_check(f"tamper.accessibility.{acc.get('name')}", "altérations", "critical", "fail",
                                 f"Outil d'accessibilité remplacé : {acc.get('name')}",
                                 "Classique pour ouvrir une invite de commandes administrateur depuis l'écran de connexion. "
                                 "Restaurez le fichier (sfc /scannow) et cherchez qui l'a modifié."))
    foreign = data.get("ifeo_foreign") or []
    if foreign:
        critical = [f for f in foreign if f["exe"].lower() in [a.lower() for a in ACCESSIBILITY_EXES]]
        checks.append(_check("tamper.ifeo", "altérations", "critical" if critical else "high", "fail" if critical else "warn",
                             f"{len(foreign)} programme(s) détourné(s) au lancement (IFEO)",
                             "Une valeur « Debugger » remplace le programme par un autre. Certaines sont légitimes "
                             "(ex. Process Explorer à la place du Gestionnaire des tâches) : vérifiez la liste.",
                             items=[{"name": f["exe"], "value": f["debugger"]} for f in foreign[:30]]))
    for change in data.get("account_changes") or []:
        if change["kind"] == "new_admin":
            checks.append(_check(f"tamper.new_admin.{change['sid']}", "altérations", "high", "fail",
                                 f"Nouveau compte administrateur : {change.get('name')}",
                                 "Ajouté hors de la console. Si ce n'est pas vous, retirez-lui les droits."))
        elif change["kind"] == "new_user":
            checks.append(_check(f"tamper.new_user.{change['sid']}", "altérations", "medium", "warn",
                                 f"Nouveau compte Windows : {change.get('name')}",
                                 "S'il est utilisé par un enfant, cochez-le comme « compte enfant »."))
    if data.get("nrpt"):
        checks.append(_check("tamper.nrpt", "altérations", "high", "warn",
                             f"{data['nrpt']} règle(s) DNS par domaine (NRPT)",
                             "Elles peuvent envoyer certains domaines vers un autre serveur DNS, hors du filtre."))

    # Bypass tools ---------------------------------------------------------------------------------------------
    unmanaged = data.get("unmanaged") or []
    checks.append(_check("bypass.unmanaged", "navigateurs", "high", "warn" if unmanaged else "ok",
                         f"{len(unmanaged)} navigateur(s)/VPN non contrôlé(s) trouvé(s)" if unmanaged
                         else "Aucun navigateur portable ni VPN trouvé dans les dossiers des utilisateurs",
                         "Ces programmes ignorent les stratégies des navigateurs ou contournent les filtres. "
                         "Bloquez-les (et supprimez-les)." if unmanaged else "",
                         items=[dict(u, fix={"action": "apps.add",
                                             "params": {"exe_name": u["exe"], "display_name": u["name"], "path": u["path"]},
                                             "label": "Bloquer"}) for u in unmanaged[:50]]))
    exts = data.get("proxy_extensions") or []
    locked = br.get("wanted") in ("standard", "strict") if br else False
    checks.append(_check("bypass.extensions", "navigateurs", "high",
                         "ok" if not exts or locked else "warn",
                         f"{len(exts)} extension(s) VPN/proxy installée(s)" if exts else "Aucune extension VPN/proxy",
                         ("Elles sont désactivées par le verrouillage des navigateurs (Chrome, Edge, Brave)."
                          if locked else "Activez le verrouillage des navigateurs pour les désactiver.") if exts else "",
                         fix=None if (not exts or locked) else {"action": "browser_policies.set",
                                                               "params": {"level": "standard", "safesites": False},
                                                               "label": "Verrouiller les navigateurs"},
                         items=exts[:50]))
    vpns = data.get("native_vpn") or []
    if vpns:
        checks.append(_check("bypass.native_vpn", "réseau", "medium", "warn",
                             f"{len(vpns)} connexion(s) VPN Windows configurée(s)",
                             "Supprimez-les dans Paramètres > Réseau et Internet > VPN si vous ne les connaissez pas.",
                             items=vpns[:30]))
    proxies = data.get("user_proxies") or []
    if proxies:
        checks.append(_check("bypass.proxy", "réseau", "medium", "warn",
                             "Proxy configuré dans une session utilisateur",
                             "Paramètres > Réseau et Internet > Proxy : à désactiver si inconnu.", items=proxies[:20]))
    return checks


def score(checks: list) -> tuple:
    total = 0.0
    for c in checks:
        weight = PENALTY.get(c["severity"], 0)
        total += weight if c["status"] == "fail" else weight / 2 if c["status"] == "warn" else 0
    value = max(0, int(round(100 - total)))
    todo = sorted((c for c in checks if c["status"] in ("fail", "warn")),
                  key=lambda c: (c["status"] != "fail", -PENALTY.get(c["severity"], 0)))
    return value, [{"id": c["id"], "title": c["title"], "fix": c["fix"]} for c in todo[:3]]


def build_report(with_observed: bool = True) -> dict:
    started = time.time()
    data = {"children": child_sids()}
    steps = (("windows", collect_windows), ("registry", collect_registry),
             ("unmanaged", scan_unmanaged), ("extensions", scan_extensions),
             ("native_vpn", scan_native_vpn), ("user_proxies", scan_user_proxies),
             ("ifeo_foreign", scan_ifeo_debuggers), ("nrpt", scan_nrpt))
    if with_observed:
        steps += (("observed", observed_protections),)
    errors = {}
    for key, fn in steps:
        try:
            data[key] = fn()
        except Exception as e:
            logger.exception("compliance step %s failed", key)
            errors[key] = str(e)
    data["proxy_extensions"] = [e for e in data.get("extensions") or [] if e.get("proxy")]
    try:
        data["account_changes"] = account_changes(data.get("windows") or {})
    except Exception:
        logger.exception("account changes check failed")
    checks = evaluate(data)
    value, todo = score(checks)
    win = data.get("windows") or {}
    admins = set(win.get("admins") or [])
    children = set(data["children"])
    accounts = [{"sid": u.get("sid"), "name": u.get("name"), "full_name": u.get("full_name") or "",
                 "enabled": bool(u.get("enabled")), "admin": u.get("sid") in admins,
                 "child": u.get("sid") in children, "builtin": str(u.get("sid", "")).endswith(("-500", "-501", "-503", "-504")),
                 "source": u.get("source") or "", "last_logon": u.get("last_logon")}
                for u in win.get("users") or []]
    report = {"score": value, "todo": todo, "checks": checks, "accounts": accounts, "errors": errors,
              "extensions": [{k: e.get(k) for k in ("browser", "family", "id", "name", "proxy", "profile")}
                             for e in data.get("extensions") or []][:300],
              "checked_at": time.time(), "duration_s": round(time.time() - started, 1),
              "dns": (data.get("observed") or {}).get("dns")}
    try:
        atomic_write_json(REPORT_FILE, report)
    except Exception:
        logger.exception("cannot save the compliance report")
    return report


# --- accounts ------------------------------------------------------------------------------------------

def _validate_sid(sid) -> str:
    if not isinstance(sid, str) or not SID_RE.match(sid):
        raise ValueError("SID invalide")
    if sid.endswith(("-500", "-501", "-503", "-504")):
        raise ValueError("Compte système intégré : non modifiable ici.")
    return sid


def make_standard(sid: str) -> None:
    """Remove a CHILD account from Administrators (kept in Users). Guards against lock-out."""
    sid = _validate_sid(sid)
    if sid not in child_sids():
        raise ValueError("Marquez d'abord ce compte comme « compte enfant ».")
    win = collect_windows()
    admins = set(win.get("admins") or [])
    if sid not in admins:
        return
    users = win.get("users") or []
    others = [u for u in users if u.get("enabled") and u.get("sid") in admins and u.get("sid") != sid
              and u.get("sid") not in child_sids() and not str(u.get("sid", "")).endswith("-500") and _has_password(u)]
    if not others:
        raise ValueError("Aucun autre compte administrateur protégé par mot de passe : créez-en un d'abord, "
                         "sinon plus personne ne pourrait administrer ce PC.")
    _set_admin(sid, False)


def make_admin(sid: str) -> None:
    sid = _validate_sid(sid)
    _expect_admin(sid)  # done from the console: not reported as tampering
    _set_admin(sid, True)


def _set_admin(sid: str, admin: bool) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows uniquement")
    member = f"(New-Object System.Security.Principal.SecurityIdentifier('{sid}'))"
    if admin:
        script = (f"$ErrorActionPreference='Stop'; try {{ Add-LocalGroupMember -SID '{ADMINS_SID}' -Member {member} }} "
                  f"catch {{ if ($_.FullyQualifiedErrorId -notmatch 'MemberExists') {{ "
                  f"$n=(New-Object System.Security.Principal.SecurityIdentifier('{ADMINS_SID}')).Translate([System.Security.Principal.NTAccount]).Value.Split('\\')[-1]; "
                  f"([ADSI]\"WinNT://./$n,group\").Add('WinNT://{sid}') }} }}; 'OK'")
    else:
        script = (f"$ErrorActionPreference='Stop'; try {{ Add-LocalGroupMember -SID '{USERS_SID}' -Member {member} }} catch {{}}; "
                  f"try {{ Remove-LocalGroupMember -SID '{ADMINS_SID}' -Member {member} }} catch {{ "
                  f"$n=(New-Object System.Security.Principal.SecurityIdentifier('{ADMINS_SID}')).Translate([System.Security.Principal.NTAccount]).Value.Split('\\')[-1]; "
                  f"([ADSI]\"WinNT://./$n,group\").Remove('WinNT://{sid}') }}; 'OK'")
    r = _powershell(script, timeout=60)
    out = (r.stdout or "").strip().splitlines() if r else []
    if r is None or r.returncode != 0 or not out or out[-1] != "OK":
        raise RuntimeError(((r.stderr or r.stdout) if r else "PowerShell indisponible").strip()[:300])


# --- auto-repair ------------------------------------------------------------------------------------------

def repair_protections() -> list:
    """Re-apply every protection that is wanted but no longer in place. Returns what was repaired."""
    repaired = []

    def attempt(label, fn):
        try:
            if fn():
                repaired.append(label)
        except Exception:
            logger.exception("repair %s failed", label)

    def hosts_lock():
        from .core_antispy import HostsLockManager
        return HostsLockManager.is_active() and HostsLockManager._real_locked() is False and HostsLockManager.enable()

    def netblock():
        from .core_netblock import NetBlockManager
        return NetBlockManager.check_drift() == "restored"

    def apps():
        from .core_appblock import AppBlockManager
        fixed = False
        for a in AppBlockManager.list_blocked():
            if a.enabled and AppBlockManager.is_ifeo_blocked(a.exe_name) is False:
                fixed = AppBlockManager.set_enabled(a.exe_name, True) or fixed
        return fixed

    def doh():
        from .core_doh import DohBlockManager, _load_state
        fixed = False
        for bid, on in _load_state().items():
            if on and not DohBlockManager.is_blocked(bid):
                fixed = DohBlockManager.enable(bid) or fixed
        return fixed

    def browsers():
        from .core_browser_policy import BrowserPolicyManager
        return BrowserPolicyManager.repair()

    def dns():
        from .core_dnslock import DnsLockManager
        return bool(DnsLockManager.reapply())

    def applocker():
        from .core_applocker import AppLockerManager
        return AppLockerManager.repair()

    def hardening():
        from .core_hardening import NetworkHardeningManager
        return NetworkHardeningManager.repair()

    def privacy():
        from .core_antispy import AntiSpyManager, ITEMS
        user_ids = {it["id"] for it in ITEMS if it["kind"] == "hkcu_reg"}
        regressed, _ = AntiSpyManager.get_drifted_items()
        fixed = False
        for item_id in regressed:
            if item_id not in user_ids:
                fixed = AntiSpyManager.enable_item(item_id) or fixed
        return fixed

    for label, fn in (("verrou du fichier hosts", hosts_lock), ("coupure d'Internet", netblock),
                      ("blocage d'applications", apps), ("DNS sécurisé des navigateurs", doh),
                      ("navigateurs verrouillés", browsers), ("DNS verrouillé", dns),
                      ("mode anti-portable", applocker), ("VPN et proxy de Windows", hardening),
                      ("confidentialité", privacy)):
        attempt(label, fn)
    if repaired:
        logger.info("auto-repair: %s", repaired)
    return repaired
