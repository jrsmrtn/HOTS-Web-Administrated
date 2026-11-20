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
    assert kinds == ["hash"] * 4  # a folder snapshot is fingerprints only (compact), no path rule
    assert {e["source"] for e in exc} == {r"C:\MCNL\MCNL.EXE", r"C:\MCNL\APPS\FRANCAIS\E-VERBE.X86.EXE",
                                         r"C:\MCNL\OUTILS\JRE\BIN\JAVAW.EXE", r"C:\MCNL\OUTILS\JRE\BIN\JAVA.EXE"}
    # scanning again replaces the previous scan of this folder (no duplicates, no leftovers)
    found.pop()
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


def test_folder_scan_includes_hidden_folders():
    """Regression: MCNL keeps its programs in hidden subfolders; Get-ChildItem skips them without -Force."""
    assert "-Recurse -Force" in al._PS_FOLDER_INFO


def _many_hash_exceptions(n):
    return [al.clean_exception({"kind": "hash", "hash": f"{i:064x}", "file_name": f"PROG{i}.EXE", "file_length": 1000})
            for i in range(n)]


def test_large_policy_goes_through_the_mdm_bridge_from_files(tmp_path, monkeypatch):
    """Regression: 345 approved programs never reached Windows (policy passed on the PowerShell command line,
    which is limited to 32 767 characters) while the console listed them as approved."""
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    monkeypatch.setattr("hosts_editor.core_antispy._is_admin", lambda: True)
    monkeypatch.setattr(al, "_appid_start", lambda value=None: 2)
    monkeypatch.setattr(al, "_run", lambda *a, **k: None)
    monkeypatch.setattr(al, "_local_policy_xml", lambda: "")
    scripts = []

    def fake_ps(script, timeout=120):
        scripts.append(script)
        if "Get-AppLockerPolicy -Effective" in script:  # Windows Home: the local policy is not the one in force
            return types.SimpleNamespace(returncode=0, stdout="<AppLockerPolicy />", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="OK", stderr="")
    monkeypatch.setattr(al, "_ps", fake_ps)
    exceptions = _many_hash_exceptions(345)
    assert al.AppLockerManager._apply("enforce", [CHILD], exceptions)
    st = al._load_state()
    assert st["method"] == "mdm" and st["last_apply"]["ok"] and st["last_apply"]["exceptions"] == 345
    csp = [s for s in scripts if "New-CimInstance" in s]
    assert csp and all(len(s) < 8000 for s in csp)  # XML read from files, not on the command line
    assert not any("PROG100.EXE" in s for s in scripts)


