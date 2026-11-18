"""Phase 1 (anti-bypass) logic of the Windows agent, tested without Windows."""

import ipaddress
import json
import sys
import types

import pytest

_w = sys.modules.setdefault("winreg", types.ModuleType("winreg"))

from hosts_editor import compliance, core_browser_policy as bp, core_dnslock as dl  # noqa: E402
from hosts_editor.remote import actions  # noqa: E402


# --- browser lockdown -----------------------------------------------------------------------------

@pytest.fixture()
def fake_registry(tmp_path, monkeypatch):
    reg = {}
    monkeypatch.setattr(bp, "STATE_FILE", str(tmp_path / "browser_policy_state.json"))
    monkeypatch.setattr(bp, "atomic_write_json",
                        lambda path, data: open(path, "w", encoding="utf-8").write(json.dumps(data)))
    monkeypatch.setattr(bp, "reg_read", lambda p, n: (True, *reg[(p, n)]) if (p, n) in reg else (False, None, None))
    monkeypatch.setattr(bp, "reg_write", lambda p, n, t, d: reg.__setitem__((p, n), (t, d)))
    monkeypatch.setattr(bp, "reg_delete", lambda p, n: reg.pop((p, n), None))
    return reg


CHROME = r"SOFTWARE\Policies\Google\Chrome"
EDGE = r"SOFTWARE\Policies\Microsoft\Edge"
FIREFOX = r"SOFTWARE\Policies\Mozilla\Firefox"


def test_browser_levels_and_exact_restore(fake_registry):
    reg = fake_registry
    # Pre-existing values from another tool must survive a full on/off cycle.
    reg[(CHROME, "ForceGoogleSafeSearch")] = (bp.REG_DWORD, 0)
    reg[(CHROME, "ExtensionSettings")] = (bp.REG_SZ, json.dumps({"abcd": {"installation_mode": "blocked"}}))
    before = dict(reg)

    assert bp.BrowserPolicyManager.apply("standard")
    assert reg[(CHROME, "ForceGoogleSafeSearch")] == (bp.REG_DWORD, 1)
    assert reg[(CHROME, "ForceYouTubeRestrict")] == (bp.REG_DWORD, 1)
    ext = json.loads(reg[(CHROME, "ExtensionSettings")][1])
    assert ext["*"]["blocked_permissions"] == ["proxy"] and ext["abcd"]["installation_mode"] == "blocked"
    assert json.loads(reg[(EDGE, "ProxySettings")][1]) == {"ProxyMode": "direct"}
    assert reg[(EDGE, "ForceBingSafeSearch")] == (bp.REG_DWORD, 2)
    assert reg[(r"SOFTWARE\Policies\BraveSoftware\Brave", "TorDisabled")] == (bp.REG_DWORD, 1)
    assert reg[(FIREFOX + r"\Proxy", "Mode")] == (bp.REG_SZ, "none")
    assert reg[(FIREFOX, "IPProtectionAvailable")] == (bp.REG_DWORD, 0)
    assert (CHROME, "IncognitoModeAvailability") not in reg
    assert bp.BrowserPolicyManager.status()["drift"] == 0

    assert bp.BrowserPolicyManager.apply("strict", safesites=True)
    assert reg[(CHROME, "ForceYouTubeRestrict")] == (bp.REG_DWORD, 2)
    assert reg[(CHROME, "IncognitoModeAvailability")] == (bp.REG_DWORD, 1)
    assert reg[(EDGE, "InPrivateModeAvailability")] == (bp.REG_DWORD, 1)
    assert reg[(FIREFOX, "PrivateBrowsingModeAvailability")] == (bp.REG_DWORD, 1)
    assert reg[(CHROME, "SafeSitesFilterBehavior")] == (bp.REG_DWORD, 1)
    # applying twice does not stack "proxy" twice
    assert json.loads(reg[(CHROME, "ExtensionSettings")][1])["*"]["blocked_permissions"] == ["proxy"]

    assert bp.BrowserPolicyManager.apply("standard")  # back down: strict-only values removed
    assert (CHROME, "IncognitoModeAvailability") not in reg and (CHROME, "SafeSitesFilterBehavior") not in reg

    assert bp.BrowserPolicyManager.apply("off")
    assert reg == before  # exactly as before, including the other tool's values


def test_browser_drift_detected_and_repaired(fake_registry):
    reg = fake_registry
    bp.BrowserPolicyManager.apply("standard")
    del reg[(r"SOFTWARE\Policies\BraveSoftware\Brave", "BraveVPNDisabled")]
    reg[(CHROME, "ForceGoogleSafeSearch")] = (bp.REG_DWORD, 0)
    st = bp.BrowserPolicyManager.status()
    bad = {b["id"]: b["drift"] for b in st["browsers"] if not b["ok"]}
    assert bad == {"brave": ["BraveVPNDisabled"], "chrome": ["ForceGoogleSafeSearch"]}
    assert bp.BrowserPolicyManager.repair()
    assert bp.BrowserPolicyManager.status()["drift"] == 0


