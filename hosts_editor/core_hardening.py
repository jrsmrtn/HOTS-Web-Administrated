"""Windows built-in VPN / proxy: the bypasses that need no installation.

  hide_settings      hide Settings > Network > VPN and Proxy (SettingsPageVisibility)
  machine_proxy      proxy settings per machine instead of per user (a standard account can no longer
                     set its own proxy; ProxySettingsPerUser = 0)
  block_protocols    firewall: block IKEv2/IPsec (UDP 500/4500), L2TP (UDP 1701), PPTP (TCP 1723 + GRE).
                     Also blocks the parents' VPN of these types; SSTP / OpenVPN-TCP over 443 still pass.

These settings apply to the whole PC (all accounts). Every value is snapshotted and restored exactly.
"""

import json
import logging
import os
import threading

from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.hardening")

STATE_FILE = os.path.join(PROGRAMDATA_DIR, "hardening_state.json")
EXPLORER_POLICY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer"
INET_POLICY = r"SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings"
HIDDEN_PAGES = ("network-vpn", "network-proxy")
VPN_RULES = (
    ("HOTS VPN Block IKE", ["protocol=UDP", "remoteport=500,4500"]),
    ("HOTS VPN Block L2TP", ["protocol=UDP", "remoteport=1701"]),
    ("HOTS VPN Block PPTP", ["protocol=TCP", "remoteport=1723"]),
    ("HOTS VPN Block GRE", ["protocol=47"]),
)
OPTIONS = ("hide_settings", "machine_proxy", "block_protocols")


def page_visibility(existing) -> str:
    """Pure: merge our hidden pages into an existing SettingsPageVisibility value."""
    existing = (existing or "").strip()
    if existing.lower().startswith("showonly:"):
        return existing  # already a whitelist of pages: VPN/proxy are hidden unless listed
    pages = [p for p in existing[5:].split(";") if p] if existing.lower().startswith("hide:") else []
    for page in HIDDEN_PAGES:
        if page not in pages:
            pages.append(page)
    return "hide:" + ";".join(pages)


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class NetworkHardeningManager:
    last_error: str = ""
    _lock = threading.RLock()

    @staticmethod
    def current() -> dict:
        state = _load_state()
        return {k: bool(state.get(k)) for k in OPTIONS}

    @staticmethod
    def apply(hide_settings: bool, machine_proxy: bool, block_protocols: bool) -> bool:
        from .core_antispy import _is_admin
        from .core_browser_policy import REG_DWORD, REG_SZ, reg_delete, reg_read, reg_write
        from .core_netblock import _run_netsh
        with NetworkHardeningManager._lock:
            NetworkHardeningManager.last_error = ""
            if not _is_admin():
                NetworkHardeningManager.last_error = "Droits administrateur requis."
                return False
            state = _load_state()
            originals = state.get("originals") or {}

            def snapshot(path, name):
                key = f"{path}|{name}"
                if key not in originals:
                    exists, vtype, data = reg_read(path, name)
                    originals[key] = {"exists": exists, "type": vtype, "data": data}
                return originals[key]

            def restore(path, name):
                orig = originals.pop(f"{path}|{name}", None)
                if orig is None:
                    return
                if orig.get("exists"):
                    reg_write(path, name, orig.get("type") or REG_SZ, orig.get("data"))
                else:
                    reg_delete(path, name)

            try:
                if hide_settings:
                    orig = snapshot(EXPLORER_POLICY, "SettingsPageVisibility")
                    reg_write(EXPLORER_POLICY, "SettingsPageVisibility", REG_SZ,
                              page_visibility(orig.get("data") if orig.get("exists") else ""))
                else:
                    restore(EXPLORER_POLICY, "SettingsPageVisibility")
                if machine_proxy:
                    snapshot(INET_POLICY, "ProxySettingsPerUser")
                    reg_write(INET_POLICY, "ProxySettingsPerUser", REG_DWORD, 0)
                else:
                    restore(INET_POLICY, "ProxySettingsPerUser")
            except OSError as e:
                NetworkHardeningManager.last_error = str(e)
                atomic_write_json(STATE_FILE, dict(state, originals=originals))
                return False
            for name, _args in VPN_RULES:
                _run_netsh(["advfirewall", "firewall", "delete", "rule", f"name={name}"])
            if block_protocols:
                for name, args in VPN_RULES:
                    r = _run_netsh(["advfirewall", "firewall", "add", "rule", f"name={name}", "dir=out",
                                    "action=block", *args, "profile=any", "enable=yes"])
                    if r is None or r.returncode != 0:
                        NetworkHardeningManager.last_error = f"Règle {name} non créée."
            atomic_write_json(STATE_FILE, {"hide_settings": hide_settings, "machine_proxy": machine_proxy,
                                           "block_protocols": block_protocols, "originals": originals})
            return not NetworkHardeningManager.last_error

    @staticmethod
    def disable_all() -> bool:
        return NetworkHardeningManager.apply(False, False, False)

    @staticmethod
    def status() -> dict:
        from .core_browser_policy import reg_read
        cur = NetworkHardeningManager.current()
        drift = []
        if cur["hide_settings"]:
            exists, _t, data = reg_read(EXPLORER_POLICY, "SettingsPageVisibility")
            if not exists or not str(data).lower().startswith(("hide:", "showonly:")):
                drift.append("hide_settings")
        if cur["machine_proxy"]:
            exists, _t, data = reg_read(INET_POLICY, "ProxySettingsPerUser")
            if not exists or data != 0:
                drift.append("machine_proxy")
        cur["drift"] = drift
        return cur

    @staticmethod
    def repair() -> bool:
        cur = NetworkHardeningManager.status()
        if not cur["drift"]:
            return False
        return NetworkHardeningManager.apply(cur["hide_settings"], cur["machine_proxy"], cur["block_protocols"])
