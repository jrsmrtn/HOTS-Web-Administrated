""""DNS verrouillé": Cloudflare Family enforced on every network adapter, and no way around it.

On top of the existing Cloudflare Family switch (IPv4, dns_utils.py), the lock:
  1. sets Cloudflare Family for IPv6 too (the box's IPv6 DNS was left unfiltered);
  2. sets the DNS Client policy NameServer (meant to override per-adapter and DHCP settings;
     to be confirmed on Windows Home, see the audit T2) — harmless where it is ignored;
  3. blocks outbound DNS (53) and DNS-over-TLS (853) to every address except Cloudflare Family;
  4. blocks HTTPS (443, TCP + UDP/QUIC) to the IPs of well-known public DoH resolvers;
  5. (done by the caller, hosts file) blocks the DoH resolver host names: blocklists/doh_bypass.txt.
The remote agent re-applies it every few minutes, so a new adapter (USB phone tethering, another
Wi-Fi) is covered quickly. Everything is restored on disable (DNS backups + policy snapshot).

Side effects: Wi-Fi with a captive portal (hotel, train) may not open its login page; not for
work PCs that need an internal DNS server.
"""

import ipaddress
import json
import logging
import os
import re
import threading

from .remote.netutil import carve_ranges
from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.dnslock")

CF_V4 = ("1.1.1.3", "1.0.0.3")
CF_V6 = ("2606:4700:4700::1113", "2606:4700:4700::1003")
STATE_FILE = os.path.join(PROGRAMDATA_DIR, "dnslock_state.json")
POLICY_PATH = r"SOFTWARE\Policies\Microsoft\Windows NT\DNSClient"
POLICY_NAME = "NameServer"
POLICY_VALUE = " ".join(CF_V4 + CF_V6)
DOH_LIST = "doh_bypass.txt"
_TCPIP6_IFACES_KEY = r"SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters\Interfaces"

# Everything a DNS query could be sent to (loopback left alone for local resolvers).
_V4_ALL = "0.0.0.0-126.255.255.255,128.0.0.0-255.255.255.255"
_V6_ALL = "2000::/3,fc00::/7,fe80::/10"

# Public DoH resolvers (dedicated IPs only: shared CDN addresses would block ordinary websites).
DOH_V4 = (
    "8.8.8.8", "8.8.4.4",                                                     # Google
    "1.1.1.1", "1.0.0.1", "1.1.1.2", "1.0.0.2",                               # Cloudflare (unfiltered / malware)
    "9.9.9.9", "149.112.112.112", "9.9.9.10", "149.112.112.10", "9.9.9.11", "149.112.112.11",  # Quad9
    "208.67.222.222", "208.67.220.220", "208.67.222.123", "208.67.220.123",  # OpenDNS
    "146.112.41.2", "146.112.41.3",                                            # OpenDNS DoH
    "94.140.14.14", "94.140.15.15", "94.140.14.15", "94.140.15.16", "94.140.14.140", "94.140.14.141",  # AdGuard
    "185.228.168.9", "185.228.169.9", "185.228.168.10", "185.228.169.11", "185.228.168.168", "185.228.169.168",  # CleanBrowsing
    "45.90.28.0/24", "45.90.30.0/24",                                          # NextDNS
    "76.76.2.0/24", "76.76.10.0/24",                                           # Control D
    "194.242.2.0/24",                                                          # Mullvad DNS
    "185.222.222.222", "45.11.45.11",                                          # DNS.SB
    "193.110.81.0", "185.253.5.0", "193.110.81.9", "185.253.5.9", "193.110.81.1", "185.253.5.1",  # dns0.eu
    "223.5.5.5", "223.6.6.6", "1.12.12.12", "120.53.53.53", "116.202.176.26",  # AliDNS, DNSPod, LibreDNS
)
DOH_V6 = (
    "2001:4860:4860::8888", "2001:4860:4860::8844", "2001:4860:4860::6464", "2001:4860:4860::64",
    "2606:4700:4700::1111", "2606:4700:4700::1001", "2606:4700:4700::1112", "2606:4700:4700::1002",
    "2606:4700:4700::64", "2606:4700:4700::6400",
    "2620:fe::fe", "2620:fe::9", "2620:fe::10", "2620:fe::fe:10", "2620:fe::11", "2620:fe::fe:11",
    "2620:119:35::35", "2620:119:53::53", "2620:0:ccc::2", "2620:0:ccd::2",
    "2a10:50c0::ad1:ff", "2a10:50c0::ad2:ff", "2a10:50c0::1:ff", "2a10:50c0::2:ff", "2a10:50c0::bad1:ff", "2a10:50c0::bad2:ff",
    "2a0d:2a00:1::", "2a0d:2a00:2::", "2a0d:2a00:1::1", "2a0d:2a00:2::1", "2a0d:2a00:1::2", "2a0d:2a00:2::2",
    "2a07:a8c0::/32", "2a07:a8c1::/32",
    "2a07:e340::2", "2a07:e340::3", "2a07:e340::4", "2a07:e340::5", "2a07:e340::6", "2a07:e340::7", "2a07:e340::8", "2a07:e340::9",
    "2a09::", "2a11::",
    "2a0f:fc80::", "2a0f:fc81::", "2a0f:fc80::9", "2a0f:fc81::9", "2a0f:fc80::1", "2a0f:fc81::1",
)


