"""Trusted networks (school, workplace) and captive portals (hotel, train, guest Wi-Fi).

Problem: the family DNS filter (and even more the DNS lock) forces Cloudflare Family DNS. Many
networks forbid external DNS — captive portals before login, school networks that impose their own
(filtered) DNS, internal names (intranet) and sometimes a proxy. The PC would then lose Internet.

Solution, decided every 30 s by the agent:
  * trusted network (declared in the console, per account): the network's own DNS (and proxy, if
    configured) is used while connected to it. Recognised by Wi-Fi name AND gateway MAC address,
    so a phone hotspot renamed like the school Wi-Fi does not match.
  * unknown network that blocks the filtered DNS: probably a captive portal. The filter is
    suspended on that adapter for 10 minutes (at most 2 windows per network per hour, and no new
    window for 1 hour after one expired with the network still blocking), and comes back as soon
    as Cloudflare Family answers again (portal passed). Every window is logged and
    reported to the parents, who can mark the network as trusted in one click.
Browser policies, hosts blocklists and the DoH blocks stay in force during a window.
"""

import json
import logging
import os
import re
import subprocess
import time

from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.networks")

POLICY_FILE = os.path.join(PROGRAMDATA_DIR, "network_policy.json")
STATE_FILE = os.path.join(PROGRAMDATA_DIR, "network_state.json")

CAPTIVE_WINDOW = 10 * 60
CAPTIVE_MAX_PER_HOUR = 2
MAX_SEEN = 20
MAX_EVENTS = 40
_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}")
_CREATE_NO_WINDOW = 0x08000000
_SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
_TCPIP_IFACES_KEY = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"


# --- pure logic (unit-tested) ------------------------------------------------------------------------

def norm_mac(mac) -> str:
    mac = (mac or "").strip().lower().replace("-", ":")
    return mac if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) else ""


def network_key(fp: dict) -> str:
    """Stable identity of a network as seen from one adapter."""
    parts = []
    if fp.get("ssid"):
        parts.append("ssid:" + fp["ssid"])
    mac = norm_mac(fp.get("gateway_mac"))
    if mac:
        parts.append("gw:" + mac)
    elif fp.get("gateway"):
        parts.append("gwip:" + fp["gateway"])
    return "|".join(parts) or ("if:" + fp.get("name", "?"))


def clean_policy(raw) -> dict:
    """Validate the policy received from the console (it drives what the SYSTEM agent does)."""
    raw = raw if isinstance(raw, dict) else {}
    out = {"captive_portal": raw.get("captive_portal", True) is not False, "trusted": []}
    for net in raw.get("trusted") or []:
        if not isinstance(net, dict):
            continue
        ssid = str(net.get("ssid") or "")[:64]
        mac = norm_mac(net.get("gateway_mac"))
        match = net.get("match") if net.get("match") in ("ssid+gateway", "ssid", "gateway") else "ssid+gateway"
        if match == "ssid+gateway" and not (ssid and mac):
            match = "ssid" if ssid else "gateway"
        if (match == "ssid" and not ssid) or (match == "gateway" and not mac):
            continue
        proxy = net.get("proxy") if isinstance(net.get("proxy"), dict) else {}
        mode = proxy.get("mode") if proxy.get("mode") in ("none", "auto", "pac", "fixed") else "none"
        clean_proxy = {"mode": mode}
        if mode == "pac":
            url = str(proxy.get("pac_url") or "")
            if not re.fullmatch(r"https?://[^\s\"']{1,300}", url):
                clean_proxy = {"mode": "auto"}
            else:
                clean_proxy["pac_url"] = url
        elif mode == "fixed":
            server = str(proxy.get("server") or "")
            if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}:\d{1,5}", server):
                clean_proxy = {"mode": "none"}
            else:
                clean_proxy["server"] = server
        out["trusted"].append({"id": str(net.get("id") or "")[:40], "name": str(net.get("name") or ssid or mac)[:80],
                               "ssid": ssid, "gateway_mac": mac, "match": match, "proxy": clean_proxy})
    return out


def match_trusted(fp: dict, trusted: list):
    ssid = fp.get("ssid") or ""
    mac = norm_mac(fp.get("gateway_mac"))
    for net in trusted:
        if net["match"] == "ssid+gateway" and ssid and ssid == net["ssid"] and mac and mac == net["gateway_mac"]:
            return net
        if net["match"] == "ssid" and ssid and ssid == net["ssid"]:
            return net
        if net["match"] == "gateway" and mac and mac == net["gateway_mac"]:
            return net
    return None


def _label(fp: dict) -> str:
    return f"« {fp['ssid']} »" if fp.get("ssid") else f"réseau filaire ({fp.get('name', '?')})"


