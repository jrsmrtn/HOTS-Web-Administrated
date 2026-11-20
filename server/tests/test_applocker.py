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


def test_every_blocked_program_can_be_allowed():
    """Regression: no "Autoriser" button for programs signed by a person (CN=...) or events without hash."""
    events = [
        {"id": 8004, "time": "2026-11-20T10:00:00Z", "user": CHILD, "path": r"%OSDRIVE%\USERS\LEA\DOWNLOADS\OUTIL.EXE",
         "hash": "", "fqbn": r"CN=JEAN DUPONT, O=JEAN DUPONT, C=FR\OUTIL\OUTIL.EXE\1.0"},
        {"id": 8004, "time": "2026-11-20T10:01:00Z", "user": CHILD, "path": r"%OSDRIVE%\USERS\LEA\DESKTOP\JEU.EXE",
         "hash": "", "fqbn": "-"},
    ]
    groups = {g["name"]: g for g in al.summarize_events(events, [CHILD])}
    assert groups["OUTIL.EXE"]["suggestion"]["kind"] == "publisher"
    assert al.clean_exception(groups["OUTIL.EXE"]["suggestion"])["publisher"].startswith("CN=JEAN DUPONT")
    assert groups["JEU.EXE"]["suggestion"] == {"kind": "file", "path": r"%OSDRIVE%\USERS\LEA\DESKTOP\JEU.EXE",
                                               "label": "JEU.EXE"}
    with pytest.raises(ValueError):
        al.clean_exception({"kind": "file", "path": "C:\\Users\\*"})