def firewall_rules(keep_reachable=()) -> list:
    """[(name, protocol, remoteport, remoteip)] — pure, unit-tested.

    `keep_reachable`: IPs that must never be blocked (the remote console server)."""
    keep = list(keep_reachable or ())
    dns_v4 = carve_ranges(_V4_ALL, list(CF_V4) + keep)
    dns_v6 = carve_ranges(_V6_ALL, list(CF_V6) + keep)
    doh_v4 = ",".join(ip for ip in DOH_V4 if ip not in keep)
    doh_v6 = ",".join(ip for ip in DOH_V6 if ip not in keep)
    rules = []
    for proto in ("TCP", "UDP"):
        rules.append((f"HOTS DNS Lock v4 {proto}", proto, "53,853", dns_v4))
        rules.append((f"HOTS DNS Lock v6 {proto}", proto, "53,853", dns_v6))
        rules.append((f"HOTS DoH Block v4 {proto}", proto, "443", doh_v4))
        rules.append((f"HOTS DoH Block v6 {proto}", proto, "443", doh_v6))
    return rules


RULE_NAMES = tuple(name for name, *_ in firewall_rules())


def interface_ok(v4: list, v6: list) -> bool:
    """Adapter filtered: IPv4 servers only Cloudflare Family, IPv6 servers (if any) too."""
    return bool(v4) and all(s in CF_V4 for s in v4) and all(s in CF_V6 for s in v6)


def _clean_ips(values) -> list:
    out = []
    for v in values or ():
        try:
            ip = ipaddress.ip_address(str(v).split("%")[0])
        except ValueError:
            continue
        if ip.version == 6 and (ip.is_site_local or ip.is_unspecified):
            continue
        out.append(str(ip))
    return out


# --- Windows plumbing ---------------------------------------------------------------------------

def _netsh(args: list):
    from .core_netblock import _run_netsh
    return _run_netsh(args)


def _v6_servers(iface: str) -> list:
    r = _netsh(["interface", "ipv6", "show", "dnsservers", f"name={iface}"])
    if r is None or r.returncode != 0:
        return []
    found = re.findall(r"([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})", r.stdout or "")
    out = []
    for cand in found:
        try:
            ip = ipaddress.IPv6Address(cand)
        except ValueError:
            continue
        if ip.is_site_local or ip.is_loopback or ip.is_unspecified:  # fec0:0:0:ffff::1 = "no DNS" placeholder
            continue
        if str(ip) not in out:
            out.append(str(ip))
    return out


