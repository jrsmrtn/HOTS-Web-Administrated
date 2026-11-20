"""Update of HOTS Hosts on this PC by the agent (SYSTEM): from the web console ("Mettre à jour ce PC",
"Mettre à jour tous les PC") or automatically (option "auto_update", checked every 6 hours).

Only the installer published in this fork's GitHub releases is used: the console only gives a version
number, never a download address. The file is checked against SHA256SUMS.txt of the same release, saved in
ProgramData\\HOTS Hosts\\update (SYSTEM and administrators only), then run silently by a one-shot scheduled
task (it must outlive the agent, which the installer stops and restarts).
"""

import hashlib
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from ..secure_storage import PROGRAMDATA_DIR, atomic_write_json, ensure_dir
from ..version import APP_VERSION, GITHUB_REPO

log = logging.getLogger("HOTS.remote.update")

INSTALLER = "HOTS_Hosts_setup.exe"
UPDATE_DIR = os.path.join(PROGRAMDATA_DIR, "update")
STATE_FILE = os.path.join(PROGRAMDATA_DIR, "update_state.json")
TASK = "HOTS Hosts Update"
TAG_RE = re.compile(r"^v(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
AUTO_EVERY = 6 * 3600
RETRY_SAME_TAG = 24 * 3600
MAX_SIZE = 300 * 1024 * 1024
_CREATE_NO_WINDOW = 0x08000000
_SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
SCHTASKS = os.path.join(_SYSTEM32, "schtasks.exe")
_lock = threading.Lock()


def parse(tag: str):
    m = TAG_RE.match((tag or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def installed() -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", APP_VERSION)[:3]) or (0,)


def is_newer(tag: str) -> bool:
    v = parse(tag)
    return bool(v) and v > installed()


def expected_sum(sums_text: str, name: str) -> str:
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    return ""


def _ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _open(url: str, method: str = "GET", timeout: int = 30):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "HOTS-Agent-Update"})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl())


def latest_tag() -> str:
    with _open(f"https://github.com/{GITHUB_REPO}/releases/latest", "HEAD", 20) as resp:
        tag = resp.geturl().rstrip("/").rsplit("/", 1)[-1]
    if not parse(tag):
        raise RuntimeError("aucune version publiée trouvée")
    return tag


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(**fields) -> dict:
    state = dict(load_state(), **fields)
    atomic_write_json(STATE_FILE, state)
    return state


def download(tag: str) -> str:
    """Download and check the installer of `tag`; returns its path (SYSTEM-only folder)."""
    ensure_dir(PROGRAMDATA_DIR)
    os.makedirs(UPDATE_DIR, exist_ok=True)
    base = f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/"
    with _open(base + "SHA256SUMS.txt") as resp:
        expected = expected_sum(resp.read(200_000).decode("utf-8", "replace"), INSTALLER)
    if not expected:
        raise RuntimeError("empreinte de l'installateur absente de SHA256SUMS.txt")
    fd, tmp = tempfile.mkstemp(dir=UPDATE_DIR, prefix=".download_", suffix=".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out, _open(base + INSTALLER, timeout=60) as resp:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SIZE:
                    raise RuntimeError("fichier trop gros")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError("le fichier téléchargé ne correspond pas à son empreinte SHA-256 publiée")
        final = os.path.join(UPDATE_DIR, f"HOTS_Hosts_setup_{tag}.exe")
        os.replace(tmp, final)
        tmp = None
        return final
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _run(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, errors="replace", check=False, timeout=timeout,
                          creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0)


def _launch(installer: str, delay: float) -> None:
    """Run the installer silently as SYSTEM through a one-shot scheduled task, after `delay` seconds
    (time for the agent to send its answer to the console)."""
    time.sleep(delay)
    log_path = os.path.join(UPDATE_DIR, "setup.log")
    command = f'"{installer}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /LOG="{log_path}"'
    r = _run([SCHTASKS, "/Create", "/TN", TASK, "/SC", "ONCE", "/ST", "00:00", "/SD", "01/01/2030",
              "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", command, "/F"])
    if r.returncode != 0:
        _save_state(state="error", error=f"tâche de mise à jour non créée : {(r.stderr or r.stdout).strip()[:200]}")
        return
    r = _run([SCHTASKS, "/Run", "/TN", TASK])
    if r.returncode != 0:
        _save_state(state="error", error=f"installation non lancée : {(r.stderr or r.stdout).strip()[:200]}")
    else:
        log.info("installer %s started", installer)


def start_update(tag: str, by: str = "console", delay: float = 5.0) -> dict:
    """Download, check and launch the update to `tag` (in the background after `delay` s)."""
    if not parse(tag):
        raise ValueError("version invalide")
    if not is_newer(tag):
        raise ValueError(f"HOTS Hosts est déjà en version {APP_VERSION} sur ce PC.")
    with _lock:
        state = load_state()
        if state.get("state") == "installing" and time.time() - float(state.get("at") or 0) < 1800:
            raise RuntimeError("Une mise à jour est déjà en cours sur ce PC.")
        _save_state(state="downloading", target=tag, by=by, at=time.time(), error="")
    try:
        installer = download(tag)
    except Exception as e:
        _save_state(state="error", error=str(e) or type(e).__name__, at=time.time())
        raise RuntimeError(f"Téléchargement impossible : {e}") from None
    state = _save_state(state="installing", at=time.time())
    threading.Thread(target=_launch, args=(installer, delay), daemon=True, name="hots-update").start()
    return {"state": state["state"], "target": tag, "from": APP_VERSION}


def cleanup_after_start() -> None:
    """Agent start: remove the one-shot task and the old installers, record the result."""
    state = load_state()
    if state.get("state") == "installing":
        target = parse(state.get("target") or "")
        ok = target is not None and installed() >= target
        _save_state(state="done" if ok else "error", at=time.time(),
                    error="" if ok else "l'installation ne s'est pas terminée (voir update\\setup.log)")
    if sys.platform == "win32":
        _run([SCHTASKS, "/Delete", "/TN", TASK, "/F"])
    try:
        for name in os.listdir(UPDATE_DIR):
            if name.lower().endswith((".exe", ".part")):
                os.remove(os.path.join(UPDATE_DIR, name))
    except OSError:
        pass


def auto_check(enabled: bool) -> bool:
    """Called every 30 min by the agent: at most every 6 h, install a newer release if enabled.
    The same version is not retried for 24 h after a failure. Returns True if an update was started."""
    if not enabled:
        return False
    state = load_state()
    now = time.time()
    if now - float(state.get("checked_at") or 0) < AUTO_EVERY:
        return False
    _save_state(checked_at=now)
    try:
        tag = latest_tag()
    except Exception as e:
        log.info("update check failed: %s", e)
        return False
    if not is_newer(tag):
        return False
    if state.get("target") == tag and state.get("state") == "error" and now - float(state.get("at") or 0) < RETRY_SAME_TAG:
        return False
    try:
        start_update(tag, by="auto", delay=2.0)
        log.info("automatic update to %s started", tag)
        return True
    except Exception as e:
        log.warning("automatic update to %s failed: %s", tag, e)
        return False


def status() -> dict:
    state = load_state()
    return {"version": APP_VERSION, "state": state.get("state") or "", "target": state.get("target") or "",
            "error": state.get("error") or "", "at": state.get("at"), "checked_at": state.get("checked_at")}
