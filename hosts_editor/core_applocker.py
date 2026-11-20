""""Mode anti-portable": AppLocker rules that only apply to the CHILD accounts.

  * everyone may run everything (parents unaffected, accounts created later too);
  * child accounts: every .exe is denied EXCEPT under %WINDIR% and %PROGRAMFILES% (where a standard
    account cannot write), the Microsoft publisher (OneDrive, Teams… installed per user) and the
    exceptions approved by the parent (publisher, or file hash for unsigned programs);
  * writable sub-folders of Windows (Temp, Tasks, spool…) are denied explicitly for the child.

Portable browsers, renamed executables, programs on a USB stick or in Downloads stop working for
the child, whatever their name. Rollout: "audit" first (nothing is blocked, AppLocker logs what
would have been — the console lists it so the parent approves what is legitimate), then "enforce".

Applied with Set-AppLockerPolicy (local policy); if Windows does not take it into account (some
Home editions), through the MDM WMI bridge of the AppLocker CSP. The Application Identity service
(AppIDSvc) is set to start automatically. The previous local policy and service setting are saved
and restored exactly when the mode is turned off.

Known limits: not enforced in safe mode; Microsoft-signed system tools (LOLBins) remain usable.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from xml.sax.saxutils import quoteattr

from .secure_storage import PROGRAMDATA_DIR, atomic_write_json

logger = logging.getLogger("HOTS.applocker")

STATE_FILE = os.path.join(PROGRAMDATA_DIR, "applocker_state.json")
MODES = ("off", "audit", "enforce")
RULE_PREFIX = "HOTS:"
MICROSOFT_PUBLISHER = "O=MICROSOFT CORPORATION, L=REDMOND, S=WASHINGTON, C=US"
_CREATE_NO_WINDOW = 0x08000000
_SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
POWERSHELL = os.path.join(_SYSTEM32, "WindowsPowerShell", "v1.0", "powershell.exe")
APPID_KEY = r"SYSTEM\CurrentControlSet\Services\AppIDSvc"

# Folders of Windows where a standard user can write: denied for the child despite %WINDIR%\*.
WRITABLE_WINDOWS_DIRS = (
    r"%WINDIR%\Temp\*", r"%WINDIR%\Tasks\*", r"%WINDIR%\tracing\*", r"%WINDIR%\Registration\CRMLog\*",
    r"%WINDIR%\debug\WIA\*", r"%WINDIR%\ServiceProfiles\LocalService\AppData\Local\Temp\*",
    r"%SYSTEM32%\Tasks\*", r"%SYSTEM32%\spool\drivers\color\*", r"%SYSTEM32%\spool\PRINTERS\*",
    r"%SYSTEM32%\spool\SERVERS\*", r"%SYSTEM32%\com\dmp\*", r"%SYSTEM32%\FxsTmp\*",
    r"%SYSTEM32%\Microsoft\Crypto\RSA\MachineKeys\*",
    r"%WINDIR%\SysWOW64\Tasks\*", r"%WINDIR%\SysWOW64\com\dmp\*", r"%WINDIR%\SysWOW64\FxsTmp\*",
)
_HASH_RE = re.compile(r"^(0x)?[0-9A-Fa-f]{64}$")
_SID_RE = re.compile(r"^S-1-(5-21|12-1)(-\d{1,10}){4,6}$")


# --- pure: policy XML (unit-tested) ------------------------------------------------------------------------

def _rid(seed: str) -> str:
    """Deterministic rule GUID (same policy = same XML, so re-applying is idempotent)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "hots-applocker:" + seed))


# Folders too broad to be allowed: allowing them would let the child run anything he copies there.
_TOO_BROAD = re.compile(
    r"^(?:%OSDRIVE%|%REMOVABLE%|%HOT%|[A-Z]:)(?:\\USERS(?:\\[^\\]+(?:\\(?:DOWNLOADS|DESKTOP|DOCUMENTS|MUSIC|PICTURES|"
    r"VIDEOS|FAVORITES|SAVED GAMES|ONEDRIVE[^\\]*|TÉLÉCHARGEMENTS|BUREAU|APPDATA(?:\\(?:LOCAL|ROAMING|LOCALLOW)"
    r"(?:\\TEMP|\\PROGRAMS|\\PACKAGES)?)?))?)?|\\PROGRAMDATA|\\TEMP|\\WINDOWS\\TEMP|\\USERS\\PUBLIC(?:\\[^\\]+)?)?$")
MAX_FOLDER_FILES = 3000  # MCNL alone has several hundred programs