def decide(adapters: list, cf_ok, policy: dict, state: dict, now: float, dns_enforced: bool = True) -> dict:
    """Pure decision. Returns {"relax": {adapter: info}, "proxy": cfg|None, "state": new_state, "events": [...]}

    adapters: [{name, ssid, gateway, gateway_mac, dns (original/DHCP servers), domain}]
    cf_ok:    True (Cloudflare Family answers), False (blocked), None (unknown: keep as is)
    """
    state = json.loads(json.dumps(state or {}))
    old_relax = state.get("relax") or {}
    windows = {k: [t for t in v if now - t < 3600] for k, v in (state.get("windows") or {}).items()}
    seen = state.get("seen") or []
    events = []
    next_id = int(state.get("next_event_id") or 1)
    notified = {k: t for k, t in (state.get("restrictive_notified") or {}).items() if now - t < 3600}

    def event(kind, message, fp):
        nonlocal next_id
        events.append({"id": next_id, "at": now, "kind": kind, "message": message, "key": network_key(fp)})
        next_id += 1

    def remember(fp, kind):
        key = network_key(fp)
        entry = next((s for s in seen if s["key"] == key), None)
        if entry is None:
            entry = {"key": key, "first": now, "count": 0}
            seen.insert(0, entry)
        entry.update({"ssid": fp.get("ssid") or "", "gateway": fp.get("gateway") or "",
                      "gateway_mac": norm_mac(fp.get("gateway_mac")), "domain": fp.get("domain") or "",
                      "kind": kind, "last": now, "count": entry["count"] + 1})

    relax, proxy = {}, None
    present = {a["name"] for a in adapters}
    for fp in adapters:
        name = fp["name"]
        key = network_key(fp)
        prev = old_relax.get(name)
        if prev and prev.get("key") != key:
            prev = None  # same adapter, another network: start over
        trusted = match_trusted(fp, policy.get("trusted") or [])
        if trusted:
            proxy = proxy or (trusted["proxy"] if trusted["proxy"]["mode"] != "none" else None)
            if not dns_enforced:
                continue
            relax[name] = {"reason": "trusted", "key": key, "network": trusted["name"], "dns": fp.get("dns") or [],
                           "since": prev.get("since", now) if prev and prev.get("reason") == "trusted" else now,
                           "until": None}
            if not (prev and prev.get("reason") == "trusted"):
                event("trusted", f"Réseau de confiance « {trusted['name']} » : DNS de l'établissement utilisé", fp)
            continue
        if not dns_enforced:
            continue
        if cf_ok is None:
            if prev:
                relax[name] = prev
            continue
        if cf_ok:
            if prev and prev.get("reason") == "captive":
                event("captive_closed", f"Filtre DNS rétabli sur {_label(fp)} (connexion au portail réussie)", fp)
            continue
        # The filtered DNS does not answer on this network.
        if prev and prev.get("reason") == "captive" and prev.get("until", 0) > now:
            relax[name] = prev
            continue
        if prev and prev.get("reason") == "captive":
            event("captive_expired", f"Fin de la fenêtre de connexion sur {_label(fp)} : ce réseau impose son "
                                     "propre DNS. Ajoutez-le aux réseaux de confiance s'il est sûr (établissement).", fp)
            remember(fp, "restrictive")
            notified[key] = now
            continue
        if key in notified:
            continue  # this network already used its window and still blocks the filter: 1 h cooldown
        if not policy.get("captive_portal", True) or not fp.get("gateway") or not fp.get("dns"):
            if key not in notified:
                event("restrictive", f"Le réseau {_label(fp)} bloque le filtre DNS : pas d'accès Internet "
                                     "(fenêtres de connexion désactivées ou impossibles).", fp)
                remember(fp, "restrictive")
                notified[key] = now
            continue
        if len(windows.get(key, [])) >= CAPTIVE_MAX_PER_HOUR:
            if key not in notified:
                event("restrictive", f"Le réseau {_label(fp)} bloque toujours le filtre DNS : fenêtres de connexion "
                                     "épuisées pour l'heure. Ajoutez-le aux réseaux de confiance s'il est sûr.", fp)
                remember(fp, "restrictive")
                notified[key] = now
            continue
        windows.setdefault(key, []).append(now)
        relax[name] = {"reason": "captive", "key": key, "network": fp.get("ssid") or fp.get("gateway") or name,
                       "dns": fp.get("dns") or [], "since": now, "until": now + CAPTIVE_WINDOW}
        event("captive_open", f"Portail de connexion probable sur {_label(fp)} : filtre DNS suspendu "
                              f"{CAPTIVE_WINDOW // 60} minutes sur ce réseau.", fp)
        remember(fp, "captive")
    state.update({
        "relax": relax,
        "windows": {k: v for k, v in windows.items() if v},
        "seen": seen[:MAX_SEEN],
        "events": ((state.get("events") or []) + events)[-MAX_EVENTS:],
        "next_event_id": next_id,
        "restrictive_notified": notified,
        "adapters_present": sorted(present),
    })
    return {"relax": relax, "proxy": proxy, "state": state, "events": events}


# --- storage ----------------------------------------------------------------------------------------------

def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def load_policy() -> dict:
    return clean_policy(_read(POLICY_FILE, {}))