def _v6_static(iface: str) -> list:
    from .dns_utils import _iface_guid
    guid = _iface_guid.get(iface)
    if not guid:
        return []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _TCPIP6_IFACES_KEY + "\\" + guid) as key:
            value, _ = winreg.QueryValueEx(key, "NameServer")
    except OSError:
        return []
    return _clean_ips(re.split(r"[\s,]+", str(value).strip())) if value else []


def _set_v6(iface: str, servers: list) -> bool:
    if not servers:
        r = _netsh(["interface", "ipv6", "set", "dnsservers", f"name={iface}", "source=dhcp"])
        return r is not None and r.returncode == 0
    r = _netsh(["interface", "ipv6", "set", "dnsservers", f"name={iface}", "source=static",
                f"address={servers[0]}", "register=none", "validate=no"])
    ok = r is not None and r.returncode == 0
    for idx, srv in enumerate(servers[1:], start=2):
        _netsh(["interface", "ipv6", "add", "dnsservers", f"name={iface}", f"address={srv}",
                f"index={idx}", "validate=no"])
    return ok


def _rule_exists(name: str) -> bool:
    r = _netsh(["advfirewall", "firewall", "show", "rule", f"name={name}"])
    return r is not None and r.returncode == 0


def _add_rules(extra=()) -> bool:
    """(Re)create the rules. `extra`: DNS servers of a trusted network / captive portal window."""
    try:
        from .remote.config import netblock_exclusions
        keep = netblock_exclusions()
    except Exception:
        keep = []
    keep = list(keep) + [ip for ip in _clean_ips(extra)]
    ok = True
    for name, proto, port, remote in firewall_rules(keep):
        _netsh(["advfirewall", "firewall", "delete", "rule", f"name={name}"])
        r = _netsh(["advfirewall", "firewall", "add", "rule", f"name={name}", "dir=out", "action=block",
                    f"protocol={proto}", f"remoteport={port}", f"remoteip={remote}",
                    "profile=any", "interfacetype=any", "enable=yes"])
        if r is None or r.returncode != 0:
            logger.error("dnslock: cannot add rule %s: %s", name, (r.stderr or r.stdout).strip() if r else "")
            ok = False
    return ok


def _remove_rules() -> None:
    for name in RULE_NAMES:
        _netsh(["advfirewall", "firewall", "delete", "rule", f"name={name}"])


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    atomic_write_json(STATE_FILE, state)


def _relaxed() -> dict:
    try:
        from .core_networks import relaxed_adapters
        return relaxed_adapters()
    except Exception:
        return {}


def adapters() -> list:
    """[{name, dhcp, v4, v6, ok, relaxed}] for every active adapter. An adapter on a trusted network
    (school…) counts as filtered: that network filters itself."""
    from .dns_utils import get_active_interfaces, _parallel_dns_info
    interfaces = get_active_interfaces()
    info = _parallel_dns_info(interfaces)
    relaxed = _relaxed()
    out = []
    for iface in interfaces:
        dhcp, v4 = info.get(iface, (False, []))
        v6 = _v6_servers(iface)
        r = relaxed.get(iface) or {}
        ok = interface_ok(v4, v6) or r.get("reason") == "trusted"
        out.append({"name": iface, "dhcp": bool(dhcp), "v4": v4, "v6": v6, "ok": ok,
                    "relaxed": r.get("reason"), "network": r.get("network"), "until": r.get("until")})
    return out


