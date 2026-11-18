"""One-click update from the Administration page (hots_server/updater.py)."""

import hashlib
import json
import os
import zipfile

import pytest

from hots_server import settings, updater


def test_versions():
    assert updater.parse("v2.7.1") == (2, 7, 1) and updater.parse("2.7.1") is None and updater.parse("v2.7") is None
    assert updater.newer("v2.10.0", "v2.9.9") and not updater.newer("v2.7.1", "v2.7.1")
    assert updater.newer("v2.8.0", "dev") and not updater.newer("latest", "v1.0.0")


def test_checksum_line_parsing():
    sums = "aa" * 32 + "  HOTS_Hosts_setup.exe\n" + "bb" * 32 + " *HOTS_Console_Server.zip\nbad line\n"
    assert updater.expected_sum(sums, "HOTS_Console_Server.zip") == "bb" * 32
    assert updater.expected_sum(sums, "other.zip") == ""


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)


def test_safe_extract_refuses_paths_outside(tmp_path):
    bad = tmp_path / "bad.zip"
    _zip(bad, {"HOTS_Console_Server/hots_server/__init__.py": "", "../../evil.sh": "rm -rf /"})
    with pytest.raises(RuntimeError):
        updater.safe_extract(str(bad), str(tmp_path / "out"))
    assert not (tmp_path / "evil.sh").exists()
    good = tmp_path / "good.zip"
    _zip(good, {"HOTS_Console_Server/hots_server/__init__.py": "", "HOTS_Console_Server/VERSION": "v9.9.9\n"})
    folder = updater.safe_extract(str(good), str(tmp_path / "out2"))
    assert folder.endswith("HOTS_Console_Server")


@pytest.fixture()
def shared_install(tmp_path, monkeypatch):
    """A console installed by install-mutualise.sh: ~/hots-console/{hots, hots.env, data, htaccess.path}."""
    home = tmp_path / "hots-console"
    (home / "data").mkdir(parents=True)
    (home / "hots").write_text("#!/bin/sh\n")
    (home / "hots.env").write_text("HOTS_PORT=1\n")
    (home / "htaccess.path").write_text(str(tmp_path / "public_html" / ".htaccess") + "\n")
    monkeypatch.setattr(settings, "DATA_DIR", str(home / "data"))
    return home


def test_install_mode_detection(shared_install, monkeypatch, tmp_path):
    assert updater.install_mode() == {"mode": "shared", "dir": str(shared_install), "reason": ""}
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "elsewhere"))
    assert updater.install_mode()["mode"] == "manual"


def test_shared_update_end_to_end(shared_install, monkeypatch, tmp_path):
    marker = tmp_path / "installer-args.txt"
    script = f'#!/bin/bash\necho "$@" > "{marker}"\n'
    pkg = tmp_path / "pkg.zip"
    _zip(pkg, {"HOTS_Console_Server/hots_server/__init__.py": "", "HOTS_Console_Server/VERSION": "v9.9.9\n",
               "HOTS_Console_Server/install-mutualise.sh": script})
    digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
    sums = {"ok": f"{digest}  HOTS_Console_Server.zip\n", "bad": "0" * 64 + "  HOTS_Console_Server.zip\n"}
    mode = {"sums": "bad"}

    def fake_download(url, dest, timeout=120):
        assert url.startswith(f"https://github.com/{updater.REPO}/releases/download/v9.9.9/")
        if url.endswith(".zip"):
            with open(pkg, "rb") as src, open(dest, "wb") as out:
                out.write(src.read())
        else:
            with open(dest, "w") as out:
                out.write(sums[mode["sums"]])

    monkeypatch.setattr(updater, "_download", fake_download)
    # a corrupted download is refused and nothing is installed
    assert updater.run_update("shared", "v9.9.9") == 1
    st = updater.read_status()
    assert st["state"] == "error" and "SHA-256" in st["error"] and not marker.exists()
    mode["sums"] = "ok"
    assert updater.run_update("shared", "v9.9.9") == 0
    st = updater.read_status()
    assert st["state"] == "done" and st["version"] == "v9.9.9"
    args = marker.read_text().split()
    assert args[:3] == ["--yes", "--dir", str(shared_install)]
    assert args[3:] == ["--webroot", str(tmp_path / "public_html")]
    assert not os.path.exists(os.path.join(settings.DATA_DIR, "update-work"))
    # a package with another version inside is refused
    _zip(pkg, {"HOTS_Console_Server/hots_server/__init__.py": "", "HOTS_Console_Server/VERSION": "v1.0.0\n",
               "HOTS_Console_Server/install-mutualise.sh": script})
    sums["ok"] = hashlib.sha256(pkg.read_bytes()).hexdigest() + "  HOTS_Console_Server.zip\n"
    assert updater.run_update("shared", "v9.9.9") == 1 and "version attendue" in updater.read_status()["error"]


def test_vps_request_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(updater, "install_mode", lambda: {"mode": "vps", "dir": "/opt/hots-console", "reason": ""})
    updater.request_update("v9.9.9", "admin@example.com")
    assert json.loads((tmp_path / "update-request").read_text())["tag"] == "v9.9.9"
    assert updater.is_running()
    with pytest.raises(RuntimeError):
        updater.request_update("v9.9.9")  # one at a time
    # the root unit reads and deletes the request, even when the tag is wrong
    (tmp_path / "update-request").write_text(json.dumps({"tag": "../../x"}))
    assert updater.run_update("vps", request_file=str(tmp_path / "update-request")) == 1
    assert not (tmp_path / "update-request").exists()


def test_update_api(admin, monkeypatch, tmp_path):
    client = admin
    monkeypatch.setattr(updater, "latest_release", lambda force=False, timeout=10: {"tag": "v9.9.9", "url": "https://x"})
    monkeypatch.setattr(updater, "current_version", lambda: "v2.7.1")
    data = client.get("/api/admin/update").json()
    assert data["current"] == "v2.7.1" and data["latest"]["tag"] == "v9.9.9" and data["available"] is True
    assert data["install"]["mode"] == "manual"
    r = client.post("/api/admin/update", json={"tag": "v9.9.9"})
    assert r.status_code == 409  # installed from source: no one-click update
    calls = []
    monkeypatch.setattr(updater, "request_update", lambda tag, by="": calls.append((tag, by)) or {"state": "queued"})
    assert client.post("/api/admin/update", json={"tag": "v9.0.0"}).status_code == 400  # only the latest
    assert client.post("/api/admin/update", json={"tag": "v9.9.9"}).status_code == 200
    assert calls == [("v9.9.9", "admin@example.com")]
    monkeypatch.setattr(updater, "current_version", lambda: "v9.9.9")
    assert client.post("/api/admin/update", json={"tag": "v9.9.9"}).status_code == 400  # already up to date
    # a client account cannot see or start updates
    client.post("/api/admin/accounts", json={"account_name": "Client", "email": "client@example.com",
                                             "password": "motdepasse-client"})
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": "client@example.com", "password": "motdepasse-client"})
    assert client.get("/api/admin/update").status_code == 403
    assert client.post("/api/admin/update", json={"tag": "v9.9.9"}).status_code == 403