def test_file_exception_is_resolved_on_the_pc(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    exe = tmp_path / "JEU.EXE"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(al, "expand_event_path", lambda p: str(exe) if "DESKTOP" in p else "")
    outputs = {"value": '{"hash":"0x' + "AB" * 32 + '","file":"JEU.EXE","length":2}'}
    monkeypatch.setattr(al, "_ps", lambda script, timeout=120: types.SimpleNamespace(returncode=0, stdout=outputs["value"], stderr=""))
    ok = al.AppLockerManager.add_exception({"kind": "file", "path": r"%OSDRIVE%\USERS\LEA\DESKTOP\JEU.EXE", "label": "JEU.EXE"})
    assert ok  # mode off: stored only
    exc = al.AppLockerManager.current()["exceptions"][0]
    assert exc["kind"] == "hash" and exc["hash"] == "0x" + "AB" * 32 and exc["file_length"] == 2
    outputs["value"] = ('{"hash":"0x' + "CD" * 32 + '","file":"JEU.EXE","length":2,'
                        '"publisher":"O=ROBLOX CORPORATION, L=SAN MATEO, S=CALIFORNIA, C=US","product":"ROBLOX"}')
    assert al.AppLockerManager.add_exception({"kind": "file", "path": r"%OSDRIVE%\USERS\LEA\DESKTOP\JEU.EXE"})
    assert al.AppLockerManager.current()["exceptions"][-1]["kind"] == "publisher"
    # program on a USB stick: clear message instead of a silent failure
    assert not al.AppLockerManager.add_exception({"kind": "file", "path": r"%REMOVABLE%\JEU.EXE"})
    assert "clé USB" in al.AppLockerManager.last_error


def test_folder_rules():
    folder = al.clean_exception({"kind": "folder", "path": r"%OSDRIVE%\MCNL"})
    assert folder["path"] == r"%OSDRIVE%\MCNL" and folder["label"] == "MCNL"
    root = ET.fromstring(al.build_policy_xml([CHILD], "enforce", [folder]))
    exe = next(c for c in root.findall("RuleCollection") if c.get("Type") == "Exe")
    deny = next(r for r in exe.findall("FilePathRule") if r.get("Action") == "Deny" and r.find("Exceptions") is not None)
    assert r"%OSDRIVE%\MCNL\*" in [c.get("Path") for c in deny.find("Exceptions").findall("FilePathCondition")]
    for broad in (r"C:\Users\Lea\Downloads", r"%OSDRIVE%\USERS\LEA\APPDATA\LOCAL", "C:\\", r"C:\ProgramData", "%REMOVABLE%"):
        with pytest.raises(ValueError):
            al.clean_exception({"kind": "folder", "path": broad})
        with pytest.raises(ValueError):
            al.clean_exception({"kind": "folder_scan", "path": broad})


def test_folder_scan_allows_what_is_there_now(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    (tmp_path / "MCNL").mkdir()
    monkeypatch.setattr(al, "expand_event_path", lambda p: str(tmp_path / "MCNL"))
    found = [
        {"hash": "0x" + "11" * 32, "file": "MCNL.EXE", "length": 10, "path": r"C:\MCNL\MCNL.EXE"},
        {"hash": "0x" + "22" * 32, "file": "E-VERBE.X86.EXE", "length": 20, "path": r"C:\MCNL\APPS\FRANCAIS\E-VERBE.X86.EXE"},
        {"hash": "0x" + "33" * 32, "file": "JAVAW.EXE", "length": 30, "path": r"C:\MCNL\OUTILS\JRE\BIN\JAVAW.EXE",
         "publisher": "O=ORACLE AMERICA, INC., L=REDWOOD SHORES, S=CALIFORNIA, C=US", "product": "JAVA(TM) PLATFORM SE 8"},
        {"hash": "0x" + "44" * 32, "file": "JAVA.EXE", "length": 30, "path": r"C:\MCNL\OUTILS\JRE\BIN\JAVA.EXE",
         "publisher": "O=ORACLE AMERICA, INC., L=REDWOOD SHORES, S=CALIFORNIA, C=US", "product": "JAVA(TM) PLATFORM SE 8"},
    ]
    import json as _json
    monkeypatch.setattr(al, "_ps", lambda script, timeout=120: types.SimpleNamespace(returncode=0, stdout=_json.dumps(found), stderr=""))
    assert al.AppLockerManager.add_exception({"kind": "folder_scan", "path": r"%OSDRIVE%\MCNL", "label": "MCNL"})
    exc = al.AppLockerManager.current()["exceptions"]
    kinds = sorted(e["kind"] for e in exc)
    assert kinds == ["hash", "hash", "publisher"]  # one rule per publisher, one per unsigned program, no path rule
    assert {e["source"] for e in exc if e["kind"] == "hash"} == {r"C:\MCNL\MCNL.EXE", r"C:\MCNL\APPS\FRANCAIS\E-VERBE.X86.EXE"}
    # scanning again does not duplicate
    assert al.AppLockerManager.add_exception({"kind": "folder_scan", "path": r"%OSDRIVE%\MCNL"})
    assert len(al.AppLockerManager.current()["exceptions"]) == 3
    monkeypatch.setattr(al, "_ps", lambda script, timeout=120: types.SimpleNamespace(returncode=0, stdout="[]", stderr=""))
    assert not al.AppLockerManager.add_exception({"kind": "folder_scan", "path": r"%OSDRIVE%\MCNL"})
    assert "Aucun programme" in al.AppLockerManager.last_error


def test_file_approval_remembers_its_program(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    exe = tmp_path / "MCNL.EXE"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(al, "expand_event_path", lambda p: str(exe))
    monkeypatch.setattr(al, "_ps", lambda script, timeout=120: types.SimpleNamespace(
        returncode=0, stdout='{"hash":"0x' + "AB" * 32 + '","file":"MCNL.EXE","length":2}', stderr=""))
    assert al.AppLockerManager.add_exception({"kind": "file", "path": r"%OSDRIVE%\MCNL\MCNL.EXE"})
    assert al.AppLockerManager.current()["exceptions"][0]["source"] == r"%OSDRIVE%\MCNL\MCNL.EXE"