def clean_folder(path) -> str:
    """'C:\\Users\\Lea\\AppData\\Local\\MCNL\\' -> 'C:\\USERS\\LEA\\APPDATA\\LOCAL\\MCNL' (upper case, no trailing
    backslash). ValueError if invalid or too broad."""
    folder = str(path or "").strip().strip('"').replace("/", "\\").rstrip("\\")
    if not folder or len(folder) > 300 or re.search(r"[<>\"\r\n*?|]", folder) or "\\..\\" in folder + "\\":
        raise ValueError("dossier invalide")
    if not re.match(r"^(?:%OSDRIVE%|%REMOVABLE%|%HOT%|%PROGRAMFILES%|%WINDIR%|%SYSTEM32%|[A-Za-z]:)(?:\\|$)", folder):
        raise ValueError("indiquez un chemin complet, ex. C:\\Users\\Lea\\AppData\\Local\\MonJeu")
    folder = folder.upper()
    if _TOO_BROAD.match(folder):
        raise ValueError("dossier trop large : l'enfant pourrait y copier n'importe quel programme. "
                         "Choisissez le dossier du logiciel lui-même.")
    return folder


def clean_exception(raw) -> dict:
    """Validate an exception coming from the console (publisher or hash)."""
    if not isinstance(raw, dict):
        raise ValueError("exception invalide")
    kind = raw.get("kind")
    label = str(raw.get("label") or "")[:80]
    if kind == "publisher":
        publisher = str(raw.get("publisher") or "").strip()
        product = str(raw.get("product") or "*").strip() or "*"
        if not re.fullmatch(r"(O|CN)=[^\r\n<>&\"]{1,300}", publisher) or len(product) > 200 or re.search(r"[<>&\"\r\n]", product):
            raise ValueError("éditeur invalide")
        return {"id": _rid(f"pub|{publisher}|{product}")[:8], "kind": kind, "label": label or product,
                "publisher": publisher, "product": product}
    if kind == "hash":
        digest = str(raw.get("hash") or "").strip()
        name = os.path.basename(str(raw.get("file_name") or "programme.exe"))[:120]
        length = raw.get("file_length") if isinstance(raw.get("file_length"), int) and raw["file_length"] >= 0 else 0
        if not _HASH_RE.match(digest) or re.search(r"[<>&\"\r\n]", name):
            raise ValueError("empreinte invalide")
        digest = "0x" + digest[-64:].upper()
        return {"id": _rid(f"hash|{digest}")[:8], "kind": kind, "label": label or name, "hash": digest,
                "file_name": name, "file_length": length}
    if kind == "file":
        # Resolved on the PC (publisher if signed, else hash) by AppLockerManager.add_exception: never kept
        # as a path rule (the child could put anything at that path).
        path = str(raw.get("path") or "").strip()
        if not path or len(path) > 400 or re.search(r"[<>\"\r\n*?]", path):
            raise ValueError("chemin invalide")
        return {"kind": "file", "path": path, "label": label or os.path.basename(path.replace("\\", "/"))}
    if kind == "folder":
        # Path rule: everything in the folder, including programs added later (less safe; broad folders refused).
        folder = clean_folder(raw.get("path"))
        return {"id": _rid(f"folder|{folder}")[:8], "kind": kind, "path": folder,
                "label": label or folder.rsplit("\\", 1)[-1] or folder}
    if kind == "folder_scan":
        # Resolved on the PC: one publisher / hash exception per program found now in the folder.
        folder = clean_folder(raw.get("path"))
        return {"kind": kind, "path": folder, "label": label or folder.rsplit("\\", 1)[-1] or folder}
    raise ValueError("type d'exception inconnu")


def expand_event_path(path: str) -> str:
    """AppLocker event path ('%OSDRIVE%\\USERS\\LEA\\X.EXE') -> real path, '' if not resolvable here
    (%REMOVABLE% / %HOT%: USB stick or CD whose drive letter is not in the event)."""
    variables = {"%OSDRIVE%": os.environ.get("SystemDrive", "C:"),
                 "%WINDIR%": os.environ.get("SystemRoot", r"C:\\Windows"),
                 "%SYSTEM32%": os.path.join(os.environ.get("SystemRoot", r"C:\\Windows"), "System32"),
                 "%PROGRAMFILES%": os.environ.get("ProgramFiles", r"C:\\Program Files")}
    upper = path.upper()
    for var, value in variables.items():
        if upper.startswith(var):
            return value + path[len(var):]
    if re.match(r"^[A-Za-z]:\\", path):
        return path
    return ""