def save_policy(raw) -> bool:
    """Store the console policy; returns True if it changed."""
    policy = clean_policy(raw)
    if policy == load_policy() and os.path.exists(POLICY_FILE):
        return False
    atomic_write_json(POLICY_FILE, policy)
    return True


def load_state() -> dict:
    return _read(STATE_FILE, {})


def relaxed_adapters() -> dict:
    return load_state().get("relax") or {}


# --- Windows collection ---------------------------------------------------------------------------------

def _run(args, timeout=10) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", check=False,
                           creationflags=_CREATE_NO_WINDOW, timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def _wifi_ssids() -> dict:
    """{adapter friendly name: (ssid, bssid)} from `netsh wlan show interfaces` (labels are not
    translated for SSID/BSSID; the adapter is found by value, whatever the label language)."""
    out = _run([os.path.join(_SYSTEM32, "netsh.exe"), "wlan", "show", "interfaces"])
    result = {}
    for block in re.split(r"\r?\n\s*\r?\n", out):
        values, ssid, bssid = [], "", ""
        for line in block.splitlines():
            m = re.match(r"^\s*([^:]+?)\s*:\s*(.*?)\s*$", line)
            if not m:
                continue
            label, value = m.group(1).strip(), m.group(2)
            values.append(value)
            if label.upper() == "SSID":
                ssid = value
            elif label.upper() == "BSSID":
                bssid = value
        if ssid:
            for v in values:
                result[v] = (ssid, bssid)
    return result


def _gateway_mac(ip: str) -> str:
    if not ip:
        return ""
    arp = os.path.join(_SYSTEM32, "arp.exe")
    out = _run([arp, "-a", ip])
    m = _MAC_RE.search(out)
    if not m:
        _run([os.path.join(_SYSTEM32, "ping.exe"), "-n", "1", "-w", "500", ip], timeout=5)
        m = _MAC_RE.search(_run([arp, "-a", ip]))
    return norm_mac(m.group(0)) if m else ""


def _reg_iface(guid: str, name: str):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _TCPIP_IFACES_KEY + "\\" + guid) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def collect_adapters() -> list:
    """Fingerprint of every active adapter: Wi-Fi name, gateway (+MAC), the network's own DNS."""
    from .dns_utils import get_active_interfaces, _iface_guid, _load_dns_backup
    interfaces = get_active_interfaces()
    wifi = _wifi_ssids()
    backup = _load_dns_backup() or {}
    out = []
    for iface in interfaces:
        guid = _iface_guid.get(iface, "")
        gw = _reg_iface(guid, "DhcpDefaultGateway") or _reg_iface(guid, "DefaultGateway") or []
        gw = [g for g in (gw if isinstance(gw, list) else str(gw).split()) if g and g != "0.0.0.0"]
        dhcp_dns = str(_reg_iface(guid, "DhcpNameServer") or "")
        dns = backup.get(iface) or [s for s in re.split(r"[\s,]+", dhcp_dns) if s]
        ssid, bssid = wifi.get(iface, ("", ""))
        gateway = gw[0] if gw else ""
        out.append({"name": iface, "ssid": ssid, "bssid": bssid, "gateway": gateway,
                    "gateway_mac": _gateway_mac(gateway),
                    "domain": str(_reg_iface(guid, "DhcpDomain") or _reg_iface(guid, "Domain") or ""),
                    "dns": dns})
    return out


def cf_reachable():
    """True if Cloudflare Family answers DNS on this network, False if it is blocked."""
    from .core import _dns_lookup_external
    for server in ("1.1.1.3", "1.0.0.3"):
        if _dns_lookup_external("www.msftconnecttest.com", server, timeout=2) is not None:
            return True
    return False


# --- apply ---------------------------------------------------------------------------------------------------

def tick() -> dict:
    """One evaluation: collect, decide, apply. Returns {"changed", "events", "proxy"}."""
    from .core_dnslock import DnsLockManager
    from .dns_utils import has_dns_backup
    policy = load_policy()
    old = load_state()
    lock = DnsLockManager.is_active()
    enforced = lock or has_dns_backup()
    adapters = collect_adapters()
    cf_ok = cf_reachable() if enforced else True
    plan = decide(adapters, cf_ok, policy, old, time.time(), dns_enforced=enforced)
    old_relax = old.get("relax") or {}
    new_relax = plan["relax"]
    changed = False
    if enforced:
        changed = DnsLockManager.apply_relax(old_relax, new_relax, lock)
    atomic_write_json(STATE_FILE, plan["state"])
    try:
        from .core_browser_policy import BrowserPolicyManager
        if BrowserPolicyManager.set_network_proxy(plan["proxy"]):
            changed = True
    except Exception:
        logger.exception("network proxy for browsers failed")
    for ev in plan["events"]:
        logger.info("network: %s", ev["message"])
    return {"changed": changed or bool(plan["events"]), "events": plan["events"], "proxy": plan["proxy"]}


def status() -> dict:
    state = load_state()
    return {"policy": load_policy(), "relax": state.get("relax") or {}, "seen": state.get("seen") or [],
            "events": (state.get("events") or [])[-20:]}
