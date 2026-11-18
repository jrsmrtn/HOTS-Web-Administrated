"""Phase 2: SafeSearch through hosts, Windows VPN/proxy settings, extension allow-list, tampering."""

import json
import struct
import sys
import types

sys.modules.setdefault("winreg", types.ModuleType("winreg"))

from hosts_editor import compliance, constants, core, core_browser_policy as bp, core_hardening as hd  # noqa: E402
from hosts_editor import core_safesearch as ss  # noqa: E402

IPS = {"google": "216.239.38.120", "youtube_strict": "216.239.38.120", "youtube_moderate": "216.239.38.119",
       "bing": "204.79.197.220"}


def test_safesearch_mapping():
    m = ss.build_mapping({"google": True, "youtube": "moderate", "bing": True}, IPS)
    assert m["www.google.fr"] == IPS["google"] and m["www.youtube.com"] == IPS["youtube_moderate"]
    assert m["www.bing.com"] == IPS["bing"]
    # YouTube blocked by the parental category: never turned into "restricted"
    m = ss.build_mapping({"google": True, "youtube": "strict", "bing": False}, IPS, youtube_blocked=True)
    assert "www.youtube.com" not in m and "www.bing.com" not in m
    assert ss.build_mapping({"google": True, "youtube": "off", "bing": False}, {}) == {}  # no IP, no entry


def test_dns_answer_parser():
    query_id = 0x1234
    header = struct.pack(">HHHHHH", query_id, 0x8180, 1, 2, 0, 0)
    question = b"\x0fforcesafesearch\x06google\x03com\x00" + struct.pack(">HH", 1, 1)
    cname = b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 300, 2) + b"\xc0\x0c"
    a = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 300, 4) + bytes([216, 239, 38, 120])
    assert ss.parse_a_answers(header + question + cname + a, query_id) == ["216.239.38.120"]
    assert ss.parse_a_answers(header + question + cname + a, 999) == []


def test_safesearch_writes_a_tagged_hosts_block(tmp_path, monkeypatch):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    monkeypatch.setattr(constants, "HOSTS_PATH", str(hosts))
    monkeypatch.setattr(core, "HOSTS_PATH", str(hosts))
    monkeypatch.setattr(core, "flush_dns_cache_debounced", lambda: None)
    monkeypatch.setattr(core, "_MIN_WRITE_GAP", 0)
    monkeypatch.setattr(ss, "STATE_FILE", str(tmp_path / "ss.json"))
    monkeypatch.setattr(ss, "atomic_write_json", lambda p, d: open(p, "w").write(json.dumps(d)))
    monkeypatch.setattr(ss, "resolve_a", lambda name, server="1.1.1.3", timeout=3: {
        "forcesafesearch.google.com": ["216.239.38.120"], "restrictmoderate.youtube.com": ["216.239.38.119"],
        "strict.bing.com": ["204.79.197.220"]}.get(name, []))
    assert ss.SafeSearchManager.apply({"google": True, "youtube": "moderate", "bing": True})
    text = hosts.read_text(encoding="utf-8")
    assert "# === HOSTS_EDITOR_PARENTAL_SAFESEARCH_START ===" in text
    assert "216.239.38.120 www.google.fr # SafeSearch" in text and "216.239.38.119 www.youtube.com # SafeSearch" in text
    assert core.is_parental_active("safesearch.txt")
    assert ss.SafeSearchManager.apply({"google": False, "youtube": "off", "bing": False})
    assert "SAFESEARCH" not in hosts.read_text(encoding="utf-8")


def test_settings_page_visibility_merge():
    assert hd.page_visibility("") == "hide:network-vpn;network-proxy"
    assert hd.page_visibility("hide:windowsupdate") == "hide:windowsupdate;network-vpn;network-proxy"
    assert hd.page_visibility("hide:network-vpn") == "hide:network-vpn;network-proxy"
    assert hd.page_visibility("showonly:display;about") == "showonly:display;about"