_PS_FILE_INFO = (
    "$ErrorActionPreference='Stop'; $i = Get-AppLockerFileInformation -Path {path}; "
    "$o = [ordered]@{{ hash = [string]$i.Hash.HashDataString; file = [string]$i.Hash.SourceFileName; "
    "length = [int64]$i.Hash.SourceFileLength }}; "
    "if ($i.Publisher) {{ $o.publisher = [string]$i.Publisher.PublisherName; $o.product = [string]$i.Publisher.ProductName }}; "
    "ConvertTo-Json -InputObject $o -Compress")


_PS_FOLDER_INFO = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "$f = @(Get-ChildItem -LiteralPath {path} -Recurse -Force -File -Filter *.exe -Depth 6 | Select-Object -First {limit}); "
    "$out = @($f | ForEach-Object {{ $i = Get-AppLockerFileInformation -Path $_.FullName; if ($i) {{ "
    "$o = [ordered]@{{ hash = [string]$i.Hash.HashDataString; file = [string]$i.Hash.SourceFileName; "
    "length = [int64]$i.Hash.SourceFileLength; path = [string]$_.FullName }}; "
    "if ($i.Publisher) {{ $o.publisher = [string]$i.Publisher.PublisherName; $o.product = [string]$i.Publisher.ProductName }}; $o }} }}); "
    "ConvertTo-Json -InputObject $out -Depth 3 -Compress")


def exceptions_from_folder_info(items: list, folder_label: str) -> list:
    """Pure: one publisher exception per (publisher, product), one hash exception per unsigned program."""
    out, seen = [], set()
    for info in items if isinstance(items, list) else []:
        if not isinstance(info, dict):
            continue
        try:
            exc = exception_from_file_info(info, f"{folder_label} : {info.get('file') or '?'}")
        except ValueError:
            continue
        if isinstance(info.get("path"), str):
            exc["source"] = info["path"].upper()[:400]
        if exc["id"] not in seen:
            seen.add(exc["id"])
            if exc["kind"] == "publisher":
                exc["label"] = f"{folder_label} : {exc['product']}"
            out.append(exc)
    return out


def exception_from_file_info(info: dict, label: str) -> dict:
    """Pure: Get-AppLockerFileInformation result -> publisher exception if signed, else hash exception."""
    if info.get("publisher"):
        return clean_exception({"kind": "publisher", "publisher": info["publisher"], "product": info.get("product") or "*",
                                "label": label})
    return clean_exception({"kind": "hash", "hash": info.get("hash"), "file_name": info.get("file") or label,
                            "file_length": info.get("length") if isinstance(info.get("length"), int) else 0,
                            "label": label})


# 2: Appx collection added. Without it, once AppLocker is active Windows blocks every Store app
# (Microsoft Store, Photos, HEIF extension...) with "bloquée par votre administrateur système".
POLICY_VERSION = 3  # 3: revision checked, large policies through the MDM bridge from files