def test_local_policy_must_be_this_revision_and_clears_old_mdm(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    monkeypatch.setattr("hosts_editor.core_antispy._is_admin", lambda: True)
    monkeypatch.setattr(al, "_appid_start", lambda value=None: 2)
    monkeypatch.setattr(al, "_run", lambda *a, **k: None)
    monkeypatch.setattr(al, "_local_policy_xml", lambda: "")
    written = {}

    def fake_set_local(xml):
        written["xml"] = xml
        return True
    monkeypatch.setattr(al, "_set_local_policy", fake_set_local)
    mdm_calls = []
    monkeypatch.setattr(al, "_set_mdm_policy", lambda c: mdm_calls.append(c) or True)
    monkeypatch.setattr(al, "_conversion_result", lambda since: (True, ""))  # Windows log: 8001 policy applied
    effective = {"xml": f"{al.RULE_PREFIX} old policy rev:0000000000000000"}
    monkeypatch.setattr(al, "_ps", lambda script, timeout=120: types.SimpleNamespace(returncode=0, stdout=effective["xml"], stderr=""))
    # an older HOTS policy in force is not taken for the new one: fallback to the MDM bridge
    assert al.AppLockerManager._apply("enforce", [CHILD], _many_hash_exceptions(3))
    assert al._load_state()["method"] == "mdm" and mdm_calls and mdm_calls[-1]
    # the new revision is in force: local policy, and our old MDM policy is removed
    mdm_calls.clear()
    monkeypatch.setattr(al, "_ps", lambda script, timeout=120: types.SimpleNamespace(returncode=0, stdout=written["xml"], stderr=""))
    assert al.AppLockerManager._apply("enforce", [CHILD], _many_hash_exceptions(4))
    assert al._load_state()["method"] == "local" and mdm_calls == [None]
    assert "rev:" in written["xml"]


def test_failed_application_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    monkeypatch.setattr("hosts_editor.core_antispy._is_admin", lambda: True)
    monkeypatch.setattr(al, "_appid_start", lambda value=None: 2)
    monkeypatch.setattr(al, "_run", lambda *a, **k: None)
    monkeypatch.setattr(al, "_local_policy_xml", lambda: "")
    monkeypatch.setattr(al, "_ps", lambda script, timeout=120: types.SimpleNamespace(returncode=1, stdout="", stderr="Accès refusé"))
    assert not al.AppLockerManager._apply("enforce", [CHILD], _many_hash_exceptions(2))
    last = al.AppLockerManager.status()["last_apply"]
    assert last["ok"] is False and "Accès refusé" in last["error"]


def test_diagnose_reads_what_windows_says(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    seen = {}
    out = {"counts": {"mode": "Enabled", "hash": 345, "publisher": 4, "path": 20},
           "tests": {"user": CHILD, "path": r"C:\MCNL\OUTILS\JRE\BIN\JAVAW.EXE", "decision": "Denied", "rule": "HOTS compte enfant"},
           "events": [], "mdm": 0, "service": "Running"}

    def fake_ps(script, timeout=120):
        seen["script"] = script
        return types.SimpleNamespace(returncode=0, stdout=_json.dumps(out), stderr="")
    monkeypatch.setattr(al, "_ps", fake_ps)
    d = al.diagnose([r"%OSDRIVE%\MCNL\OUTILS\JRE\BIN\JAVAW.EXE", "C:\\x'; Remove-Item C:\\ -Recurse #"], [CHILD, "not-a-sid"])
    assert d["counts"]["hash"] == 345 and d["tests"][0]["decision"] == "Denied"  # single object -> list
    assert "Remove-Item" not in seen["script"] and "not-a-sid" not in seen["script"]
    assert "JAVAW.EXE" in seen["script"] and CHILD in seen["script"]


def test_folder_with_too_many_programs_is_refused_not_cut(tmp_path, monkeypatch):
    """Regression: the scan stopped at 400 programs; MCNL's Java and utilities stayed blocked silently."""
    import json as _json
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    (tmp_path / "BIG").mkdir()
    monkeypatch.setattr(al, "expand_event_path", lambda p: str(tmp_path / "BIG"))
    monkeypatch.setattr(al, "MAX_FOLDER_FILES", 5)
    items = [{"hash": f"0x{i:064X}", "file": f"P{i}.EXE", "length": 1, "path": f"C:\\BIG\\P{i}.EXE"} for i in range(6)]
    monkeypatch.setattr(al, "_ps", lambda script, timeout=120: types.SimpleNamespace(returncode=0, stdout=_json.dumps(items), stderr=""))
    assert not al.AppLockerManager.add_exception({"kind": "folder_scan", "path": r"C:\BIG"})
    assert "Plus de 5 programmes" in al.AppLockerManager.last_error and al.AppLockerManager.current()["exceptions"] == []



def test_policy_stays_within_windows_limits(tmp_path, monkeypatch):
    """Regression: 375 fingerprints + 94 publishers were too big for Windows ("conversion de stratégie AppID a
    échoué : ACL non valide"), the old policy stayed in force and MCNL stayed blocked."""
    hs = [al.clean_exception({"kind": "hash", "hash": f"{i:064x}", "file_name": f"P{i}.EXE", "file_length": 1}) for i in range(470)]
    root = ET.fromstring(al.build_policy_xml([CHILD], "enforce", hs))
    deny = next(r for r in root.find("RuleCollection").findall("FilePathRule") if r.find("Exceptions") is not None)
    conds = deny.find("Exceptions").findall("FileHashCondition")
    assert len(conds) == 1 and len(conds[0].findall("FileHash")) == 470
    assert al.estimate_acl_bytes(hs) < al.ACL_BUDGET
    pubs = [al.clean_exception({"kind": "publisher", "publisher": "O=RARE IDEAS, LLC, L=SPRINGFIELD, S=MASSACHUSETTS, C=US",
                                "product": f"PORTABLE APP {i}"}) for i in range(94)]
    assert al.estimate_acl_bytes(pubs + hs[:375]) > al.ACL_BUDGET
    # too big: refused with a clear message, nothing sent to Windows
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    monkeypatch.setattr("hosts_editor.core_antispy._is_admin", lambda: True)
    monkeypatch.setattr(al, "_appid_start", lambda value=None: 2)
    monkeypatch.setattr(al, "_run", lambda *a, **k: None)
    monkeypatch.setattr(al, "_local_policy_xml", lambda: "")
    monkeypatch.setattr(al, "_set_local_policy", lambda xml: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert not al.AppLockerManager._apply("enforce", [CHILD], pubs + hs[:375])
    assert "Ko de règles" in al.AppLockerManager.status()["last_apply"]["error"]


def test_conversion_failure_reported_by_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "STATE_FILE", str(tmp_path / "applocker.json"))
    monkeypatch.setattr("hosts_editor.core_antispy._is_admin", lambda: True)
    monkeypatch.setattr(al, "_appid_start", lambda value=None: 2)
    monkeypatch.setattr(al, "_run", lambda *a, **k: None)
    monkeypatch.setattr(al, "_local_policy_xml", lambda: "")
    monkeypatch.setattr(al, "_set_local_policy", lambda xml: True)
    monkeypatch.setattr(al, "_effective", lambda revision="": True)
    monkeypatch.setattr(al, "_set_mdm_policy", lambda c: (_ for _ in ()).throw(AssertionError("no MDM fallback")) if c else True)
    monkeypatch.setattr(al, "_ps", lambda script, timeout=120: types.SimpleNamespace(
        returncode=0, stdout="8000|La conversion de stratégie AppID a échoué. Status Structure de liste de contrôle de l'accès (ACL) non valide.", stderr=""))
    assert not al.AppLockerManager._apply("enforce", [CHILD], [])
    last = al.AppLockerManager.status()["last_apply"]
    assert last["ok"] is False and "ACL" in last["error"]