class DnsLockManager:
    last_error: str = ""
    _lock = threading.RLock()

    @staticmethod
    def is_active() -> bool:
        return _load_state().get("active") is True

    @staticmethod
    def enable() -> bool:
        from .core_antispy import _is_admin
        from .core_browser_policy import reg_read, reg_write
        from .dns_utils import enable_cf_family_dns, get_active_interfaces
        with DnsLockManager._lock:
            DnsLockManager.last_error = ""
            if not _is_admin():
                DnsLockManager.last_error = "no_admin"
                return False
            ok, failed = enable_cf_family_dns()
            if not ok:
                DnsLockManager.last_error = "Impossible de régler les serveurs DNS : " + ", ".join(failed)
                return False
            state = _load_state()
            v6_backup = dict(state.get("v6_backup") or {})
            for iface in get_active_interfaces():
                if iface not in v6_backup:
                    v6_backup[iface] = _v6_static(iface)
                _set_v6(iface, list(CF_V6))
            if "policy_original" not in state:
                exists, vtype, data = reg_read(POLICY_PATH, POLICY_NAME)
                state["policy_original"] = {"exists": exists, "type": vtype, "data": data}
            try:
                reg_write(POLICY_PATH, POLICY_NAME, 1, POLICY_VALUE)
            except OSError as e:
                logger.warning("dnslock: NameServer policy not written: %s", e)
            rules_ok = _add_rules()
            state.update({"active": True, "v6_backup": v6_backup})
            _save_state(state)
            _flush_dns()
            if not rules_ok:
                DnsLockManager.last_error = "Certaines règles du pare-feu n'ont pas pu être créées."
                return False
            logger.info("dnslock: enabled")
            return True

    @staticmethod
    def disable() -> bool:
        from .core_antispy import _is_admin
        from .core_browser_policy import reg_write, reg_delete
        with DnsLockManager._lock:
            DnsLockManager.last_error = ""
            if not _is_admin():
                DnsLockManager.last_error = "no_admin"
                return False
            state = _load_state()
            _remove_rules()
            original = state.get("policy_original")
            try:
                if original and original.get("exists"):
                    reg_write(POLICY_PATH, POLICY_NAME, original.get("type") or 1, original.get("data"))
                else:
                    reg_delete(POLICY_PATH, POLICY_NAME)
            except OSError as e:
                logger.warning("dnslock: cannot restore the NameServer policy: %s", e)
            for iface, servers in (state.get("v6_backup") or {}).items():
                _set_v6(iface, _clean_ips(servers))
            _save_state({"active": False})
            _flush_dns()
            logger.info("dnslock: disabled")
            return True

    @staticmethod
    def reapply() -> list:
        """Cover new adapters / undo changes. Returns what was repaired (empty if nothing)."""
        from .core_browser_policy import reg_read, reg_write
        from .dns_utils import (get_active_interfaces, _parallel_dns_info, _load_dns_backup,
                                _save_dns_backup, get_dns_for_interface, set_dns_for_interface)
        if not DnsLockManager.is_active():
            return []
        repaired = []
        with DnsLockManager._lock:
            state = _load_state()
            v6_backup = dict(state.get("v6_backup") or {})
            v4_backup = _load_dns_backup() or {}
            relaxed = _relaxed()
            interfaces = [i for i in get_active_interfaces() if i not in relaxed]
            info = _parallel_dns_info(interfaces)
            for iface in interfaces:
                _dhcp, v4 = info.get(iface, (False, []))
                if not (v4 and all(s in CF_V4 for s in v4)):
                    if iface not in v4_backup:
                        v4_backup[iface] = get_dns_for_interface(iface)
                        _save_dns_backup(v4_backup)
                    if set_dns_for_interface(iface, list(CF_V4)):
                        repaired.append(f"DNS IPv4 : {iface}")
                v6 = _v6_servers(iface)
                if any(s not in CF_V6 for s in v6) or not v6:
                    if iface not in v6_backup:
                        v6_backup[iface] = _v6_static(iface)
                    if _set_v6(iface, list(CF_V6)) and v6:
                        repaired.append(f"DNS IPv6 : {iface}")
            state["v6_backup"] = v6_backup
            _save_state(state)
            exists, _t, data = reg_read(POLICY_PATH, POLICY_NAME)
            if not relaxed and (not exists or data != POLICY_VALUE):
                try:
                    reg_write(POLICY_PATH, POLICY_NAME, 1, POLICY_VALUE)
                    repaired.append("stratégie NameServer")
                except OSError:
                    pass
            if not all(_rule_exists(name) for name in RULE_NAMES):
                _add_rules(state.get("allowed_extra") or [])
                repaired.append("règles du pare-feu DNS")
        if repaired:
            _flush_dns()
            logger.info("dnslock: repaired %s", repaired)
        return repaired

    @staticmethod
    def apply_relax(old: dict, new: dict, lock: bool) -> bool:
        """Suspend / restore the filter adapter by adapter (trusted network, captive portal window).
        Also used when only the family DNS filter is on (lock=False): then only adapter DNS changes."""
        from .core_browser_policy import reg_read, reg_write, reg_delete
        from .dns_utils import _load_dns_backup, get_active_interfaces, set_dns_for_interface
        changed = False
        with DnsLockManager._lock:
            state = _load_state()
            v4_backup = _load_dns_backup() or {}
            v6_backup = state.get("v6_backup") or {}
            active = set(get_active_interfaces())
            from .dns_utils import get_dns_info
            for iface, info in new.items():
                prev = old.get(iface)
                if prev and prev.get("key") == info.get("key") and iface in active:
                    _dhcp, current = get_dns_info(iface)
                    if not (current and all(s in CF_V4 for s in current)):
                        continue  # already on the network's own DNS
                set_dns_for_interface(iface, _clean_ips(v4_backup.get(iface, [])))  # the network's own DNS
                if lock:
                    _set_v6(iface, _clean_ips(v6_backup.get(iface, [])))
                changed = True
                logger.info("dns filter suspended on %s (%s)", iface, info.get("reason"))
            for iface in old:
                if iface not in new and iface in active:
                    set_dns_for_interface(iface, list(CF_V4))
                    if lock:
                        _set_v6(iface, list(CF_V6))
                    changed = True
                    logger.info("dns filter restored on %s", iface)
            if lock:
                extra = sorted({ip for info in new.values() for ip in _clean_ips(info.get("dns"))}
                               | {ip for iface in new if iface in active for ip in _v6_servers(iface)
                                  if ip not in CF_V6})
                if extra != sorted(state.get("allowed_extra") or []):
                    _add_rules(extra)
                    state["allowed_extra"] = extra
                    _save_state(state)
                    changed = True
                exists, _t, data = reg_read(POLICY_PATH, POLICY_NAME)
                try:
                    if new and exists and data == POLICY_VALUE:
                        original = state.get("policy_original") or {}
                        if original.get("exists"):
                            reg_write(POLICY_PATH, POLICY_NAME, original.get("type") or 1, original.get("data"))
                        else:
                            reg_delete(POLICY_PATH, POLICY_NAME)
                        changed = True
                    elif not new and (not exists or data != POLICY_VALUE):
                        reg_write(POLICY_PATH, POLICY_NAME, 1, POLICY_VALUE)
                        changed = True
                except OSError as e:
                    logger.warning("dnslock: NameServer policy not updated: %s", e)
        if changed:
            _flush_dns()
        return changed

    @staticmethod
    def status(include_rules: bool = True) -> dict:
        from .core_browser_policy import reg_read
        items = adapters()
        exists, _t, data = reg_read(POLICY_PATH, POLICY_NAME)
        out = {
            "active": DnsLockManager.is_active(),
            "adapters": items,
            "all_ok": bool(items) and all(a["ok"] for a in items),
            "any_cf": any(any(s in CF_V4 for s in a["v4"]) for a in items),
            "policy": exists and data == POLICY_VALUE,
        }
        if include_rules:
            out["rules"] = all(_rule_exists(name) for name in RULE_NAMES)
        return out


def _flush_dns() -> None:
    try:
        from .core import flush_dns_cache
        flush_dns_cache()
    except Exception:
        pass