def build_policy_xml(child_sids: list, mode: str, exceptions: list) -> str:
    """AppLocker policy: Exe collection (children restricted) + Appx collection that allows every signed
    packaged app (Store apps) for everyone, i.e. Microsoft's default packaged-app rule."""
    if mode not in ("audit", "enforce"):
        raise ValueError("mode")
    sids = [s for s in child_sids if _SID_RE.match(s)]
    if not sids:
        raise ValueError("aucun compte enfant")
    enforcement = "AuditOnly" if mode == "audit" else "Enabled"
    exc = [r'<FilePathCondition Path="%WINDIR%\*" />', r'<FilePathCondition Path="%PROGRAMFILES%\*" />',
           f'<FilePublisherCondition PublisherName={quoteattr(MICROSOFT_PUBLISHER)} ProductName="*" BinaryName="*">'
           '<BinaryVersionRange LowSection="*" HighSection="*" /></FilePublisherCondition>']
    for e in exceptions:
        if e["kind"] == "publisher":
            exc.append(f'<FilePublisherCondition PublisherName={quoteattr(e["publisher"])} '
                       f'ProductName={quoteattr(e["product"])} BinaryName="*">'
                       '<BinaryVersionRange LowSection="*" HighSection="*" /></FilePublisherCondition>')
        elif e["kind"] == "folder":
            exc.append(f'<FilePathCondition Path={quoteattr(e["path"] + chr(92) + "*")} />')
        elif e["kind"] == "hash":
            exc.append(f'<FileHashCondition><FileHash Type="SHA256" Data={quoteattr(e["hash"])} '
                       f'SourceFileName={quoteattr(e["file_name"])} SourceFileLength="{int(e["file_length"])}" />'
                       '</FileHashCondition>')
    rules = [f'<FilePathRule Id="{_rid("allow-everyone")}" Name="{RULE_PREFIX} tous les comptes - tout autoriser" '
             'Description="" UserOrGroupSid="S-1-1-0" Action="Allow">'
             '<Conditions><FilePathCondition Path="*" /></Conditions></FilePathRule>']
    for sid in sids:
        rules.append(f'<FilePathRule Id="{_rid("deny-" + sid)}" Name="{RULE_PREFIX} compte enfant - hors Windows '
                     f'et Program Files" Description="" UserOrGroupSid="{sid}" Action="Deny">'
                     '<Conditions><FilePathCondition Path="*" /></Conditions>'
                     f'<Exceptions>{"".join(exc)}</Exceptions></FilePathRule>')
        for path in WRITABLE_WINDOWS_DIRS:
            rules.append(f'<FilePathRule Id="{_rid("deny-" + sid + path)}" Name="{RULE_PREFIX} compte enfant - '
                         f'dossier Windows inscriptible" Description="" UserOrGroupSid="{sid}" Action="Deny">'
                         f'<Conditions><FilePathCondition Path={quoteattr(path)} /></Conditions></FilePathRule>')
    appx = (f'<RuleCollection Type="Appx" EnforcementMode="{enforcement}">'
            f'<FilePublisherRule Id="{_rid("appx-all-signed")}" Name="{RULE_PREFIX} applications du Store signées" '
            'Description="" UserOrGroupSid="S-1-1-0" Action="Allow"><Conditions>'
            '<FilePublisherCondition PublisherName="*" ProductName="*" BinaryName="*">'
            '<BinaryVersionRange LowSection="0.0.0.0" HighSection="*" /></FilePublisherCondition>'
            '</Conditions></FilePublisherRule></RuleCollection>')
    return (f'<AppLockerPolicy Version="1"><RuleCollection Type="Exe" EnforcementMode="{enforcement}">'
            + "".join(rules) + "</RuleCollection>" + appx + "</AppLockerPolicy>")


def split_collections(policy_xml: str) -> dict:
    """{"Exe": "<RuleCollection ...>...</RuleCollection>", "Appx": ...} (for the AppLocker CSP)."""
    out = {}
    for m in re.finditer(r'<RuleCollection Type="(\w+)".*?</RuleCollection>', policy_xml, re.S):
        out[m.group(1)] = m.group(0)
    return out


def parse_fqbn(fqbn: str) -> dict:
    """'O=ROBLOX CORPORATION, L=…, C=US\\ROBLOX\\ROBLOXPLAYERBETA.EXE\\1.2.3' -> parts ('-' = unsigned).
    Publishers signed by a person are 'CN=JEAN DUPONT, ...'."""
    if not fqbn or fqbn.strip() in ("-", ""):
        return {}
    parts = fqbn.split("\\")
    if len(parts) < 2 or not parts[0].startswith(("O=", "CN=")):
        return {}
    return {"publisher": parts[0], "product": parts[1] if len(parts) > 1 else "*",
            "binary": parts[2] if len(parts) > 2 else ""}


def summarize_events(events: list, child_sids: list) -> list:
    """Group AppLocker events (8003 = would be blocked, 8004 = blocked) by program, children only,
    with a ready-made exception suggestion (publisher if signed, else hash)."""
    groups = {}
    for ev in events:
        if not isinstance(ev, dict) or ev.get("user") not in child_sids:
            continue
        path = str(ev.get("path") or "")
        key = path.lower() or str(ev.get("hash") or "")
        g = groups.get(key)
        if g is None:
            fq = parse_fqbn(str(ev.get("fqbn") or ""))
            name = os.path.basename(path.replace("\\", "/")) or "?"
            suggestion = None
            if fq:
                suggestion = {"kind": "publisher", "publisher": fq["publisher"], "product": fq["product"] or "*",
                              "label": f"{fq['product'] or name} ({fq['publisher'].split(',')[0][2:].title()})"}
            elif _HASH_RE.match(str(ev.get("hash") or "").strip()):
                suggestion = {"kind": "hash", "hash": str(ev["hash"]).strip(), "file_name": name,
                              "file_length": ev.get("size") or 0, "label": name}
            elif path:
                # Nothing usable in the event: the PC reads the file itself when "Autoriser" is clicked.
                suggestion = {"kind": "file", "path": path, "label": name}
            g = groups[key] = {"path": path, "name": name, "signed": bool(fq),
                               "publisher": fq.get("publisher", ""), "count": 0, "blocked": 0, "last": "",
                               "suggestion": suggestion}
        g["count"] += 1
        if ev.get("id") == 8004:
            g["blocked"] += 1
        g["last"] = max(g["last"], str(ev.get("time") or ""))
    return sorted(groups.values(), key=lambda g: g["last"], reverse=True)[:100]


