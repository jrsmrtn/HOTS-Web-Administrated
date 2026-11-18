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
import threading

from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.browser_policy")

STATE_FILE = os.path.join(PROGRAMDATA_DIR, "browser_policy_state.json")
LEVELS = ("off", "standard", "strict")

REG_SZ, REG_DWORD = 1, 4

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


def desired_values(level: str, safesites: bool = False, originals: dict = None) -> dict:
    """{"path|name": (type, data)} for a level. ExtensionSettings is merged with the original value."""
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    out = {}
    if level == "off":
        return out
    originals = originals or {}
    strict = level == "strict"

    def put(path, name, vtype, data):
        out[f"{path}|{name}"] = (vtype, data)

    for bid, _name, path in CHROMIUM:
        orig = originals.get(f"{path}|ExtensionSettings")
        if orig is None:
            exists, vtype, data = reg_read(path, "ExtensionSettings")
            base = data if exists and vtype == REG_SZ else ""
        else:
            base = orig.get("data") if orig.get("exists") and orig.get("type") == REG_SZ else ""
        put(path, "ExtensionSettings", REG_SZ, _merge_extension_settings(base))
        put(path, "ProxySettings", REG_SZ, json.dumps({"ProxyMode": "direct"}))
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
    put(fpath + r"\Proxy", "Mode", REG_SZ, "none")
    put(fpath + r"\Proxy", "Locked", REG_DWORD, 1)
    if strict:
        put(fpath, "PrivateBrowsingModeAvailability", REG_DWORD, 1)
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
            try:
                wanted = desired_values(level, safesites, originals)
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
                _save_state({"level": level, "safesites": bool(safesites), "originals": originals, "applied": applied})
                return False
            if level == "off":
                for key in list(originals):
                    BrowserPolicyManager._restore(key, originals.pop(key))
                applied = {}
            _save_state({"level": level, "safesites": bool(safesites) and level != "off",
                         "originals": originals, "applied": applied})
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
        return {"level": cur["level"], "safesites": cur["safesites"], "browsers": browsers, "drift": drift}

    @staticmethod
    def repair() -> bool:
        """Re-apply the current level if some value was changed or removed."""
        cur = BrowserPolicyManager.current()
        if cur["level"] == "off" or not BrowserPolicyManager.status()["drift"]:
            return False
        return BrowserPolicyManager.apply(cur["level"], cur["safesites"])
