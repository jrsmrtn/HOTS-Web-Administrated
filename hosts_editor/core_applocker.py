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


def clean_exception(raw) -> dict:
    """Validate an exception coming from the console (publisher or hash)."""
    if not isinstance(raw, dict):
        raise ValueError("exception invalide")
    kind = raw.get("kind")
    label = str(raw.get("label") or "")[:80]
    if kind == "publisher":
        publisher = str(raw.get("publisher") or "").strip()
        product = str(raw.get("product") or "*").strip() or "*"
        if not re.fullmatch(r"O=[^\r\n<>&\"]{1,300}", publisher) or len(product) > 200 or re.search(r"[<>&\"\r\n]", product):
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
    raise ValueError("type d'exception inconnu")


def build_policy_xml(child_sids: list, mode: str, exceptions: list) -> str:
    """AppLocker policy (Exe collection only). Packaged apps (Store) are not restricted."""
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
    return (f'<AppLockerPolicy Version="1"><RuleCollection Type="Exe" EnforcementMode="{enforcement}">'
            + "".join(rules) + "</RuleCollection></AppLockerPolicy>")


def parse_fqbn(fqbn: str) -> dict:
    """'O=ROBLOX CORPORATION, L=…, C=US\\ROBLOX\\ROBLOXPLAYERBETA.EXE\\1.2.3' -> parts ('-' = unsigned)."""
    if not fqbn or fqbn.strip() in ("-", ""):
        return {}
    parts = fqbn.split("\\")
    if len(parts) < 2 or not parts[0].startswith("O="):
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
            elif ev.get("hash"):
                suggestion = {"kind": "hash", "hash": str(ev["hash"]), "file_name": name,
                              "file_length": ev.get("size") or 0, "label": name}
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


_MDM_CLASS = "MDM_AppLocker_ApplicationLaunchRestrictions01_EXE03"
_MDM_NS = r"root\cimv2\mdm\dmmap"
_MDM_PARENT = "./Vendor/MSFT/AppLocker/ApplicationLaunchRestrictions/HOTS"


def _set_mdm_policy(rule_collection_xml) -> bool:
    """Fallback for editions where the local AppLocker policy is not applied: AppLocker CSP."""
    script = (f"$ErrorActionPreference='Stop'; "
              f"Get-CimInstance -Namespace '{_MDM_NS}' -ClassName '{_MDM_CLASS}' -Filter \"ParentID='{_MDM_PARENT}'\" "
              f"-ErrorAction SilentlyContinue | Remove-CimInstance -ErrorAction SilentlyContinue; ")
    if rule_collection_xml:
        escaped = rule_collection_xml.replace("'", "''")
        script += (f"$p = [System.Security.SecurityElement]::Escape('{escaped}'); "
                   f"New-CimInstance -Namespace '{_MDM_NS}' -ClassName '{_MDM_CLASS}' "
                   f"-Property @{{ParentID='{_MDM_PARENT}'; InstanceID='EXE'; Policy=$p}} | Out-Null; ")
    script += "'OK'"
    r = _ps(script)
    if not _ps_ok(r):
        logger.warning("AppLocker CSP (MDM bridge) failed: %s", ((r.stderr or r.stdout) if r else "").strip()[:300])
        return False
    return True


def _effective() -> bool:
    r = _ps("Get-AppLockerPolicy -Effective -Xml")
    return r is not None and RULE_PREFIX in (r.stdout or "")


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
        method = "local" if _set_local_policy(xml) and _effective() else None
        if method is None:
            collection = xml[len('<AppLockerPolicy Version="1">'):-len("</AppLockerPolicy>")]
            method = "mdm" if _set_mdm_policy(collection) else None
        state.update({"mode": mode, "children": children, "exceptions": exceptions, "method": method,
                      "since": state.get("since") if state.get("mode") == mode else time.time()})
        _save_state(state)
        if method is None:
            AppLockerManager.last_error = ("Windows n'a pas accepté la stratégie AppLocker sur ce PC "
                                           "(édition non prise en charge ?).")
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
        cur = AppLockerManager.current()
        exceptions = [e for e in cur["exceptions"] if e["id"] != exc["id"]] + [exc]
        if cur["mode"] == "off":
            state = _load_state()
            state["exceptions"] = exceptions
            _save_state(state)
            return True
        return AppLockerManager._apply(cur["mode"], cur["children"] or [], exceptions)

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
        if sorted(cur["children"]) != sorted(children) or lost:
            return AppLockerManager._apply(cur["mode"], children, cur["exceptions"])
        return False

    @staticmethod
    def is_effective() -> bool:
        cur = AppLockerManager.current()
        if cur["mode"] == "off":
            return True
        return cur["method"] == "mdm" or (cur["method"] == "local" and _effective())
