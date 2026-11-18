"""Trusted networks and captive portals: the agent's decision logic (hosts_editor/core_networks.py)."""

import json
import sys
import types

sys.modules.setdefault("winreg", types.ModuleType("winreg"))

from hosts_editor import core_browser_policy as bp  # noqa: E402
from hosts_editor.core_networks import CAPTIVE_WINDOW, clean_policy, decide, match_trusted, network_key  # noqa: E402

SCHOOL = {"name": "Wi-Fi", "ssid": "College-Pedago", "gateway": "10.20.0.1", "gateway_mac": "00-1A-2B-3C-4D-5E",
          "dns": ["10.20.0.10", "10.20.0.11"], "domain": "college.lan"}
HOTEL = {"name": "Wi-Fi", "ssid": "Hotel-Guest", "gateway": "192.168.100.1", "gateway_mac": "aa:bb:cc:00:11:22",
         "dns": ["192.168.100.1"], "domain": ""}
HOME = {"name": "Wi-Fi", "ssid": "Freebox-1234", "gateway": "192.168.1.254", "gateway_mac": "f4:ca:e5:00:00:01",
        "dns": ["192.168.1.254"], "domain": ""}

POLICY = clean_policy({"captive_portal": True, "trusted": [
    {"id": "n1", "name": "Collège Jean Moulin", "ssid": "College-Pedago", "gateway_mac": "00:1a:2b:3c:4d:5e",
     "proxy": {"mode": "fixed", "server": "proxy.college.lan:3128"}}]})


def test_policy_is_validated():
    p = clean_policy({"trusted": [
        {"name": "ok", "ssid": "A", "gateway_mac": "00-11-22-33-44-55"},
        {"name": "ssid only", "ssid": "B", "match": "ssid"},
        {"name": "no identity"},
        {"name": "bad proxy", "ssid": "C", "match": "ssid", "proxy": {"mode": "fixed", "server": "x; rm -rf /"}},
        {"name": "pac", "ssid": "D", "match": "ssid", "proxy": {"mode": "pac", "pac_url": "http://wpad.lan/wpad.dat"}},
    ]})
    names = [n["name"] for n in p["trusted"]]
    assert names == ["ok", "ssid only", "bad proxy", "pac"]
    assert p["trusted"][0]["gateway_mac"] == "00:11:22:33:44:55" and p["trusted"][0]["match"] == "ssid+gateway"
    assert p["trusted"][2]["proxy"] == {"mode": "none"}
    assert p["trusted"][3]["proxy"] == {"mode": "pac", "pac_url": "http://wpad.lan/wpad.dat"}


def test_school_network_uses_its_own_dns_and_proxy():
    plan = decide([SCHOOL], cf_ok=False, policy=POLICY, state={}, now=1000)
    relax = plan["relax"]["Wi-Fi"]
    assert relax["reason"] == "trusted" and relax["dns"] == ["10.20.0.10", "10.20.0.11"] and relax["until"] is None
    assert plan["proxy"] == {"mode": "fixed", "server": "proxy.college.lan:3128"}
    assert plan["events"][0]["kind"] == "trusted"
    # still trusted later, no new event
    again = decide([SCHOOL], cf_ok=True, policy=POLICY, state=plan["state"], now=2000)
    assert again["relax"]["Wi-Fi"]["reason"] == "trusted" and again["events"] == []


def test_phone_hotspot_named_like_the_school_does_not_match():
    fake = dict(SCHOOL, gateway="192.168.43.1", gateway_mac="02:00:00:aa:bb:cc", dns=["192.168.43.1"])
    assert match_trusted(fake, POLICY["trusted"]) is None
    plan = decide([fake], cf_ok=True, policy=POLICY, state={}, now=1000)  # hotspot gives Internet: filter stays
    assert plan["relax"] == {} and plan["proxy"] is None


def test_captive_portal_window_then_restored_once_logged_in():
    plan = decide([HOTEL], cf_ok=False, policy=POLICY, state={}, now=1000)
    relax = plan["relax"]["Wi-Fi"]
    assert relax["reason"] == "captive" and relax["until"] == 1000 + CAPTIVE_WINDOW
    assert plan["events"][0]["kind"] == "captive_open"
    # still blocked 5 minutes later: window kept
    mid = decide([HOTEL], cf_ok=False, policy=POLICY, state=plan["state"], now=1300)
    assert mid["relax"]["Wi-Fi"]["until"] == relax["until"] and mid["events"] == []
    # portal passed: Cloudflare Family answers -> filter back immediately
    done = decide([HOTEL], cf_ok=True, policy=POLICY, state=mid["state"], now=1400)
    assert done["relax"] == {} and done["events"][0]["kind"] == "captive_closed"