# --- state ------------------------------------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    atomic_write_json(STATE_FILE, state)


# --- Windows plumbing ---------------------------------------------------------------------------------------------

def _ps(script: str, timeout: int = 120):
    try:
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                               "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + script],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                              creationflags=_CREATE_NO_WINDOW, timeout=timeout)
    except Exception as e:
        logger.warning("powershell: %s", e)
        return None


def _ps_ok(r) -> bool:
    return r is not None and r.returncode == 0 and (r.stdout or "").strip().splitlines()[-1:] == ["OK"]


def _ps_quote(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"


def _local_policy_xml() -> str:
    r = _ps("Get-AppLockerPolicy -Local -Xml")
    return (r.stdout or "").strip() if r is not None and r.returncode == 0 else ""


def _set_local_policy(xml: str) -> bool:
    fd, path = tempfile.mkstemp(prefix="hots_applocker_", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(xml)
        r = _ps(f"$ErrorActionPreference='Stop'; Set-AppLockerPolicy -XmlPolicy {_ps_quote(path)}; 'OK'")
        if not _ps_ok(r):
            logger.warning("Set-AppLockerPolicy failed: %s", ((r.stderr or r.stdout) if r else "").strip()[:300])
            return False
        _run([os.path.join(_SYSTEM32, "gpupdate.exe"), "/target:computer", "/force"], timeout=120)
        return True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


_MDM_CLASSES = {"Exe": ("MDM_AppLocker_ApplicationLaunchRestrictions01_EXE03", "EXE"),
                "Appx": ("MDM_AppLocker_ApplicationLaunchRestrictions01_StoreApps03", "StoreApps")}
_MDM_NS = r"root\cimv2\mdm\dmmap"
_MDM_PARENT = "./Vendor/MSFT/AppLocker/ApplicationLaunchRestrictions/HOTS"


def _set_mdm_policy(collections) -> bool:
    """Fallback for editions where the local AppLocker policy is not applied: AppLocker CSP.
    collections: {"Exe": xml, "Appx": xml} or None to remove ours."""
    script = "$ErrorActionPreference='Stop'; "
    for cls, _iid in _MDM_CLASSES.values():
        script += (f"Get-CimInstance -Namespace '{_MDM_NS}' -ClassName '{cls}' -Filter \"ParentID='{_MDM_PARENT}'\" "
                   f"-ErrorAction SilentlyContinue | Remove-CimInstance -ErrorAction SilentlyContinue; ")
    files = []
    r = None
    try:
        for kind, xml in (collections or {}).items():
            if kind not in _MDM_CLASSES:
                continue
            cls, iid = _MDM_CLASSES[kind]
            # Large policies (hundreds of programs allowed) do not fit on a command line: read from a file.
            fd, path = tempfile.mkstemp(prefix="hots_applocker_csp_", suffix=".xml")
            files.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(xml)
            script += (f"$x = Get-Content -Raw -Encoding UTF8 -LiteralPath {_ps_quote(path)}; "
                       f"$p = [System.Security.SecurityElement]::Escape($x); "
                       f"New-CimInstance -Namespace '{_MDM_NS}' -ClassName '{cls}' "
                       f"-Property @{{ParentID='{_MDM_PARENT}'; InstanceID='{iid}'; Policy=$p}} | Out-Null; ")
        script += "'OK'"
        r = _ps(script, timeout=300)
    finally:
        for path in files:
            try:
                os.remove(path)
            except OSError:
                pass
    if not _ps_ok(r):
        detail = ((r.stderr or r.stdout) if r else "").strip()
        logger.warning("AppLocker CSP (MDM bridge) failed: %s", detail[:300])
        _set_mdm_policy.last_error = detail[-300:]
        return False
    return True


_set_mdm_policy.last_error = ""


def policy_revision(xml: str) -> str:
    return hashlib.sha256(xml.encode("utf-8")).hexdigest()[:16]


def _effective(revision: str = "") -> bool:
    """Our policy is in force; with `revision`, this exact version of it (not an older one)."""
    r = _ps("Get-AppLockerPolicy -Effective -Xml")
    out = (r.stdout or "") if r is not None else ""
    return RULE_PREFIX in out and (not revision or f"rev:{revision}" in out)


def _run(args, timeout=60):
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace", check=False,
                              creationflags=_CREATE_NO_WINDOW, timeout=timeout)
    except Exception:
        return None


def _appid_start(value=None):
    """Read (value=None) or set the AppIDSvc start type (2 = automatic, 3 = manual)."""
    import winreg
    if value is None:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, APPID_KEY) as k:
                return winreg.QueryValueEx(k, "Start")[0]
        except OSError:
            return None
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, APPID_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Start", 0, winreg.REG_DWORD, int(value))
    return value