# --- DNS lock --------------------------------------------------------------------------------------

def _in(spec: str, ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    for part in spec.split(","):
        if "/" in part:
            if addr in ipaddress.ip_network(part):
                return True
        elif "-" in part:
            lo, hi = part.split("-")
            if ipaddress.ip_address(lo) <= addr <= ipaddress.ip_address(hi):
                return True
        elif addr == ipaddress.ip_address(part):
            return True
    return False


def test_dns_lock_firewall_rules():
    rules = {name: (proto, port, remote) for name, proto, port, remote in dl.firewall_rules(["51.15.20.30"])}
    assert len(rules) == 8
    v4 = rules["HOTS DNS Lock v4 UDP"][2]
    assert rules["HOTS DNS Lock v4 UDP"][1] == "53,853"
    for allowed in ("1.1.1.3", "1.0.0.3", "51.15.20.30", "127.0.0.1"):
        assert not _in(v4, allowed), allowed
    for blocked in ("8.8.8.8", "192.168.1.1", "1.1.1.1", "9.9.9.9"):
        assert _in(v4, blocked), blocked
    v6 = rules["HOTS DNS Lock v6 TCP"][2]
    assert not _in(v6, "2606:4700:4700::1113") and _in(v6, "2001:4860:4860::8888") and _in(v6, "fe80::1")
    doh = rules["HOTS DoH Block v4 TCP"]
    assert doh[1] == "443" and _in(doh[2], "8.8.8.8") and _in(doh[2], "45.90.28.7") and not _in(doh[2], "1.1.1.3")
    assert not any(ip in dl.DOH_V4 for ip in dl.CF_V4) and not any(ip in dl.DOH_V6 for ip in dl.CF_V6)
    for spec in list(dl.DOH_V4) + list(dl.DOH_V6):
        ipaddress.ip_network(spec, strict=False)  # every entry is valid


def test_dns_interface_coverage():
    assert dl.interface_ok(["1.1.1.3", "1.0.0.3"], [])
    assert dl.interface_ok(["1.1.1.3"], ["2606:4700:4700::1113"])
    assert not dl.interface_ok(["1.1.1.3"], ["2a01:cb00::1"])        # IPv6 DNS of the box: bypass
    assert not dl.interface_ok(["192.168.1.1"], [])
    assert not dl.interface_ok([], [])


def test_doh_bypass_list_never_blocks_the_family_resolver():
    from hosts_editor.resource_utils import blocklists_dir
    import os
    with open(os.path.join(blocklists_dir(), "doh_bypass.txt"), encoding="utf-8") as f:
        names = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    assert "dns.google" in names and "cloudflare-dns.com" in names
    assert "family.cloudflare-dns.com" not in names


# --- compliance --------------------------------------------------------------------------------------

CHILD = "S-1-5-21-111-222-333-1002"
PARENT = "S-1-5-21-111-222-333-1001"


def _data(**over):
    data = {
        "children": [CHILD],
        "windows": {
            "admins": [PARENT, CHILD, "S-1-5-21-111-222-333-500"],
            "users": [
                {"name": "Papa", "sid": PARENT, "enabled": True, "source": "Local", "password_last_set": "2026-01-01T00:00:00"},
                {"name": "Lea", "sid": CHILD, "enabled": True, "source": "MicrosoftAccount"},
                {"name": "Administrateur", "sid": "S-1-5-21-111-222-333-500", "enabled": False},
                {"name": "Invité", "sid": "S-1-5-21-111-222-333-501", "enabled": False},
            ],
            "bitlocker": 0, "features": {"Microsoft-Windows-Subsystem-Linux": 1, "VirtualMachinePlatform": 0},
        },
        "registry": {"EnableLUA": 1, "PromptOnSecureDesktop": 1, "SecureBoot": 1,
                     "DataBasePath": r"%SystemRoot%\System32\drivers\etc"},
        "observed": {"hosts_lock": {"wanted": True, "real": False, "drift": True},
                     "browsers": {"wanted": "off", "drift": False},
                     "dns": {"wanted": False, "all_ok": False, "any_cf": True, "adapters": [
                         {"name": "Wi-Fi", "ok": True}, {"name": "Ethernet 2 (téléphone)", "ok": False}]}},
        "unmanaged": [{"profile": "Lea", "name": "Opera / Opera GX (VPN intégré)", "kind": "browser",
                       "exe": "opera.exe", "path": r"C:\Users\Lea\AppData\Local\Programs\Opera\opera.exe"}],
        "proxy_extensions": [{"browser": "Google Chrome", "name": "Free VPN", "required": True}],
    }
    data.update(over)
    return data


def test_compliance_flags_child_admin_and_bypasses():
    checks = {c["id"]: c for c in compliance.evaluate(_data())}
    child = checks[f"accounts.child_admin.{CHILD}"]
    assert child["status"] == "fail" and child["fix"]["action"] == "accounts.make_standard"
    assert checks["accounts.guardian"]["status"] == "ok"
    assert checks["protections.real"]["status"] == "fail" and checks["protections.real"]["fix"]["action"] == "protections.repair"
    assert checks["dns.coverage"]["status"] == "fail" and "téléphone" in checks["dns.coverage"]["title"]
    assert checks["browsers.policies"]["fix"]["params"] == {"level": "standard", "safesites": False}
    assert checks["bypass.unmanaged"]["items"][0]["fix"]["params"]["exe_name"] == "opera.exe"
    assert checks["bypass.extensions"]["status"] == "warn"
    assert checks["system.virtualization"]["status"] == "warn"
    score, todo = compliance.score(list(checks.values()))
    assert score < 40
    assert todo[0]["fix"]["action"] == "accounts.make_standard"  # the critical problem comes first


def test_compliance_all_good():
    data = _data(children=[CHILD], unmanaged=[], proxy_extensions=[])
    data["windows"]["admins"] = [PARENT]
    data["windows"]["bitlocker"] = 1
    data["windows"]["features"] = {}
    data["observed"] = {"hosts_lock": {"drift": False}, "browsers": {"wanted": "standard", "drift": False},
                        "dns": {"wanted": True, "all_ok": True, "any_cf": True, "adapters": []}}
    checks = compliance.evaluate(data)
    assert all(c["status"] == "ok" for c in checks), [c["title"] for c in checks if c["status"] != "ok"]
    assert compliance.score(checks) == (100, [])


def test_compliance_hosts_redirection_is_critical():
    data = _data()
    data["registry"]["DataBasePath"] = r"C:\Temp\fake"
    checks = {c["id"]: c for c in compliance.evaluate(data)}
    assert checks["hosts.databasepath"]["status"] == "fail"


def test_scans_find_portable_browsers_extensions_and_vpn(tmp_path):
    home = tmp_path / "Lea"
    (home / "Downloads" / "tor-browser" / "Browser").mkdir(parents=True)
    (home / "Downloads" / "tor-browser" / "Browser" / "tor.exe").write_bytes(b"")
    (home / "AppData" / "Local" / "Programs" / "Opera").mkdir(parents=True)
    (home / "AppData" / "Local" / "Programs" / "Opera" / "opera.exe").write_bytes(b"")
    (home / "Desktop").mkdir()
    (home / "Desktop" / "psiphon3.exe").write_bytes(b"")
    (home / "Desktop" / "notes.txt").write_text("x")
    ext = home / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default" / "Extensions" / "abc" / "1.0"
    (ext / "_locales" / "fr").mkdir(parents=True)
    (ext / "manifest.json").write_text(json.dumps({"name": "__MSG_appName__", "default_locale": "fr",
                                                   "permissions": ["proxy", "storage"]}))
    (ext / "_locales" / "fr" / "messages.json").write_text(json.dumps({"appName": {"message": "VPN Gratuit"}}))
    pbk = home / "AppData" / "Roaming" / "Microsoft" / "Network" / "Connections" / "Pbk"
    pbk.mkdir(parents=True)
    (pbk / "rasphone.pbk").write_text("[Mon VPN]\nMEDIA=rastapi\n")
    profiles = [("Lea", str(home))]

    found = {f["exe"]: f for f in compliance.scan_unmanaged(profiles)}
    assert set(found) == {"tor.exe", "opera.exe", "psiphon3.exe"}
    assert found["psiphon3.exe"]["kind"] == "vpn"
    exts = compliance.scan_proxy_extensions(profiles)
    assert exts == [{"profile": "Lea", "browser": "Google Chrome", "id": "abc", "family": "chromium",
                     "name": "VPN Gratuit", "required": True}]
    assert {"profile": "Lea", "name": "Mon VPN"} in compliance.scan_native_vpn(profiles)


def test_make_standard_guards(monkeypatch):
    monkeypatch.setattr(compliance, "child_sids", lambda: [CHILD])
    called = []
    monkeypatch.setattr(compliance, "_set_admin", lambda sid, admin: called.append((sid, admin)))
    with pytest.raises(ValueError, match="compte enfant"):
        compliance.make_standard(PARENT)
    with pytest.raises(ValueError, match="intégré"):
        compliance.make_standard("S-1-5-21-111-222-333-500")
    # no other admin with a password -> refused (would lock everybody out)
    monkeypatch.setattr(compliance, "collect_windows", lambda: {"admins": [CHILD], "users": [
        {"name": "Lea", "sid": CHILD, "enabled": True}]})
    with pytest.raises(ValueError, match="Aucun autre compte administrateur"):
        compliance.make_standard(CHILD)
    monkeypatch.setattr(compliance, "collect_windows", lambda: _data()["windows"])
    compliance.make_standard(CHILD)
    assert called == [(CHILD, False)]


def test_new_actions_validate_parameters():
    assert actions.execute("browser_policies.set", {"level": "maximum"})[2] == "Niveau inconnu : maximum"
    assert actions.execute("accounts.make_standard", {"sid": "S-1-5-18"})[2] == "Identifiant de compte invalide."
    assert "manquant" in actions.execute("dns_lock.set", {})[2]
