"""Browser lockdown ("navigateurs verrouillés"): machine policies under HKLM\\SOFTWARE\\Policies.

Applied to every supported browser even when it is not detected: per-user installs and portable
copies of these browsers read the same machine policies. Every value we touch is snapshotted
first, so turning the feature off restores exactly what was there before (including policies set
by another tool). Levels:

  standard  extensions that require the "proxy" permission are blocked (VPN/proxy add-ons), proxy
            forced to "direct", Google SafeSearch, YouTube restricted (moderate), Bing SafeSearch
            strict, guest mode off, Brave Tor/VPN off, Firefox built-in VPN off and proxy locked.
  strict    standard + YouTube strict + private browsing (incognito / InPrivate) disabled.
  safesites (option) Chrome's own adult-content URL filter; it sends visited URLs to Google
            SafeSearch for classification, hence off by default.

Sources: Chromium policy_definitions (ExtensionSettings, ProxySettings, ForceGoogleSafeSearch,
ForceYouTubeRestrict, BrowserGuestModeEnabled, IncognitoModeAvailability, SafeSitesFilterBehavior),
Microsoft Edge policies (ForceBingSafeSearch, InPrivateModeAvailability), Brave Group Policy
(TorDisabled, BraveVPNDisabled), Firefox policies (Proxy, PrivateBrowsingModeAvailability,
IPProtectionAvailable — Firefox 149+).
"""

import json
import logging
import os
import re
import threading

from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.browser_policy")

STATE_FILE = os.path.join(PROGRAMDATA_DIR, "browser_policy_state.json")
LEVELS = ("off", "standard", "strict")

REG_SZ, REG_DWORD, REG_MULTI_SZ = 1, 4, 7
CHROMIUM_ID_RE = re.compile(r"^[a-p]{32}$")
FIREFOX_ID_RE = re.compile(r"^(\{[0-9a-fA-F-]{36}\}|[\w.+\-]{1,80}@[\w.\-]{1,80})$")

CHROMIUM = (
    ("chrome", "Google Chrome", r"SOFTWARE\Policies\Google\Chrome"),
    ("edge", "Microsoft Edge", r"SOFTWARE\Policies\Microsoft\Edge"),
    ("brave", "Brave", r"SOFTWARE\Policies\BraveSoftware\Brave"),
    ("vivaldi", "Vivaldi", r"SOFTWARE\Policies\Vivaldi"),
    ("chromium", "Chromium", r"SOFTWARE\Policies\Chromium"),
)
FIREFOX = ("firefox", "Mozilla Firefox", r"SOFTWARE\Policies\Mozilla\Firefox")
BROWSER_NAMES = {bid: name for bid, name, _path in CHROMIUM + (FIREFOX,)}
_PATH_BROWSER = {path: bid for bid, _name, path in CHROMIUM + (FIREFOX,)}
_PATH_BROWSER[FIREFOX[2] + r"\Proxy"] = "firefox"


# --- registry access (module-level so tests can replace them) -----------------------------------

def reg_read(path: str, name: str):
    """(exists, type, data) for HKLM\\path\\name."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ) as key:
            data, vtype = winreg.QueryValueEx(key, name)
            return True, vtype, data
    except OSError:
        return False, None, None


def reg_write(path: str, name: str, vtype: int, data) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, vtype, data)


def reg_delete(path: str, name: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


# --- desired values ------------------------------------------------------------------------------

def _merge_extension_settings(existing) -> str:
    """Add "proxy" to ExtensionSettings["*"].blocked_permissions, keeping anything already set."""
    try:
        data = json.loads(existing) if isinstance(existing, str) and existing.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    star = data.get("*") if isinstance(data.get("*"), dict) else {}
    blocked = star.get("blocked_permissions") if isinstance(star.get("blocked_permissions"), list) else []
    if "proxy" not in blocked:
        blocked = blocked + ["proxy"]
    star["blocked_permissions"] = blocked
    data["*"] = star
    return json.dumps(data, separators=(",", ":"))


def chromium_proxy(proxy) -> dict:
    """ProxySettings for a trusted network's proxy (school) or "direct" everywhere else."""
    mode = (proxy or {}).get("mode", "none")
    if mode == "auto":
        return {"ProxyMode": "auto_detect"}
    if mode == "pac":
        return {"ProxyMode": "pac_script", "ProxyPacUrl": proxy["pac_url"]}
    if mode == "fixed":
        return {"ProxyMode": "fixed_servers", "ProxyServer": proxy["server"]}
    return {"ProxyMode": "direct"}