def collect_events(days: int = 7) -> list:
    script = (
        "$ErrorActionPreference='SilentlyContinue'\n"
        f"$ev = Get-WinEvent -FilterHashtable @{{LogName='Microsoft-Windows-AppLocker/EXE and DLL'; Id=8003,8004; "
        f"StartTime=(Get-Date).AddDays(-{int(days)})}} -MaxEvents 1000\n"
        "$out = @($ev | ForEach-Object { $x = [xml]$_.ToXml(); $d = $x.Event.UserData.RuleAndFileData;\n"
        "  [ordered]@{ id = $_.Id; time = $_.TimeCreated.ToUniversalTime().ToString('o'); user = $_.UserId.Value;\n"
        "    path = [string]$d.FilePath; hash = [string]$d.FileHash; fqbn = [string]$d.Fqbn } })\n"
        "ConvertTo-Json -InputObject $out -Depth 3 -Compress")
    r = _ps(script, timeout=120)
    try:
        data = json.loads((r.stdout or "").strip() or "[]") if r is not None else []
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


# --- manager -----------------------------------------------------------------------------------------------------

_PS_DIAGNOSE = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "$pol = Get-AppLockerPolicy -Effective; $x = [xml](Get-AppLockerPolicy -Effective -Xml); "
    "$exe = $x.AppLockerPolicy.RuleCollection | Where-Object {{ $_.Type -eq 'Exe' }}; "
    "$counts = [ordered]@{{ mode = [string]$exe.EnforcementMode; "
    "hash = @($x.SelectNodes(\"//RuleCollection[@Type='Exe']//FileHashCondition\")).Count; "
    "publisher = @($x.SelectNodes(\"//RuleCollection[@Type='Exe']//FilePublisherCondition\")).Count; "
    "path = @($x.SelectNodes(\"//RuleCollection[@Type='Exe']//FilePathCondition\")).Count }}; "
    "$users = @({users}); $paths = @({paths}); "
    "$tests = @(foreach ($p in $paths) {{ foreach ($u in $users) {{ "
    "if (-not (Test-Path -LiteralPath $p)) {{ [ordered]@{{ user = $u; path = $p; decision = 'Introuvable'; rule = '' }}; continue }}; "
    "$r = $pol | Test-AppLockerPolicy -Path $p -User $u; "
    "if (-not $r) {{ $n = (New-Object System.Security.Principal.SecurityIdentifier($u)).Translate([System.Security.Principal.NTAccount]).Value; "
    "$r = $pol | Test-AppLockerPolicy -Path $p -User $n }}; "
    "[ordered]@{{ user = $u; path = $p; decision = $(if ($r) {{ [string]$r.PolicyDecision }} else {{ 'Erreur' }}); rule = [string]$r.MatchingRule }} }} }}); "
    "$ev = @(Get-WinEvent -LogName 'Microsoft-Windows-AppLocker/EXE and DLL' -MaxEvents 8 | ForEach-Object {{ "
    "[ordered]@{{ time = $_.TimeCreated.ToString('o'); id = $_.Id; message = $_.Message }} }}); "
    "$mdm = @(Get-CimInstance -Namespace 'root\\cimv2\\mdm\\dmmap' -ClassName 'MDM_AppLocker_ApplicationLaunchRestrictions01_EXE03').Count; "
    "$svc = [string](Get-Service AppIDSvc).Status; "
    "ConvertTo-Json -Depth 4 -Compress -InputObject ([ordered]@{{ counts = $counts; tests = $tests; events = $ev; mdm = $mdm; service = $svc }})")


def diagnose(paths: list, child_sids: list) -> dict:
    """What Windows itself says: rules in force, Test-AppLockerPolicy for each program and child account,
    last AppLocker events. Read-only, for the console (no copy-paste on the PC needed)."""
    real = []
    for p in paths[:5]:
        p = str(p or "").strip()
        if not p or len(p) > 400 or re.search(r"[<>\"'\r\n*?|]", p):
            continue
        r = expand_event_path(p)
        if r:
            real.append(r)
    users = [s for s in child_sids if _SID_RE.match(s)][:4]
    r = _ps(_PS_DIAGNOSE.format(users=", ".join(_ps_quote(u) for u in users) or "''",
                                paths=", ".join(_ps_quote(p) for p in real) or ""), timeout=240)
    try:
        data = json.loads((r.stdout or "").strip()) if r is not None and r.returncode == 0 else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in ("tests", "events"):
        if isinstance(data.get(key), dict):
            data[key] = [data[key]]
        data.setdefault(key, [])
    state = _load_state()
    data["state"] = {"mode": state.get("mode"), "method": state.get("method"),
                     "exceptions": len(state.get("exceptions") or []), "last_apply": state.get("last_apply") or {}}
    if not data.get("counts"):
        data["error"] = ((r.stderr or "") if r is not None else "PowerShell indisponible").strip()[-300:]
    return data


