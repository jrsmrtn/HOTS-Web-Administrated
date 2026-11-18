"""Anti-portable mode: AppLocker policy generation and audit summaries (no Windows needed)."""

import sys
import types
import xml.etree.ElementTree as ET

import pytest

sys.modules.setdefault("winreg", types.ModuleType("winreg"))

from hosts_editor import core_applocker as al  # noqa: E402

CHILD = "S-1-5-21-111-222-333-1002"
CHILD2 = "S-1-5-21-111-222-333-1003"


def _rules(xml):
    root = ET.fromstring(xml)
    coll = root.find("RuleCollection")
    return coll, coll.findall("FilePathRule")


def test_policy_restricts_only_children():
    roblox = al.clean_exception({"kind": "publisher", "publisher": "O=ROBLOX CORPORATION, L=SAN MATEO, S=CALIFORNIA, C=US",
                                 "product": "ROBLOX", "label": "Roblox"})
    tool = al.clean_exception({"kind": "hash", "hash": "AB" * 32, "file_name": "devoirs.exe", "file_length": 1234})
    xml = al.build_policy_xml([CHILD, CHILD2], "enforce", [roblox, tool])
    coll, rules = _rules(xml)
    assert coll.get("Type") == "Exe" and coll.get("EnforcementMode") == "Enabled"
    allow = [r for r in rules if r.get("Action") == "Allow"]
    assert len(allow) == 1 and allow[0].get("UserOrGroupSid") == "S-1-1-0"  # parents and future accounts unaffected
    deny = [r for r in rules if r.get("Action") == "Deny"]
    assert {r.get("UserOrGroupSid") for r in deny} == {CHILD, CHILD2}
    main = next(r for r in deny if r.get("UserOrGroupSid") == CHILD and r.find("Exceptions") is not None)
    paths = [c.get("Path") for c in main.find("Exceptions").findall("FilePathCondition")]
    assert paths == [r"%WINDIR%\*", r"%PROGRAMFILES%\*"]
    pubs = [c.get("PublisherName") for c in main.find("Exceptions").findall("FilePublisherCondition")]
    assert al.MICROSOFT_PUBLISHER in pubs and roblox["publisher"] in pubs
    h = main.find("Exceptions").find("FileHashCondition/FileHash")
    assert h.get("Data") == "0x" + "AB" * 32 and h.get("SourceFileName") == "devoirs.exe"
    temp = [r for r in deny if r.find("Conditions/FilePathCondition").get("Path") == r"%WINDIR%\Temp\*"]
    assert len(temp) == 2  # writable Windows folder denied for each child
    assert len({r.get("Id") for r in rules}) == len(rules)  # unique rule ids
    assert al.build_policy_xml([CHILD, CHILD2], "enforce", [roblox, tool]) == xml  # deterministic


def test_audit_mode_and_guards():
    coll, _ = _rules(al.build_policy_xml([CHILD], "audit", []))
    assert coll.get("EnforcementMode") == "AuditOnly"
    with pytest.raises(ValueError):
        al.build_policy_xml([], "audit", [])
    with pytest.raises(ValueError):
        al.build_policy_xml(["S-1-1-0"], "audit", [])  # never "everyone"
    for bad in ({"kind": "publisher", "publisher": 'O=X" Action="Allow'}, {"kind": "hash", "hash": "zz"},
                {"kind": "path", "path": "C:\\Users\\*"}, None):
        with pytest.raises(ValueError):
            al.clean_exception(bad)