def clean_extensions(raw) -> dict:
    """{"mode": "proxy"|"allowlist", "chromium": [ids], "firefox": [ids]} (ids validated)."""
    raw = raw if isinstance(raw, dict) else {}
    mode = "allowlist" if raw.get("mode") == "allowlist" else "proxy"
    chromium = sorted({i for i in raw.get("chromium") or [] if isinstance(i, str) and CHROMIUM_ID_RE.match(i)})
    firefox = sorted({i for i in raw.get("firefox") or [] if isinstance(i, str) and FIREFOX_ID_RE.match(i)})
    return {"mode": mode, "chromium": chromium[:200], "firefox": firefox[:200]}


def desired_values(level: str, safesites: bool = False, originals: dict = None, proxy: dict = None,
                   extensions: dict = None) -> dict:
    """{"path|name": (type, data)} for a level. ExtensionSettings is merged with the original value.
    `proxy`: proxy of the trusted network the PC is on (None = no proxy allowed)."""
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    out = {}
    if level == "off":
        return out
    originals = originals or {}
    strict = level == "strict"
    ext = clean_extensions(extensions)

    def put(path, name, vtype, data):
        out[f"{path}|{name}"] = (vtype, data)

    for bid, _name, path in CHROMIUM:
        orig = originals.get(f"{path}|ExtensionSettings")
        if orig is None:
            exists, vtype, data = reg_read(path, "ExtensionSettings")
            base = data if exists and vtype == REG_SZ else ""
        else:
            base = orig.get("data") if orig.get("exists") and orig.get("type") == REG_SZ else ""
        settings = json.loads(_merge_extension_settings(base))
        if ext["mode"] == "allowlist":
            # Only the extensions approved by the parent can be installed or kept.
            settings["*"]["installation_mode"] = "blocked"
            for ext_id in ext["chromium"]:
                entry = settings.get(ext_id) if isinstance(settings.get(ext_id), dict) else {}
                entry["installation_mode"] = "allowed"
                settings[ext_id] = entry
        put(path, "ExtensionSettings", REG_SZ, json.dumps(settings, separators=(",", ":")))
        put(path, "ProxySettings", REG_SZ, json.dumps(chromium_proxy(proxy)))
        put(path, "ForceGoogleSafeSearch", REG_DWORD, 1)
        put(path, "ForceYouTubeRestrict", REG_DWORD, 2 if strict else 1)
        put(path, "BrowserGuestModeEnabled", REG_DWORD, 0)
        if bid == "edge":
            put(path, "ForceBingSafeSearch", REG_DWORD, 2)
            if strict:
                put(path, "InPrivateModeAvailability", REG_DWORD, 1)
        elif strict:
            put(path, "IncognitoModeAvailability", REG_DWORD, 1)
        if bid == "brave":
            put(path, "TorDisabled", REG_DWORD, 1)
            put(path, "BraveVPNDisabled", REG_DWORD, 1)
        if bid == "chrome" and safesites:
            put(path, "SafeSitesFilterBehavior", REG_DWORD, 1)

    fpath = FIREFOX[2]
    put(fpath, "IPProtectionAvailable", REG_DWORD, 0)
    pmode = (proxy or {}).get("mode", "none")
    if pmode == "auto":
        put(fpath + r"\Proxy", "Mode", REG_SZ, "autoDetect")
    elif pmode == "pac":
        put(fpath + r"\Proxy", "Mode", REG_SZ, "autoConfig")
        put(fpath + r"\Proxy", "AutoConfigURL", REG_SZ, proxy["pac_url"])
    elif pmode == "fixed":
        put(fpath + r"\Proxy", "Mode", REG_SZ, "manual")
        put(fpath + r"\Proxy", "HTTPProxy", REG_SZ, proxy["server"])
        put(fpath + r"\Proxy", "UseHTTPProxyForAllProtocols", REG_DWORD, 1)
    else:
        put(fpath + r"\Proxy", "Mode", REG_SZ, "none")
    put(fpath + r"\Proxy", "Locked", REG_DWORD, 1)
    if strict:
        put(fpath, "PrivateBrowsingModeAvailability", REG_DWORD, 1)
    if ext["mode"] == "allowlist":
        ff = {"*": {"installation_mode": "blocked"}}
        ff.update({ext_id: {"installation_mode": "allowed"} for ext_id in ext["firefox"]})
        put(fpath, "ExtensionSettings", REG_MULTI_SZ, [json.dumps(ff, separators=(",", ":"))])
    return out