class AppLockerManager:
    last_error: str = ""

    @staticmethod
    def current() -> dict:
        state = _load_state()
        return {"mode": state.get("mode") if state.get("mode") in MODES else "off",
                "exceptions": state.get("exceptions") or [], "method": state.get("method"),
                "since": state.get("since"), "children": state.get("children") or []}

    @staticmethod
    def _apply(mode: str, children: list, exceptions: list) -> bool:
        from .core_antispy import _is_admin
        AppLockerManager.last_error = ""
        if not _is_admin():
            AppLockerManager.last_error = "Droits administrateur requis."
            return False
        state = _load_state()
        if mode == "off":
            return AppLockerManager._restore(state)
        try:
            xml = build_policy_xml(children, mode, exceptions)
        except ValueError:
            AppLockerManager.last_error = "Déclarez d'abord le compte Windows de l'enfant (onglet Sécurité)."
            return False
        if "original_policy" not in state:
            state["original_policy"] = _local_policy_xml()
            state["original_appid_start"] = _appid_start()
        try:
            _appid_start(2)
        except OSError as e:
            logger.warning("AppIDSvc start type not changed: %s", e)
        _run([os.path.join(_SYSTEM32, "sc.exe"), "start", "AppIDSvc"])
        revision = policy_revision(xml)
        xml = xml.replace('Description="" UserOrGroupSid="S-1-1-0" Action="Allow"><Conditions><FilePathCondition',
                          f'Description="rev:{revision}" UserOrGroupSid="S-1-1-0" Action="Allow"><Conditions><FilePathCondition', 1)
        method = None
        if _set_local_policy(xml) and _effective(revision):
            method = "local"
            _set_mdm_policy(None)  # an older policy left in the MDM bridge would still block (most restrictive wins)
        elif _set_mdm_policy(split_collections(xml)):
            method = "mdm"
        state.update({"mode": mode, "children": children, "exceptions": exceptions, "method": method,
                      "policy_version": POLICY_VERSION,
                      "since": state.get("since") if state.get("mode") == mode else time.time()})
        error = "" if method else ("Windows n'a pas accepté la stratégie AppLocker sur ce PC"
                                   + (f" : {_set_mdm_policy.last_error}" if _set_mdm_policy.last_error else "."))
        state["last_apply"] = {"ok": bool(method), "error": error, "at": time.time(), "method": method,
                               "exceptions": len(exceptions), "revision": revision}
        _save_state(state)
        if method is None:
            AppLockerManager.last_error = error
            return False
        logger.info("applocker: %s via %s for %s", mode, method, children)
        return True

    @staticmethod
    def _restore(state: dict) -> bool:
        ok = True
        if state.get("method") == "mdm":
            ok = _set_mdm_policy(None) and ok
        original = state.get("original_policy") or '<AppLockerPolicy Version="1" />'
        if "original_policy" in state or state.get("method") == "local":
            ok = _set_local_policy(original) and ok
        if state.get("original_appid_start") is not None:
            try:
                _appid_start(state["original_appid_start"])
            except OSError:
                pass
        _save_state({"mode": "off", "exceptions": state.get("exceptions") or [], "events": state.get("events") or []})
        if not ok:
            AppLockerManager.last_error = "La stratégie d'origine n'a pas pu être entièrement restaurée."
        return ok

    @staticmethod
    def set_mode(mode: str) -> bool:
        from .compliance import child_sids
        if mode not in MODES:
            AppLockerManager.last_error = f"Mode inconnu : {mode}"
            return False
        cur = AppLockerManager.current()
        return AppLockerManager._apply(mode, child_sids(), cur["exceptions"])

    @staticmethod
    def add_exception(raw) -> bool:
        exc = clean_exception(raw)
        new = [exc]
        if exc["kind"] == "file":
            exc = AppLockerManager._resolve_file(exc)
            if exc is None:
                return False
            new = [exc]
        elif exc["kind"] == "folder_scan":
            new = AppLockerManager._resolve_folder(exc)
            if not new:
                return False
        ids = {e["id"] for e in new}
        cur = AppLockerManager.current()
        exceptions = [e for e in cur["exceptions"] if e["id"] not in ids] + new
        if cur["mode"] == "off":
            state = _load_state()
            state["exceptions"] = exceptions
            _save_state(state)
            return True
        return AppLockerManager._apply(cur["mode"], cur["children"] or [], exceptions)

    @staticmethod
    def _resolve_file(exc: dict):
        """Publisher (signed) or hash exception for a program blocked at `path`, read on this PC."""
        real = expand_event_path(exc["path"])
        if not real:
            AppLockerManager.last_error = ("Ce programme était sur une clé USB ou un CD : rebranchez-la, lancez le "
                                           "programme une fois (il sera bloqué), puis autorisez la nouvelle ligne.")
            return None
        if not os.path.isfile(real):
            AppLockerManager.last_error = f"Programme introuvable sur le PC : {real} (supprimé ou déplacé ?)"
            return None
        r = _ps(_PS_FILE_INFO.format(path=_ps_quote(real)))
        try:
            info = json.loads((r.stdout or "").strip().splitlines()[-1]) if r is not None and r.returncode == 0 else None
            resolved = exception_from_file_info(info or {}, exc["label"])
            resolved["source"] = exc["path"].upper()[:400]
            return resolved
        except (ValueError, IndexError, TypeError) as e:
            logger.warning("Get-AppLockerFileInformation %s: %s / %s", real, e, ((r.stderr or "") if r else "")[:300])
            AppLockerManager.last_error = "Windows n'a pas pu lire la signature ou l'empreinte de ce programme."
            return None

    @staticmethod
    def _resolve_folder(exc: dict) -> list:
        """Publisher / hash exceptions for the programs found now in the folder (and its subfolders)."""
        real = expand_event_path(exc["path"])
        if not real or not os.path.isdir(real):
            AppLockerManager.last_error = f"Dossier introuvable sur le PC : {real or exc['path']}"
            return []
        r = _ps(_PS_FOLDER_INFO.format(path=_ps_quote(real), limit=MAX_FOLDER_FILES + 1), timeout=1200)
        try:
            items = json.loads((r.stdout or "").strip() or "[]") if r is not None else []
        except ValueError:
            items = []
        items = items if isinstance(items, list) else [items]
        if len(items) > MAX_FOLDER_FILES:
            # Never allow part of a folder silently (the rest would stay blocked without explanation).
            AppLockerManager.last_error = (f"Plus de {MAX_FOLDER_FILES} programmes dans ce dossier : choisissez un "
                                           "sous-dossier, ou « Tout le dossier ».")
            return []
        new = exceptions_from_folder_info(items, exc["label"])
        if not new:
            AppLockerManager.last_error = "Aucun programme (.exe) trouvé dans ce dossier."
        return new

    @staticmethod
    def remove_exception(exc_id: str) -> bool:
        cur = AppLockerManager.current()
        exceptions = [e for e in cur["exceptions"] if e["id"] != exc_id]
        if cur["mode"] == "off":
            state = _load_state()
            state["exceptions"] = exceptions
            _save_state(state)
            return True
        return AppLockerManager._apply(cur["mode"], cur["children"] or [], exceptions)

    @staticmethod
    def refresh_events() -> list:
        """Read the AppLocker log (children only) and keep the summary for the console."""
        from .compliance import child_sids
        state = _load_state()
        summary = summarize_events(collect_events(), child_sids())
        state["events"] = summary
        state["events_at"] = time.time()
        _save_state(state)
        return summary

    @staticmethod
    def status() -> dict:
        state = _load_state()
        cur = AppLockerManager.current()
        cur["events"] = state.get("events") or []
        cur["events_at"] = state.get("events_at")
        cur["last_apply"] = state.get("last_apply") or {}
        return cur

    @staticmethod
    def repair() -> bool:
        """Children changed, or the policy disappeared: re-apply."""
        from .compliance import child_sids
        cur = AppLockerManager.current()
        if cur["mode"] == "off":
            return False
        children = child_sids()
        if not children:  # no child account left: nothing to restrict
            return AppLockerManager._restore(_load_state())
        lost = cur["method"] == "local" and not _effective()
        outdated = _load_state().get("policy_version") != POLICY_VERSION  # e.g. Store apps rule added
        if sorted(cur["children"]) != sorted(children) or lost or outdated:
            return AppLockerManager._apply(cur["mode"], children, cur["exceptions"])
        return False

    @staticmethod
    def is_effective() -> bool:
        cur = AppLockerManager.current()
        if cur["mode"] == "off":
            return True
        return cur["method"] == "mdm" or (cur["method"] == "local" and _effective())