def test_audit_events_become_suggestions():
    events = [
        {"id": 8003, "time": "2026-11-18T10:00:00Z", "user": CHILD,
         "path": r"%OSDRIVE%\USERS\LEA\APPDATA\LOCAL\ROBLOX\VERSIONS\ROBLOXPLAYERBETA.EXE", "hash": "0x" + "CD" * 32,
         "fqbn": r"O=ROBLOX CORPORATION, L=SAN MATEO, S=CALIFORNIA, C=US\ROBLOX\ROBLOXPLAYERBETA.EXE\1.2.3.4"},
        {"id": 8003, "time": "2026-11-18T11:00:00Z", "user": CHILD,
         "path": r"%OSDRIVE%\USERS\LEA\APPDATA\LOCAL\ROBLOX\VERSIONS\ROBLOXPLAYERBETA.EXE", "hash": "0x" + "CD" * 32,
         "fqbn": r"O=ROBLOX CORPORATION, L=SAN MATEO, S=CALIFORNIA, C=US\ROBLOX\ROBLOXPLAYERBETA.EXE\1.2.3.4"},
        {"id": 8004, "time": "2026-11-18T12:00:00Z", "user": CHILD,
         "path": r"%OSDRIVE%\USERS\LEA\DOWNLOADS\TOR BROWSER\BROWSER\FIREFOX.EXE", "hash": "0x" + "EF" * 32, "fqbn": "-"},
        {"id": 8003, "time": "2026-11-18T12:00:00Z", "user": "S-1-5-21-111-222-333-1001",
         "path": r"%OSDRIVE%\USERS\PAPA\X.EXE", "hash": "0x" + "00" * 32, "fqbn": "-"},
    ]
    summary = al.summarize_events(events, [CHILD])
    assert [g["name"] for g in summary] == ["FIREFOX.EXE", "ROBLOXPLAYERBETA.EXE"]  # parent's events ignored
    tor, roblox = summary
    assert tor["blocked"] == 1 and not tor["signed"] and tor["suggestion"]["kind"] == "hash"
    assert roblox["count"] == 2 and roblox["suggestion"] == {
        "kind": "publisher", "publisher": "O=ROBLOX CORPORATION, L=SAN MATEO, S=CALIFORNIA, C=US",
        "product": "ROBLOX", "label": "ROBLOX (Roblox Corporation)"}
    # the suggestions are accepted as exceptions as-is
    al.clean_exception(roblox["suggestion"])
    al.clean_exception(dict(tor["suggestion"], file_length=0))


def test_store_apps_stay_allowed():
    """Regression: with only the Exe collection, Windows blocked every Store app once AppLocker was on
    (Microsoft Store, Photos, HEIF extension: "bloquée par votre administrateur système")."""
    for mode, enforcement in (("enforce", "Enabled"), ("audit", "AuditOnly")):
        root = ET.fromstring(al.build_policy_xml([CHILD], mode, []))
        appx = [c for c in root.findall("RuleCollection") if c.get("Type") == "Appx"]
        assert len(appx) == 1 and appx[0].get("EnforcementMode") == enforcement
        rules = appx[0].findall("FilePublisherRule")
        assert len(rules) == 1 and rules[0].get("Action") == "Allow" and rules[0].get("UserOrGroupSid") == "S-1-1-0"
        cond = rules[0].find("Conditions/FilePublisherCondition")
        assert (cond.get("PublisherName"), cond.get("ProductName"), cond.get("BinaryName")) == ("*", "*", "*")
        assert not appx[0].findall("FilePathRule")  # nothing denied among packaged apps
    parts = al.split_collections(al.build_policy_xml([CHILD], "enforce", []))
    assert set(parts) == {"Exe", "Appx"} and all(p.startswith("<RuleCollection") for p in parts.values())


def test_outdated_policy_is_reapplied(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    al._save_state({"mode": "enforce", "children": [CHILD], "exceptions": [], "method": "local"})  # version 1
    monkeypatch.setattr("hosts_editor.compliance.child_sids", lambda: [CHILD])
    monkeypatch.setattr(al, "_effective", lambda: True)
    applied = []
    monkeypatch.setattr(al.AppLockerManager, "_apply", staticmethod(lambda *a: applied.append(a) or True))
    assert al.AppLockerManager.repair() is True and applied == [("enforce", [CHILD], [])]
    al._save_state({"mode": "enforce", "children": [CHILD], "exceptions": [], "method": "local",
                    "policy_version": al.POLICY_VERSION})
    applied.clear()
    assert al.AppLockerManager.repair() is False and applied == []