# --- manager ------------------------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    atomic_write_json(STATE_FILE, state)


def _split(key: str):
    path, _, name = key.rpartition("|")
    return path, name


def _same(vtype, data, want_type, want_data) -> bool:
    if vtype != want_type:
        return False
    if want_type == REG_MULTI_SZ:
        try:
            return [json.loads(x) for x in data or []] == [json.loads(x) for x in want_data]
        except (TypeError, ValueError):
            return False
    if want_type == REG_SZ and want_data.startswith("{"):
        try:
            return json.loads(data) == json.loads(want_data)
        except (TypeError, ValueError):
            return False
    return data == want_data


class BrowserPolicyManager:
    last_error: str = ""
    _lock = threading.RLock()

    @staticmethod
    def current() -> dict:
        state = _load_state()
        level = state.get("level") if state.get("level") in LEVELS else "off"
        return {"level": level, "safesites": bool(state.get("safesites"))}

    @staticmethod
    def apply(level: str, safesites: bool = False) -> bool:
        """Switch to a level; values no longer wanted are restored to their original state."""
        with BrowserPolicyManager._lock:
            BrowserPolicyManager.last_error = ""
            if level not in LEVELS:
                BrowserPolicyManager.last_error = f"unknown level {level}"
                return False
            state = _load_state()
            originals = dict(state.get("originals") or {})
            applied = dict(state.get("applied") or {})
            proxy = state.get("network_proxy")
            extensions = state.get("extensions")
            try:
                wanted = desired_values(level, safesites, originals, proxy, extensions)
                for key in list(applied):
                    if key not in wanted:
                        BrowserPolicyManager._restore(key, originals.pop(key, None))
                        applied.pop(key, None)
                for key, (vtype, data) in wanted.items():
                    path, name = _split(key)
                    if key not in originals:
                        exists, otype, odata = reg_read(path, name)
                        if isinstance(odata, bytes):  # REG_BINARY: keep it JSON-serialisable
                            originals[key] = {"exists": exists, "type": otype, "data_hex": odata.hex()}
                        else:
                            originals[key] = {"exists": exists, "type": otype, "data": odata}
                    reg_write(path, name, vtype, data)
                    applied[key] = [vtype, data]
            except Exception as e:
                BrowserPolicyManager.last_error = str(e)
                logger.exception("browser policies: apply(%s) failed", level)
                _save_state({"level": level, "safesites": bool(safesites), "originals": originals, "applied": applied,
                             "network_proxy": proxy, "extensions": extensions,
                             "child_private_off": state.get("child_private_off"), "child_applied": state.get("child_applied")})
                return False
            if level == "off":
                for key in list(originals):
                    BrowserPolicyManager._restore(key, originals.pop(key))
                applied = {}
            _save_state({"level": level, "safesites": bool(safesites) and level != "off",
                         "originals": originals, "applied": applied, "network_proxy": proxy, "extensions": extensions,
                         "child_private_off": state.get("child_private_off"), "child_applied": state.get("child_applied")})
            logger.info("browser policies: level %s (safesites=%s)", level, safesites)
            return True

    @staticmethod
    def _restore(key: str, original) -> None:
        path, name = _split(key)
        if original and original.get("exists"):
            data = bytes.fromhex(original["data_hex"]) if "data_hex" in original else original.get("data")
            reg_write(path, name, original.get("type") or REG_SZ, data)
        else:
            reg_delete(path, name)

    @staticmethod
    def status() -> dict:
        """Wanted vs observed values, per browser."""
        state = _load_state()
        cur = BrowserPolicyManager.current()
        applied = state.get("applied") or {}
        per_browser = {bid: {"id": bid, "name": name, "ok": True, "drift": []}
                       for bid, name in BROWSER_NAMES.items()}
        for key, (vtype, data) in applied.items():
            path, name = _split(key)
            exists, otype, odata = reg_read(path, name)
            if not exists or not _same(otype, odata, vtype, data):
                bid = _PATH_BROWSER.get(path)
                if bid in per_browser:
                    per_browser[bid]["ok"] = False
                    per_browser[bid]["drift"].append(name)
        browsers = list(per_browser.values())
        drift = sum(len(b["drift"]) for b in browsers)
        return {"level": cur["level"], "safesites": cur["safesites"], "browsers": browsers, "drift": drift,
                "network_proxy": state.get("network_proxy"), "extensions": clean_extensions(state.get("extensions")),
                "child_private_off": bool(state.get("child_private_off")),
                "child_applied": state.get("child_applied") or []}

    @staticmethod
    def set_network_proxy(proxy) -> bool:
        """Called by the network watcher: proxy of the trusted network the PC is on, or None."""
        with BrowserPolicyManager._lock:
            state = _load_state()
            if (state.get("network_proxy") or None) == (proxy or None):
                return False
            state["network_proxy"] = proxy or None
            _save_state(state)
            cur = BrowserPolicyManager.current()
            if cur["level"] == "off":
                return False
            return BrowserPolicyManager.apply(cur["level"], cur["safesites"])

    @staticmethod
    def set_extensions(raw) -> bool:
        """Extensions: "proxy" (VPN/proxy extensions blocked) or "allowlist" (only approved ones)."""
        with BrowserPolicyManager._lock:
            state = _load_state()
            state["extensions"] = clean_extensions(raw)
            _save_state(state)
            cur = BrowserPolicyManager.current()
            if cur["level"] == "off":
                return True
            return BrowserPolicyManager.apply(cur["level"], cur["safesites"])

    # -- per-child policies (HKEY_USERS\<child SID>): parents are not affected ----------------------------

    CHILD_VALUES = (
        (r"Software\Policies\Google\Chrome", "IncognitoModeAvailability"),
        (r"Software\Policies\BraveSoftware\Brave", "IncognitoModeAvailability"),
        (r"Software\Policies\Vivaldi", "IncognitoModeAvailability"),
        (r"Software\Policies\Chromium", "IncognitoModeAvailability"),
        (r"Software\Policies\Microsoft\Edge", "InPrivateModeAvailability"),
        (r"Software\Policies\Mozilla\Firefox", "PrivateBrowsingModeAvailability"),
    )

    @staticmethod
    def set_child_private_off(enabled: bool) -> bool:
        with BrowserPolicyManager._lock:
            state = _load_state()
            state["child_private_off"] = bool(enabled)
            _save_state(state)
        BrowserPolicyManager.apply_child_policies()
        return True

    @staticmethod
    def apply_child_policies(children=None) -> bool:
        """Write/remove the per-child values in every loaded child profile (called every 30 s)."""
        import winreg
        if children is None:
            from .compliance import child_sids
            children = child_sids()
        with BrowserPolicyManager._lock:
            state = _load_state()
            wanted = set(children) if state.get("child_private_off") else set()
            done = set(state.get("child_applied") or [])
            changed = False
            for sid in sorted(wanted | done):
                try:
                    winreg.OpenKey(winreg.HKEY_USERS, sid).Close()
                except OSError:
                    continue  # profile not loaded (child not logged in): retried later
                for path, name in BrowserPolicyManager.CHILD_VALUES:
                    full = f"{sid}\\{path}"
                    try:
                        if sid in wanted:
                            with winreg.CreateKeyEx(winreg.HKEY_USERS, full, 0, winreg.KEY_SET_VALUE) as k:
                                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 1)
                        else:
                            with winreg.OpenKey(winreg.HKEY_USERS, full, 0, winreg.KEY_SET_VALUE) as k:
                                winreg.DeleteValue(k, name)
                    except OSError:
                        pass
                if sid in wanted and sid not in done:
                    done.add(sid)
                    changed = True
                elif sid not in wanted and sid in done:
                    done.discard(sid)
                    changed = True
            if changed:
                state["child_applied"] = sorted(done)
                _save_state(state)
            return changed

    @staticmethod
    def repair() -> bool:
        """Re-apply the current level if some value was changed or removed."""
        cur = BrowserPolicyManager.current()
        if cur["level"] == "off" or not BrowserPolicyManager.status()["drift"]:
            return False
        return BrowserPolicyManager.apply(cur["level"], cur["safesites"])
