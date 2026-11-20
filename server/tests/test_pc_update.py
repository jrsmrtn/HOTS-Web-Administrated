"""Update of HOTS Hosts on the PC from the console or automatically (hosts_editor/remote/selfupdate.py)."""

import hashlib
import io
import sys
import types

import pytest

sys.modules.setdefault("winreg", types.ModuleType("winreg"))

from hosts_editor.remote import selfupdate as su  # noqa: E402


@pytest.fixture()
def pc(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "STATE_FILE", str(tmp_path / "update_state.json"))
    monkeypatch.setattr(su, "UPDATE_DIR", str(tmp_path / "update"))
    monkeypatch.setattr(su, "PROGRAMDATA_DIR", str(tmp_path))
    monkeypatch.setattr(su, "APP_VERSION", "2.8.3")
    launched = []
    monkeypatch.setattr(su, "_launch", lambda path, delay: launched.append(path))

    class _Thread:
        def __init__(self, target, args, daemon, name):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)
    monkeypatch.setattr(su.threading, "Thread", _Thread)
    return launched


def _fake_github(monkeypatch, files):
    class Resp(io.BytesIO):
        def __init__(self, data, url):
            super().__init__(data)
            self.url = url

        def geturl(self):
            return self.url

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(url, method="GET", timeout=30):
        for suffix, data in files.items():
            if url.endswith(suffix):
                return Resp(data if isinstance(data, bytes) else b"", data if isinstance(data, str) else url)
        raise OSError(f"404 {url}")
    monkeypatch.setattr(su, "_open", fake_open)


def test_versions_and_sums():
    assert su.parse("v2.9.0") == (2, 9, 0) and su.parse("2.9.0") is None and su.parse("v2.9.0;calc") is None
    sums = "ab" * 32 + "  HOTS_Console_Server.zip\n" + "cd" * 32 + "  HOTS_Hosts_setup.exe\n"
    assert su.expected_sum(sums, su.INSTALLER) == "cd" * 32


def test_update_downloads_checks_and_launches(pc, monkeypatch, tmp_path):
    exe = b"MZ fake installer"
    good = hashlib.sha256(exe).hexdigest() + "  HOTS_Hosts_setup.exe\n"
    _fake_github(monkeypatch, {"/v2.9.0/SHA256SUMS.txt": good.encode(), "/v2.9.0/HOTS_Hosts_setup.exe": exe})
    with pytest.raises(ValueError):
        su.start_update("v2.8.3")  # not newer
    with pytest.raises(ValueError):
        su.start_update("https://evil.example/x.exe")
    out = su.start_update("v2.9.0", delay=0)
    assert out["target"] == "v2.9.0" and out["from"] == "2.8.3"
    assert len(pc) == 1 and open(pc[0], "rb").read() == exe and pc[0].endswith("HOTS_Hosts_setup_v2.9.0.exe")
    assert su.load_state()["state"] == "installing"
    with pytest.raises(RuntimeError):
        su.start_update("v2.9.0")  # one at a time
    # after the installer, the new agent records the result and cleans up
    monkeypatch.setattr(su, "APP_VERSION", "2.9.0")
    su.cleanup_after_start()
    assert su.load_state()["state"] == "done" and not [f for f in (tmp_path / "update").iterdir() if f.suffix == ".exe"]


def test_corrupted_download_is_refused(pc, monkeypatch, tmp_path):
    _fake_github(monkeypatch, {"/v2.9.0/SHA256SUMS.txt": ("0" * 64 + "  HOTS_Hosts_setup.exe\n").encode(),
                               "/v2.9.0/HOTS_Hosts_setup.exe": b"tampered"})
    with pytest.raises(RuntimeError):
        su.start_update("v2.9.0", delay=0)
    assert pc == [] and su.load_state()["state"] == "error" and "SHA-256" in su.load_state()["error"]
    assert not any((tmp_path / "update").iterdir())  # partial file removed


def test_automatic_updates(pc, monkeypatch):
    exe = b"MZ"
    _fake_github(monkeypatch, {"/releases/latest": "https://github.com/x/y/releases/tag/v2.9.0",
                               "/v2.9.0/SHA256SUMS.txt": (hashlib.sha256(exe).hexdigest() + "  HOTS_Hosts_setup.exe").encode(),
                               "/v2.9.0/HOTS_Hosts_setup.exe": exe})
    assert su.auto_check(False) is False and pc == []
    assert su.auto_check(True) is True and len(pc) == 1
    assert su.auto_check(True) is False  # checked less than 6 h ago
    # a failed version is not retried for 24 h, even after 6 h
    su._save_state(checked_at=0, state="error", target="v2.9.0", at=su.time.time())
    assert su.auto_check(True) is False and len(pc) == 1


def test_agent_actions(pc, monkeypatch, tmp_path):
    from hosts_editor import compliance
    from hosts_editor.remote import actions
    monkeypatch.setattr(compliance, "OPTIONS_FILE", str(tmp_path / "agent_options.json"))
    ok, res, err, _ = actions.execute("agent.set_options", {"auto_update": False})
    assert ok and res == {"auto_repair": True, "auto_update": False}
    ok, res, err, _ = actions.execute("agent.set_options", {"auto_update": "yes"})
    assert not ok
    ok, res, err, _ = actions.execute("agent.update", {"tag": "v1.0.0"})
    assert not ok and "déjà en version" in err


def test_latest_version_endpoint(admin, monkeypatch):
    from hots_server import updater
    from hots_server.app import ACTIONS
    assert ACTIONS["agent.update"] is True
    monkeypatch.setattr(updater, "latest_release", lambda force=False, timeout=10: {"tag": "v2.9.0", "url": "https://x"})
    assert admin.get("/api/updates/latest").json() == {"tag": "v2.9.0", "url": "https://x", "error": ""}
    admin.post("/api/auth/logout")
    assert admin.get("/api/updates/latest").status_code == 401
