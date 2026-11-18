"""One-click update of the console from the Administration page.

  latest_release()        latest version published on GitHub (cached 1 h)
  install_mode()          how this console was installed: "vps" (deploy.sh), "shared" (install-mutualise.sh)
                          or "manual" (from source: no one-click update)
  request_update(tag)     start the update (shared: a detached process; vps: a request file picked up by
                          the root unit hots-console-update.path installed by deploy.sh)
  run_update(...)         `python -m hots_server self-update`: download the release zip, check it against
                          SHA256SUMS.txt, back up the database, run the installer of the new version, which
                          keeps the data and restarts the console. Progress in DATA_DIR/update-status.json.

Only the console's own GitHub repository is used (HOTS_UPDATE_REPO), and only release tags vX.Y.Z.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

from . import settings

REPO = os.environ.get("HOTS_UPDATE_REPO", "jrsmrtn/HOTS-Web-Administrated").strip()
TAG_RE = re.compile(r"^v(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
ZIP_NAME = "HOTS_Console_Server.zip"
VPS_DATA_DIR = "/var/lib/hots-console"
VPS_INSTALL_DIR = "/opt/hots-console"
VPS_PATH_UNIT = "/etc/systemd/system/hots-console-update.path"
STALE_RUNNING = 45 * 60
_cache = {"at": 0.0, "value": None}


def _paths():
    data = settings.DATA_DIR
    return {"status": os.path.join(data, "update-status.json"), "request": os.path.join(data, "update-request"),
            "log": os.path.join(data, "update.log"), "work": os.path.join(data, "update-work")}


def current_version() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


def parse(tag: str):
    m = TAG_RE.match((tag or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(latest: str, current: str) -> bool:
    a, b = parse(latest), parse(current)
    return bool(a) and (b is None or a > b)


def latest_release(force: bool = False, timeout: int = 10) -> dict:
    """{"tag", "url"} of the latest release. Uses the /releases/latest redirect (no API rate limit)."""
    now = time.time()
    if not force and _cache["value"] and now - _cache["at"] < 3600:
        return _cache["value"]
    req = urllib.request.Request(f"https://github.com/{REPO}/releases/latest", method="HEAD",
                                 headers={"User-Agent": "hots-console-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final = resp.geturl()
    tag = final.rstrip("/").rsplit("/", 1)[-1]
    if not parse(tag):
        raise RuntimeError("Aucune version publiée trouvée sur GitHub.")
    value = {"tag": tag, "url": f"https://github.com/{REPO}/releases/tag/{tag}"}
    _cache.update(at=now, value=value)
    return value


def install_mode() -> dict:
    """{"mode": "shared"|"vps"|"manual", "dir", "reason"}"""
    data = os.path.abspath(settings.DATA_DIR)
    parent = os.path.dirname(data)
    if os.path.isfile(os.path.join(parent, "hots")) and os.path.isfile(os.path.join(parent, "hots.env")):
        return {"mode": "shared", "dir": parent, "reason": ""}
    if data == VPS_DATA_DIR and os.path.isfile(os.path.join(VPS_INSTALL_DIR, "deploy.sh")):
        if os.path.isfile(VPS_PATH_UNIT):
            return {"mode": "vps", "dir": VPS_INSTALL_DIR, "reason": ""}
        return {"mode": "manual", "dir": VPS_INSTALL_DIR,
                "reason": "Pour activer la mise à jour en un clic, mettez à jour une dernière fois à la main : "
                          "sudo /opt/hots-console/deploy.sh puis « Mettre à jour »."}
    return {"mode": "manual", "dir": "", "reason": "Console lancée depuis les sources : mettez-la à jour avec git."}


def read_status() -> dict:
    try:
        with open(_paths()["status"], "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(**fields) -> dict:
    status = dict(read_status(), **fields, at=time.time())
    path = _paths()["status"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False)
    os.chmod(tmp, 0o644)  # the console (another user on a VPS) reads what root writes
    os.replace(tmp, path)
    return status


def is_running(status: dict = None) -> bool:
    status = read_status() if status is None else status
    return status.get("state") in ("queued", "running") and time.time() - float(status.get("at") or 0) < STALE_RUNNING


def request_update(tag: str, by: str = "") -> dict:
    if not parse(tag):
        raise ValueError("version invalide")
    mode = install_mode()
    if mode["mode"] == "manual":
        raise RuntimeError(mode["reason"])
    if is_running():
        raise RuntimeError("Une mise à jour est déjà en cours.")
    status = write_status(state="queued", step="Mise à jour demandée", target=tag, current=current_version(),
                          by=by, error="", started=time.time())
    paths = _paths()
    if mode["mode"] == "vps":
        with open(paths["request"], "w", encoding="utf-8") as f:
            json.dump({"tag": tag, "by": by, "at": time.time()}, f)
        return status
    log = open(paths["log"], "ab")
    try:
        subprocess.Popen([sys.executable, "-m", "hots_server", "self-update", "--mode", "shared", "--tag", tag],
                         cwd=mode["dir"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True, env=dict(os.environ))
    finally:
        log.close()
    return status


# --- the update itself (separate process) ------------------------------------------------------------

def _download(url: str, dest: str, timeout: int = 120) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "hots-console-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_sum(sums_text: str, name: str) -> str:
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    return ""


def safe_extract(zip_path: str, dest: str) -> str:
    """Extract the zip in dest (no path outside dest) and return the HOTS_Console_Server folder."""
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            target = os.path.realpath(os.path.join(root, member.filename))
            if target != root and not target.startswith(root + os.sep):
                raise RuntimeError(f"chemin refusé dans l'archive : {member.filename}")
        z.extractall(root)
    folder = os.path.join(root, "HOTS_Console_Server")
    if not os.path.isdir(os.path.join(folder, "hots_server")):
        raise RuntimeError("archive inattendue (dossier HOTS_Console_Server absent)")
    return folder


def run_update(mode: str, tag: str = "", request_file: str = "") -> int:
    paths = _paths()
    if request_file:
        try:
            with open(request_file, "r", encoding="utf-8") as f:
                tag = (json.load(f) or {}).get("tag", "")
        except (OSError, ValueError):
            tag = ""
        try:
            os.remove(request_file)
        except OSError:
            pass
    if not parse(tag):
        write_status(state="error", step="", error="Version demandée invalide.")
        return 1
    try:
        write_status(state="running", step=f"Téléchargement de la version {tag}", target=tag, error="")
        shutil.rmtree(paths["work"], ignore_errors=True)
        os.makedirs(paths["work"], mode=0o700)
        base = f"https://github.com/{REPO}/releases/download/{tag}/"
        zip_path = os.path.join(paths["work"], ZIP_NAME)
        _download(base + ZIP_NAME, zip_path)
        sums_path = os.path.join(paths["work"], "SHA256SUMS.txt")
        _download(base + "SHA256SUMS.txt", sums_path)
        with open(sums_path, "r", encoding="utf-8", errors="replace") as f:
            expected = expected_sum(f.read(), ZIP_NAME)
        if not expected or expected != _sha256(zip_path):
            raise RuntimeError("le fichier téléchargé ne correspond pas à son empreinte SHA-256 publiée.")
        write_status(step="Vérification et décompression")
        folder = safe_extract(zip_path, paths["work"])
        with open(os.path.join(folder, "VERSION"), "r", encoding="utf-8") as f:
            if f.read().strip() != tag:
                raise RuntimeError("le paquet téléchargé ne porte pas la version attendue.")
        log = open(paths["log"], "ab")
        try:
            if mode == "shared":
                install_dir = os.path.dirname(os.path.abspath(settings.DATA_DIR))
                write_status(step="Sauvegarde de la base de données")
                subprocess.run([sys.executable, "-m", "hots_server", "backup", os.path.join(install_dir, "backups"),
                                "--keep", "14"], stdout=log, stderr=subprocess.STDOUT, check=False, timeout=600)
                args = ["bash", os.path.join(folder, "install-mutualise.sh"), "--yes", "--dir", install_dir]
                ht = os.path.join(install_dir, "htaccess.path")
                if os.path.isfile(ht):
                    with open(ht, "r", encoding="utf-8") as f:
                        args += ["--webroot", os.path.dirname(f.read().strip())]
                else:
                    args.append("--no-htaccess")
            else:
                args = ["bash", os.path.join(folder, "deploy.sh"), "--update", "--yes"]
            write_status(step="Installation et redémarrage de la console")
            r = subprocess.run(args, cwd=folder, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                               check=False, timeout=1800)
        finally:
            log.close()
        if r.returncode != 0:
            raise RuntimeError(f"l'installation a échoué (code {r.returncode}) : voir le journal de mise à jour.")
        write_status(state="done", step="Mise à jour terminée", done=time.time(), version=tag)
        shutil.rmtree(paths["work"], ignore_errors=True)
        return 0
    except Exception as e:  # reported in the console
        write_status(state="error", error=str(e) or type(e).__name__)
        return 1


def log_tail(lines: int = 30) -> str:
    try:
        with open(_paths()["log"], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