def test_extension_allowlist(tmp_path, monkeypatch):
    reg = {}
    monkeypatch.setattr(bp, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(bp, "atomic_write_json", lambda p, d: open(p, "w").write(json.dumps(d)))
    monkeypatch.setattr(bp, "reg_read", lambda p, n: (True, *reg[(p, n)]) if (p, n) in reg else (False, None, None))
    monkeypatch.setattr(bp, "reg_write", lambda p, n, t, d: reg.__setitem__((p, n), (t, d)))
    monkeypatch.setattr(bp, "reg_delete", lambda p, n: reg.pop((p, n), None))
    chrome, ff = r"SOFTWARE\Policies\Google\Chrome", r"SOFTWARE\Policies\Mozilla\Firefox"
    bp.BrowserPolicyManager.apply("standard")
    assert bp.BrowserPolicyManager.set_extensions({"mode": "allowlist", "chromium": ["cjpalhdlnbpafiamejdnhcphjbkeiagm"],
                                                   "firefox": ["uBlock0@raymondhill.net"]})
    chrome_settings = json.loads(reg[(chrome, "ExtensionSettings")][1])
    assert chrome_settings["*"] == {"blocked_permissions": ["proxy"], "installation_mode": "blocked"}
    assert chrome_settings["cjpalhdlnbpafiamejdnhcphjbkeiagm"] == {"installation_mode": "allowed"}
    vtype, data = reg[(ff, "ExtensionSettings")]
    assert vtype == bp.REG_MULTI_SZ and json.loads(data[0])["uBlock0@raymondhill.net"] == {"installation_mode": "allowed"}
    assert bp.BrowserPolicyManager.status()["drift"] == 0
    assert bp.BrowserPolicyManager.set_extensions({"mode": "proxy"})
    assert (ff, "ExtensionSettings") not in reg
    assert json.loads(reg[(chrome, "ExtensionSettings")][1]) == {"*": {"blocked_permissions": ["proxy"]}}


def test_tamper_checks():
    data = {"children": [], "windows": {
        "users": [{"name": "Papa", "sid": "S-1-5-21-1-2-3-1001", "enabled": True, "password_last_set": "x"}],
        "admins": ["S-1-5-21-1-2-3-1001"],
        "accessibility": [{"name": "sethc.exe", "status": "HashMismatch", "signer": ""},
                          {"name": "osk.exe", "status": "Valid", "signer": "CN=Microsoft Windows, O=Microsoft Corporation"}]},
        "registry": {}, "ifeo_foreign": [{"exe": "utilman.exe", "debugger": "cmd.exe"}], "nrpt": 2,
        "account_changes": [{"kind": "new_admin", "sid": "S-1-5-21-1-2-3-1009", "name": "hacker", "at": 1}]}
    checks = {c["id"]: c for c in compliance.evaluate(data)}
    assert checks["tamper.accessibility.sethc.exe"]["status"] == "fail"
    assert "tamper.accessibility.osk.exe" not in checks
    assert checks["tamper.ifeo"]["severity"] == "critical"
    assert checks["tamper.new_admin.S-1-5-21-1-2-3-1009"]["status"] == "fail"
    assert checks["tamper.nrpt"]["status"] == "warn"


def test_account_changes_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(compliance, "ACCOUNTS_SNAPSHOT", str(tmp_path / "snap.json"))
    monkeypatch.setattr(compliance, "atomic_write_json", lambda p, d: open(p, "w").write(json.dumps(d)))
    win = {"users": [{"sid": "S-1-5-21-1-2-3-1001", "name": "Papa"}, {"sid": "S-1-5-21-1-2-3-1002", "name": "Lea"}],
           "admins": ["S-1-5-21-1-2-3-1001"]}
    assert compliance.account_changes(win, now=1000) == []  # first run: baseline only
    win["users"].append({"sid": "S-1-5-21-1-2-3-1003", "name": "Nouveau"})
    win["admins"].append("S-1-5-21-1-2-3-1002")
    changes = compliance.account_changes(win, now=2000)
    assert {(c["kind"], c["name"]) for c in changes} == {("new_user", "Nouveau"), ("new_admin", "Lea")}
    compliance._expect_admin("S-1-5-21-1-2-3-1003")  # made admin from the console: not tampering
    win["admins"].append("S-1-5-21-1-2-3-1003")
    assert len(compliance.account_changes(win, now=3000)) == 2
    assert compliance.account_changes(win, now=2000 + 8 * 86400) == []  # kept 7 days