def test_window_expires_and_is_limited_per_hour():
    state = {}
    plan = decide([HOME], cf_ok=False, policy=POLICY, state=state, now=0)
    assert plan["relax"]["Wi-Fi"]["reason"] == "captive"
    expired = decide([HOME], cf_ok=False, policy=POLICY, state=plan["state"], now=CAPTIVE_WINDOW + 1)
    assert expired["relax"] == {} and expired["events"][0]["kind"] == "captive_expired"
    assert expired["state"]["seen"][0]["ssid"] == "Freebox-1234"
    second = decide([HOME], cf_ok=False, policy=POLICY, state=expired["state"], now=CAPTIVE_WINDOW + 100)
    assert second["relax"] == {} and second["events"] == []  # 1 h cooldown after an expired window
    later = decide([HOME], cf_ok=False, policy=POLICY, state=second["state"], now=3600 + CAPTIVE_WINDOW + 200)
    assert later["relax"]["Wi-Fi"]["reason"] == "captive"  # a new hour, a new window
    third = decide([HOME], cf_ok=False, policy=POLICY, state=later["state"], now=3600 + 2 * CAPTIVE_WINDOW + 300)
    third = decide([HOME], cf_ok=False, policy=POLICY, state=third["state"], now=3600 + 2 * CAPTIVE_WINDOW + 400)
    assert third["relax"] == {}  # at most 2 windows per hour


def test_captive_windows_can_be_disabled():
    policy = dict(POLICY, captive_portal=False)
    plan = decide([HOTEL], cf_ok=False, policy=policy, state={}, now=1000)
    assert plan["relax"] == {} and plan["events"][0]["kind"] == "restrictive"


def test_unknown_reachability_and_filter_off_change_nothing():
    plan = decide([HOTEL], cf_ok=False, policy=POLICY, state={}, now=1000)
    keep = decide([HOTEL], cf_ok=None, policy=POLICY, state=plan["state"], now=1100)
    assert keep["relax"] == plan["relax"]
    no_filter = decide([SCHOOL], cf_ok=False, policy=POLICY, state={}, now=1000, dns_enforced=False)
    assert no_filter["relax"] == {} and no_filter["proxy"]["mode"] == "fixed"  # browsers still need the proxy


def test_changing_network_on_same_adapter_restarts():
    plan = decide([HOTEL], cf_ok=False, policy=POLICY, state={}, now=1000)
    moved = decide([HOME], cf_ok=True, policy=POLICY, state=plan["state"], now=1100)
    assert moved["relax"] == {}
    assert network_key(HOTEL) != network_key(HOME)


def test_browser_proxy_follows_the_network(tmp_path, monkeypatch):
    reg = {}
    monkeypatch.setattr(bp, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(bp, "atomic_write_json", lambda p, d: open(p, "w").write(json.dumps(d)))
    monkeypatch.setattr(bp, "reg_read", lambda p, n: (True, *reg[(p, n)]) if (p, n) in reg else (False, None, None))
    monkeypatch.setattr(bp, "reg_write", lambda p, n, t, d: reg.__setitem__((p, n), (t, d)))
    monkeypatch.setattr(bp, "reg_delete", lambda p, n: reg.pop((p, n), None))
    chrome, ff = r"SOFTWARE\Policies\Google\Chrome", r"SOFTWARE\Policies\Mozilla\Firefox\Proxy"
    assert bp.BrowserPolicyManager.apply("standard")
    assert json.loads(reg[(chrome, "ProxySettings")][1]) == {"ProxyMode": "direct"}
    assert bp.BrowserPolicyManager.set_network_proxy({"mode": "fixed", "server": "proxy.college.lan:3128"})
    assert json.loads(reg[(chrome, "ProxySettings")][1]) == {"ProxyMode": "fixed_servers", "ProxyServer": "proxy.college.lan:3128"}
    assert reg[(ff, "Mode")][1] == "manual" and reg[(ff, "HTTPProxy")][1] == "proxy.college.lan:3128"
    assert not bp.BrowserPolicyManager.set_network_proxy({"mode": "fixed", "server": "proxy.college.lan:3128"})
    assert bp.BrowserPolicyManager.set_network_proxy(None)  # back home: no proxy allowed again
    assert json.loads(reg[(chrome, "ProxySettings")][1]) == {"ProxyMode": "direct"}
    assert (ff, "HTTPProxy") not in reg and reg[(ff, "Mode")][1] == "none"
    assert bp.BrowserPolicyManager.status()["drift"] == 0
