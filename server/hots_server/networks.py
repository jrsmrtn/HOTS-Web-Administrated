"""Account network policy: trusted networks (school, workplace) and captive-portal windows.

Sent to every PC of the account with each poll; the agent validates it again
(hosts_editor/core_networks.py: clean_policy) before using it.
"""

import re
import secrets

from . import db

_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
MAX_TRUSTED = 50


class PolicyError(ValueError):
    pass


def default_policy() -> dict:
    return {"captive_portal": True, "trusted": []}


def get_policy(account_row) -> dict:
    data = db.loads(account_row["network_policy_json"]) if account_row else None
    return data if isinstance(data, dict) else default_policy()


def validate(data) -> dict:
    if not isinstance(data, dict):
        raise PolicyError("Politique réseau invalide.")
    trusted = data.get("trusted") or []
    if not isinstance(trusted, list) or len(trusted) > MAX_TRUSTED:
        raise PolicyError("Liste de réseaux invalide.")
    out = {"captive_portal": data.get("captive_portal", True) is not False, "trusted": []}
    for net in trusted:
        if not isinstance(net, dict):
            raise PolicyError("Réseau invalide.")
        name = str(net.get("name") or "").strip()[:80]
        ssid = str(net.get("ssid") or "")[:64]
        mac = str(net.get("gateway_mac") or "").strip().lower().replace("-", ":")
        if mac and not _MAC.match(mac):
            raise PolicyError(f"Adresse MAC de passerelle invalide : {net.get('gateway_mac')}")
        match = net.get("match") if net.get("match") in ("ssid+gateway", "ssid", "gateway") else "ssid+gateway"
        if match == "ssid+gateway" and not (ssid and mac):
            raise PolicyError(f"« {name or ssid} » : le nom du Wi-Fi et l'adresse de la passerelle sont nécessaires.")
        if match == "ssid" and not ssid:
            raise PolicyError("Nom du Wi-Fi manquant.")
        if match == "gateway" and not mac:
            raise PolicyError("Adresse MAC de la passerelle manquante.")
        proxy = net.get("proxy") if isinstance(net.get("proxy"), dict) else {"mode": "none"}
        mode = proxy.get("mode") if proxy.get("mode") in ("none", "auto", "pac", "fixed") else "none"
        clean = {"mode": mode}
        if mode == "pac":
            url = str(proxy.get("pac_url") or "").strip()
            if not re.fullmatch(r"https?://[^\s\"']{1,300}", url):
                raise PolicyError("Adresse du fichier PAC invalide (http://… ou https://…).")
            clean["pac_url"] = url
        elif mode == "fixed":
            server = str(proxy.get("server") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}:\d{1,5}", server):
                raise PolicyError("Proxy invalide (format : nom-ou-ip:port, ex. proxy.college.lan:3128).")
            clean["server"] = server
        out["trusted"].append({"id": str(net.get("id") or secrets.token_hex(4))[:40], "name": name or ssid or mac,
                               "ssid": ssid, "gateway_mac": mac, "match": match, "proxy": clean,
                               "note": str(net.get("note") or "")[:200]})
    return out
