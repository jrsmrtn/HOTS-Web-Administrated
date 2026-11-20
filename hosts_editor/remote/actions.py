"""Whitelisted actions the remote console may ask the agent to perform, and the state snapshot.

Security model: the server can only trigger the actions registered below, each with
validated parameters. There is deliberately no "run a command" or "write any file" action.

Windows-only modules (winreg, ctypes.windll...) are imported lazily inside the actions so
the dispatcher itself can be imported and tested on any platform.
"""

import contextlib
import hashlib
import logging
import os
import re
import socket
import sys
import time
from datetime import datetime

log = logging.getLogger("HOTS.remote.actions")

PRIVACY_LEVELS = ("basic", "medium", "advanced", "extra")
RSTRUI_EXE = "rstrui.exe"
MAX_HOSTS_TEXT = 5 * 1024 * 1024
MAX_HOSTS_TEXT_IN_READ = 2 * 1024 * 1024
MAX_CUSTOM_DOMAINS = 5000

_DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
_EXE_RE = re.compile(r"^[\w .()+\-]{1,120}\.exe$", re.IGNORECASE | re.UNICODE)
_BACKUP_RE = re.compile(r"^hosts\.bak_\d{8}_\d{6}$")
_HOSTNAME_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-]{1,253}$")


class ActionError(Exception):
    """Expected failure; the message is shown as-is in the web console."""


_REGISTRY: dict = {}


def action(name: str, mutating: bool = True):
    def deco(fn):
        _REGISTRY[name] = (fn, mutating)
        return fn
    return deco


def list_actions() -> list:
    return sorted(_REGISTRY)


def is_mutating(name: str) -> bool:
    entry = _REGISTRY.get(name)
    return bool(entry and entry[1])


@contextlib.contextmanager
def language(lang: str):
    from ..i18n import set_lang, current_lang
    previous = current_lang()
    if lang:
        set_lang(lang)
    try:
        yield
    finally:
        set_lang(previous)


def execute(name: str, params, lang: str = "fr"):
    """Run one action. Returns (ok, result, error_message, mutating)."""
    entry = _REGISTRY.get(name)
    if entry is None:
        return False, None, f"Action inconnue : {name}", False
    fn, mutating = entry
    if not isinstance(params, dict):
        params = {}
    try:
        with language(lang):
            result = fn(params)
        return True, result, "", mutating
    except ActionError as e:
        return False, None, str(e), mutating
    except Exception as e:  # unexpected: keep the agent alive, report the error
        log.exception("action %s failed", name)
        return False, None, f"{type(e).__name__}: {e}", mutating


# --- parameter helpers ---------------------------------------------------------------------

def _bool(p: dict, key: str) -> bool:
    value = p.get(key)
    if isinstance(value, bool):
        return value
    raise ActionError(f"Paramètre « {key} » manquant ou invalide.")


def _str(p: dict, key: str, maxlen: int = 255, required: bool = True, default: str = "") -> str:
    value = p.get(key, None)
    if value is None or value == "":
        if required:
            raise ActionError(f"Paramètre « {key} » manquant.")
        return default
    if not isinstance(value, str) or len(value) > maxlen or "\x00" in value:
        raise ActionError(f"Paramètre « {key} » invalide.")
    return value


def _int(p: dict, key: str, lo: int, hi: int) -> int:
    value = p.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ActionError(f"Paramètre « {key} » invalide.")
    return value


def _one_line(text: str, key: str) -> str:
    if "\n" in text or "\r" in text:
        raise ActionError(f"« {key} » ne doit pas contenir de retour à la ligne.")
    return text.strip()


# --- hosts write helper ----------------------------------------------------------------------

@contextlib.contextmanager
def _hosts_writable():
    """Temporarily lift the hosts lock (if on) so a remote change does not fail, then restore it."""
    from ..core_antispy import HostsLockManager
    relock = HostsLockManager.is_active()
    if relock and not HostsLockManager.disable():
        raise ActionError("Impossible de déverrouiller temporairement le fichier hosts : "
                          f"{HostsLockManager.last_error or 'erreur inconnue'}")
    try:
        yield
    finally:
        if relock and not HostsLockManager.enable():
            log.error("could not re-lock the hosts file: %s", HostsLockManager.last_error)


def _wrap_hosts_errors(fn, *args, **kwargs):
    from ..core import HostsLimitExceeded, HostsBusyError, MAX_ACTIVE_ENTRIES
    from ..core_antispy import HostsLockError
    try:
        return fn(*args, **kwargs)
    except HostsLimitExceeded as e:
        raise ActionError(f"Trop d'entrées actives dans le fichier hosts ({e.would_be_count} "
                          f"> {MAX_ACTIVE_ENTRIES}).") from None
    except (HostsBusyError, HostsLockError) as e:
        raise ActionError(str(e)) from None


# --- parental control ------------------------------------------------------------------------

def _category(file: str):
    from ..parental_catalog import CATEGORIES, TELEMETRY_CATEGORY, CUSTOM_FILE
    from ..resource_utils import blocklists_dir
    from ..constants import custom_domains_path
    if file == CUSTOM_FILE:
        return {"file": CUSTOM_FILE, "label_key": "par_cat_custom"}, custom_domains_path()
    for cat in CATEGORIES + [TELEMETRY_CATEGORY]:
        if cat["file"] == file:
            return cat, os.path.join(blocklists_dir(), file)
    raise ActionError(f"Catégorie inconnue : {file}")


def _toggle_block(cat: dict, path: str, enabled: bool) -> None:
    from ..core import toggle_parental_control
    from ..parental_catalog import comment_for_category
    with _hosts_writable():
        ok = _wrap_hosts_errors(toggle_parental_control, enabled, path,
                                tag_suffix=cat["file"], comment=comment_for_category(cat))
    if not ok:
        raise ActionError("L'écriture du fichier hosts a échoué.")


def read_custom_domains() -> list:
    from ..constants import custom_domains_path
    path = custom_domains_path()
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line.split()[-1].lower())
    except OSError:
        pass
    return out


def normalize_domains(items) -> tuple:
    """Same rules as the desktop editor: accepts URLs / hosts lines, returns (valid, invalid)."""
    import ipaddress
    valid, invalid, seen = [], [], set()
    for raw in items:
        if not isinstance(raw, str):
            continue
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) > 1:
            try:
                ipaddress.ip_address(parts[0])
                parts = parts[1:]
            except ValueError:
                pass
        for item in parts:
            cand = re.sub(r"^https?://", "", item, flags=re.IGNORECASE)
            cand = cand.split("/")[0].split(":")[0].strip().lower()
            if len(cand) <= 253 and _DOMAIN_RE.match(cand) and not cand.rsplit(".", 1)[-1].isdigit():
                if cand not in seen:
                    seen.add(cand)
                    valid.append(cand)
            else:
                invalid.append(item)
    return valid, invalid


@action("parental.set")
def _parental_set(p):
    from ..parental_catalog import CUSTOM_FILE
    file = _str(p, "category", 64)
    enabled = _bool(p, "enabled")
    cat, path = _category(file)
    if enabled:
        if file == CUSTOM_FILE and not read_custom_domains():
            raise ActionError("La liste de domaines personnalisés est vide.")
        if not os.path.exists(path):
            raise ActionError(f"Liste de blocage introuvable sur le PC : {file}")
    _toggle_block(cat, path, enabled)
    if file == "youtube.txt":
        try:
            refresh_safesearch()  # YouTube blocked: never leave it "restricted" instead
        except ActionError:
            pass
    return {"category": file, "enabled": enabled}


@action("custom_domains.set")
def _custom_domains_set(p):
    """Replace the custom domain list; re-apply the block if it is on (or if apply=True)."""
    from ..constants import custom_domains_path
    from ..core import is_parental_active
    from ..parental_catalog import CUSTOM_FILE
    domains = p.get("domains")
    if not isinstance(domains, list) or len(domains) > MAX_CUSTOM_DOMAINS:
        raise ActionError("Liste de domaines invalide.")
    valid, invalid = normalize_domains(domains)
    if invalid:
        raise ActionError("Domaines invalides : " + ", ".join(invalid[:15]))
    apply_now = p.get("apply") is True
    path = custom_domains_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for domain in sorted(valid):
            f.write(domain + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

    active = is_parental_active(CUSTOM_FILE)
    cat = {"file": CUSTOM_FILE, "label_key": "par_cat_custom"}
    if valid and (active or apply_now):
        _toggle_block(cat, path, True)
        active = True
    elif not valid and active:
        _toggle_block(cat, path, False)
        active = False
    return {"count": len(valid), "active": active}


@action("cf_family.set")
def _cf_family_set(p):
    from ..dns_utils import enable_cf_family_dns, disable_cf_family_dns
    enabled = _bool(p, "enabled")
    if enabled:
        # The hosts "adult" list is kept on purpose: both filters stack (defence in depth).
        ok, failed = enable_cf_family_dns()
    else:
        _dns_lock_off()  # otherwise the agent would put the filter back within minutes
        ok, failed = disable_cf_family_dns()
    if not ok:
        raise ActionError("La modification des serveurs DNS a échoué"
                          + (f" ({', '.join(failed)})." if failed else "."))
    return {"enabled": enabled, "failed_interfaces": failed}


def _doh_hostlist(enabled: bool) -> None:
    from ..core_dnslock import DOH_LIST
    from ..parental_catalog import DOH_CATEGORY
    from ..resource_utils import blocklists_dir
    path = os.path.join(blocklists_dir(), DOH_LIST)
    if enabled and not os.path.exists(path):
        raise ActionError(f"Liste de blocage introuvable sur le PC : {DOH_LIST}")
    _toggle_block(DOH_CATEGORY, path, enabled)


def _dns_lock_off() -> None:
    from ..core_dnslock import DnsLockManager
    if DnsLockManager.is_active():
        if not DnsLockManager.disable():
            raise ActionError(DnsLockManager.last_error or "Impossible de déverrouiller le DNS.")
        _doh_hostlist(False)


@action("dns_lock.set")
def _dns_lock_set(p):
    from ..core_dnslock import DnsLockManager
    enabled = _bool(p, "enabled")
    if enabled:
        if not DnsLockManager.enable():
            raise ActionError(DnsLockManager.last_error or "Le verrouillage du DNS a échoué.")
        _doh_hostlist(True)
    else:
        _dns_lock_off()
    return {"enabled": enabled}


@action("browser_policies.set")
def _browser_policies_set(p):
    from ..core_browser_policy import BrowserPolicyManager, LEVELS
    level = _str(p, "level", 16)
    if level not in LEVELS:
        raise ActionError(f"Niveau inconnu : {level}")
    safesites = p.get("safesites") is True
    if not BrowserPolicyManager.apply(level, safesites):
        raise ActionError(BrowserPolicyManager.last_error or "L'application des stratégies a échoué.")
    return {"level": level, "safesites": safesites}


# --- compliance / accounts -------------------------------------------------------------------------

@action("compliance.check")
def _compliance_check(p):
    """Full check (several seconds). Marked as mutating so the fresh report is pushed with the state."""
    from ..compliance import build_report
    return build_report()


def _sid(p: dict) -> str:
    from ..compliance import SID_RE
    sid = _str(p, "sid", 120)
    if not SID_RE.match(sid):
        raise ActionError("Identifiant de compte invalide.")
    return sid


@action("accounts.set_child")
def _accounts_set_child(p):
    from ..compliance import set_child
    sid = _sid(p)
    return {"children": set_child(sid, _bool(p, "is_child"))}


@action("accounts.make_standard")
def _accounts_make_standard(p):
    from ..compliance import make_standard
    try:
        make_standard(_sid(p))
    except (ValueError, RuntimeError) as e:
        raise ActionError(str(e)) from None
    return {"done": True, "note": "Effectif à la prochaine ouverture de session de ce compte."}


@action("accounts.make_admin")
def _accounts_make_admin(p):
    from ..compliance import make_admin
    try:
        make_admin(_sid(p))
    except (ValueError, RuntimeError) as e:
        raise ActionError(str(e)) from None
    return {"done": True}


@action("protections.repair")
def _protections_repair(p):
    from ..compliance import repair_protections
    return {"repaired": repair_protections()}


@action("applocker.set")
def _applocker_set(p):
    from ..core_applocker import AppLockerManager, MODES
    mode = _str(p, "mode", 16)
    if mode not in MODES:
        raise ActionError(f"Mode inconnu : {mode}")
    if not AppLockerManager.set_mode(mode):
        raise ActionError(AppLockerManager.last_error or "La stratégie n'a pas pu être appliquée.")
    return AppLockerManager.status()


@action("applocker.allow")
def _applocker_allow(p):
    from ..core_applocker import AppLockerManager
    try:
        ok = AppLockerManager.add_exception(p.get("exception"))
    except ValueError as e:
        raise ActionError(f"Exception invalide : {e}") from None
    if not ok:
        raise ActionError(AppLockerManager.last_error or "L'exception n'a pas pu être ajoutée.")
    return AppLockerManager.status()


@action("applocker.remove")
def _applocker_remove(p):
    from ..core_applocker import AppLockerManager
    if not AppLockerManager.remove_exception(_str(p, "id", 16)):
        raise ActionError(AppLockerManager.last_error or "L'exception n'a pas pu être retirée.")
    return AppLockerManager.status()


@action("applocker.refresh")
def _applocker_refresh(p):
    from ..core_applocker import AppLockerManager
    AppLockerManager.refresh_events()
    return AppLockerManager.status()


@action("extensions.set")
def _extensions_set(p):
    from ..core_browser_policy import BrowserPolicyManager, clean_extensions
    mode = _str(p, "mode", 16)
    if mode not in ("proxy", "allowlist"):
        raise ActionError(f"Mode inconnu : {mode}")
    ext = clean_extensions({"mode": mode, "chromium": p.get("chromium"), "firefox": p.get("firefox")})
    if not BrowserPolicyManager.set_extensions(ext):
        raise ActionError(BrowserPolicyManager.last_error or "Les stratégies n'ont pas pu être appliquées.")
    return ext


@action("child_browsing.set")
def _child_browsing_set(p):
    from ..core_browser_policy import BrowserPolicyManager
    enabled = _bool(p, "private_off")
    BrowserPolicyManager.set_child_private_off(enabled)
    return {"private_off": enabled,
            "note": "Appliqué aux comptes enfants dès qu'ils sont connectés (sinon à leur prochaine connexion)."}


@action("network_hardening.set")
def _network_hardening_set(p):
    from ..core_hardening import NetworkHardeningManager
    opts = {k: _bool(p, k) for k in ("hide_settings", "machine_proxy", "block_protocols")}
    if not NetworkHardeningManager.apply(**opts):
        raise ActionError(NetworkHardeningManager.last_error or "Les réglages n'ont pas pu être appliqués.")
    return opts


@action("safesearch.set")
def _safesearch_set(p):
    from ..core_safesearch import SafeSearchManager, YOUTUBE_LEVELS
    youtube = p.get("youtube", "off")
    if youtube not in YOUTUBE_LEVELS:
        raise ActionError("Niveau YouTube inconnu.")
    opts = {"google": p.get("google") is True, "youtube": youtube, "bing": p.get("bing") is True}
    with _hosts_writable():
        ok = _wrap_hosts_errors(SafeSearchManager.apply, opts)
    if not ok:
        raise ActionError(SafeSearchManager.last_error or "L'écriture du fichier hosts a échoué.")
    return SafeSearchManager.current()


def refresh_safesearch() -> bool:
    """Periodic: re-resolve the SafeSearch IPs and follow the YouTube category (hosts unlocked briefly)."""
    from ..core_safesearch import SafeSearchManager
    cur = SafeSearchManager.current()
    if not (cur["google"] or cur["bing"] or cur["youtube"] != "off"):
        return False
    with _hosts_writable():
        return bool(_wrap_hosts_errors(SafeSearchManager.refresh))


# --- screen time and activity journal (children's accounts) -------------------------------------------

def _child_sid(p: dict) -> str:
    from ..compliance import child_sids
    sid = _sid(p)
    if sid not in child_sids():
        raise ActionError("Ce compte n'est pas marqué comme compte enfant (onglet Sécurité).")
    return sid


def _screentime_of(sid: str) -> dict:
    from . import monitor
    for item in monitor.get().screentime_status():
        if item["sid"] == sid:
            return item
    return {}


@action("screentime.set")
def _screentime_set(p):
    from ..core_screentime import set_policy
    sid = _child_sid(p)
    try:
        set_policy(sid, p.get("policy"))
    except ValueError as e:
        raise ActionError(f"Réglage refusé : {e}") from None
    return _screentime_of(sid)


@action("screentime.bonus")
def _screentime_bonus(p):
    from ..core_screentime import add_bonus
    sid = _child_sid(p)
    minutes = _int(p, "minutes", -240, 480)
    if minutes == 0:
        raise ActionError("Durée nulle.")
    add_bonus(sid, minutes)
    return _screentime_of(sid)


@action("screentime.pause")
def _screentime_pause(p):
    from ..core_screentime import set_pause
    sid = _child_sid(p)
    set_pause(sid, _int(p, "minutes", -1, 1440))
    return _screentime_of(sid)


@action("activity.set")
def _activity_set(p):
    from .. import core_activity
    enabled = _bool(p, "enabled")
    titles = p.get("titles")
    if titles is not None and not isinstance(titles, bool):
        raise ActionError("Paramètre « titles » invalide.")
    current = core_activity.load_settings()
    titles = current["titles"] if titles is None else titles
    if enabled:
        if not core_activity.enable(titles):
            log.warning("activity journal: DNS log or browser policy not fully applied")
    elif current["enabled"]:
        core_activity.disable()
    core_activity.set_titles(titles)
    from . import monitor
    return monitor.get().activity_status()


@action("activity.sync", mutating=False)
def _activity_sync(p):
    """Today's summaries right now (the console stores them like the 5-minute pushes)."""
    from . import monitor
    mon = monitor.get()
    return {"days": mon.today(), "status": mon.activity_status()}


@action("agent.set_options")
def _agent_set_options(p):
    from ..compliance import set_options
    changes = {k: p[k] for k in ("auto_repair", "auto_update") if k in p}
    if not changes or any(not isinstance(v, bool) for v in changes.values()):
        raise ActionError("Paramètres invalides.")
    return set_options(**changes)


@action("agent.update")
def _agent_update(p):
    """Install the given release of HOTS Hosts (downloaded from GitHub by the PC, checked with SHA-256)."""
    from . import selfupdate
    tag = _str(p, "tag", 20)
    try:
        return selfupdate.start_update(tag)
    except (ValueError, RuntimeError) as e:
        raise ActionError(str(e)) from None


# --- protections -----------------------------------------------------------------------------

@action("hosts_lock.set")
def _hosts_lock_set(p):
    from ..core_antispy import HostsLockManager
    enabled = _bool(p, "enabled")
    ok = HostsLockManager.enable() if enabled else HostsLockManager.disable()
    if not ok:
        raise ActionError(HostsLockManager.last_error or "Échec du verrouillage du fichier hosts.")
    return {"enabled": enabled}


@action("netblock.set")
def _netblock_set(p):
    from ..core_netblock import NetBlockManager
    enabled = _bool(p, "enabled")
    ok = NetBlockManager.enable() if enabled else NetBlockManager.disable()
    if not ok:
        raise ActionError(NetBlockManager.last_error or "Échec de la modification du pare-feu.")
    return {"enabled": enabled}


@action("doh.set")
def _doh_set(p):
    from ..core_doh import DohBlockManager, BROWSERS
    browser = _str(p, "browser", 32)
    enabled = _bool(p, "enabled")
    if browser not in {b["id"] for b in BROWSERS}:
        raise ActionError(f"Navigateur inconnu : {browser}")
    ok = DohBlockManager.enable(browser) if enabled else DohBlockManager.disable(browser)
    if not ok:
        raise ActionError(DohBlockManager.last_error or "Échec de la modification de la stratégie DoH.")
    return {"browser": browser, "enabled": enabled}


_APPBLOCK_ERRORS = {
    "protected_system_app": "Ce programme système ne peut pas être bloqué.",
    "self_block": "HOTS Hosts ne peut pas se bloquer lui-même.",
    "no_admin": "L'agent n'a pas les droits administrateur.",
    "not_found": "Programme introuvable dans la liste des blocages.",
    "invalid_path": "Chemin invalide.",
}


def _appblock_error(default: str) -> ActionError:
    from ..core_appblock import AppBlockManager
    code = AppBlockManager.last_error or ""
    return ActionError(_APPBLOCK_ERRORS.get(code, code or default))


@action("vpn.set")
def _vpn_set(p):
    from ..core_appblock import AppBlockManager
    enabled = _bool(p, "enabled")
    ok_count, failed = AppBlockManager.add_vpn_bundle() if enabled else AppBlockManager.remove_vpn_bundle()
    if failed:
        raise ActionError("Échec pour : " + ", ".join(failed))
    return {"enabled": enabled, "count": ok_count}


@action("rstrui_lock.set")
def _rstrui_set(p):
    from ..core_appblock import AppBlockManager
    from ..i18n import T
    enabled = _bool(p, "enabled")
    ok = (AppBlockManager.add_app(RSTRUI_EXE, T("priv_rstrui_lock_title"), category="custom")
          if enabled else AppBlockManager.remove_app(RSTRUI_EXE))
    if not ok:
        raise _appblock_error("Échec du blocage de la restauration système.")
    return {"enabled": enabled}


def _exe_name(p: dict) -> str:
    name = _one_line(_str(p, "exe_name", 124), "exe_name").lower()
    if not name.endswith(".exe"):
        name += ".exe"
    if not _EXE_RE.match(name) or "\\" in name or "/" in name:
        raise ActionError("Nom d'exécutable invalide (exemple : jeu.exe).")
    from . import AGENT_EXE_NAME
    if name == AGENT_EXE_NAME.lower():
        raise ActionError(_APPBLOCK_ERRORS["self_block"])
    return name


@action("apps.add")
def _apps_add(p):
    exe = _exe_name(p)
    from ..core_appblock import AppBlockManager
    display = _one_line(_str(p, "display_name", 120, required=False, default=exe), "display_name")
    path = _str(p, "path", 400, required=False)
    if path:
        path = os.path.normpath(_one_line(path, "path"))
        if not os.path.isabs(path) or os.path.basename(path).lower() != exe or not os.path.isfile(path):
            path = ""
    if not AppBlockManager.add_app(exe, display or exe, category="custom", file_path=path):
        raise _appblock_error("Échec du blocage du programme.")
    return {"exe_name": exe}


@action("apps.set_enabled")
def _apps_set_enabled(p):
    exe = _exe_name(p)
    from ..core_appblock import AppBlockManager
    enabled = _bool(p, "enabled")
    if not AppBlockManager.set_enabled(exe, enabled):
        raise _appblock_error("Échec de la modification du blocage.")
    return {"exe_name": exe, "enabled": enabled}


@action("apps.remove")
def _apps_remove(p):
    exe = _exe_name(p)
    from ..core_appblock import AppBlockManager
    if not AppBlockManager.remove_app(exe):
        raise _appblock_error("Échec du déblocage du programme.")
    return {"exe_name": exe}


@action("apps.list_installed", mutating=False)
def _apps_list_installed(p):
    from ..core_appblock import AppBlockManager
    return {"programs": [{"name": name, "path": path, "exe": os.path.basename(path)}
                         for name, path in AppBlockManager.list_installed_programs()]}


# --- privacy -----------------------------------------------------------------------------------

def _privacy_item(item_id: str) -> dict:
    from ..core_antispy import ITEMS
    for item in ITEMS:
        if item["id"] == item_id:
            if item["kind"] == "hkcu_reg":
                raise ActionError("Ce réglage dépend de la session de l'utilisateur : modifiez-le "
                                  "depuis l'application HOTS Hosts sur le PC.")
            return item
    raise ActionError(f"Réglage inconnu : {item_id}")


def _antispy_failure(default: str) -> ActionError:
    from ..core_antispy import AntiSpyManager
    return ActionError(AntiSpyManager.last_error or default)


@action("privacy.set_item")
def _privacy_set_item(p):
    from ..core_antispy import AntiSpyManager
    item = _privacy_item(_str(p, "id", 80))
    enabled = _bool(p, "enabled")
    ok = AntiSpyManager.enable_item(item["id"]) if enabled else AntiSpyManager.disable_item(item["id"])
    if not ok:
        raise _antispy_failure("Échec de la modification du réglage.")
    return {"id": item["id"], "enabled": enabled, "warnings": list(AntiSpyManager.last_warnings)}


@action("privacy.set_level")
def _privacy_set_level(p):
    from ..core_antispy import AntiSpyManager, ITEMS
    level = _str(p, "level", 16)
    enabled = _bool(p, "enabled")
    if level not in PRIVACY_LEVELS:
        raise ActionError(f"Niveau inconnu : {level}")
    items = [it for it in ITEMS if it["level"] == level and it["kind"] != "hkcu_reg"]
    AntiSpyManager.last_error = ""
    AntiSpyManager.last_warnings = []
    task_cache, svc_cache = AntiSpyManager._prefetch_level_caches(items)
    failed = []
    for it in items:
        if enabled:
            ok = AntiSpyManager.enable_item(it["id"], _reset=False,
                                            _task_state_cache=task_cache, _svc_info_cache=svc_cache)
        else:
            ok = AntiSpyManager.disable_item(it["id"], _reset=False,
                                             _svc_info_cache=svc_cache, _task_state_cache=task_cache)
        if not ok:
            failed.append(it["id"])
    if failed:
        raise ActionError(f"{len(failed)} réglage(s) en échec : {AntiSpyManager.last_error or ', '.join(failed)}")
    return {"level": level, "enabled": enabled, "warnings": list(AntiSpyManager.last_warnings)}


@action("privacy.check_drift", mutating=False)
def _privacy_check_drift(p):
    from ..core_antispy import AntiSpyManager, ITEMS
    user_ids = {it["id"] for it in ITEMS if it["kind"] == "hkcu_reg"}
    regressed, restored = AntiSpyManager.get_drifted_items()
    return {"regressed": [i for i in regressed if i not in user_ids],
            "restored": [i for i in restored if i not in user_ids]}


@action("system.restore_point")
def _restore_point(p):
    from ..core_restore import SystemRestoreManager
    from ..i18n import T
    status, details = SystemRestoreManager.create_restore_point(T("priv_restore_point_description"))
    if status == "created":
        return {"status": "created"}
    if status == "throttled":
        return {"status": "throttled",
                "message": "Windows limite la fréquence des points de restauration (un par 24 h)."}
    raise ActionError(details or "La création du point de restauration a échoué.")


# --- hosts file ---------------------------------------------------------------------------------

def _hosts_bytes() -> bytes:
    from ..constants import HOSTS_PATH
    try:
        with open(HOSTS_PATH, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def _hosts_lines() -> list:
    from ..core import _decode_hosts_bytes
    text = _decode_hosts_bytes(_hosts_bytes()).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _entries_from_lines(lines: list) -> list:
    from ..core import _parse_line
    return [_parse_line(line) for line in lines]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _save_entries(entries: list) -> None:
    from ..constants import HOSTS_PATH
    from ..core import save_hosts, MAX_ACTIVE_ENTRIES
    active = sum(1 for e in entries if e["enabled"] is True)
    if active > MAX_ACTIVE_ENTRIES:
        raise ActionError(f"Trop d'entrées actives ({active} > {MAX_ACTIVE_ENTRIES}).")
    with _hosts_writable():
        _wrap_hosts_errors(save_hosts, HOSTS_PATH, entries)


@action("hosts.read", mutating=False)
def _hosts_read(p):
    from ..constants import HOSTS_PATH
    from ..core_antispy import HostsLockManager
    raw = _hosts_bytes()
    lines = _hosts_lines()
    entries = _entries_from_lines(lines)
    out_entries = [
        {"line": i, "enabled": e["enabled"], "ip": e["ip"], "hostname": e["hostname"],
         "comment": e["comment"], "raw": e["raw"]}
        for i, e in enumerate(entries) if e["enabled"] is not None
    ]
    text = "\n".join(lines) + "\n" if lines else ""
    return {
        "path": HOSTS_PATH,
        "sha256": _sha(raw),
        "size": len(raw),
        "locked": HostsLockManager.is_active(),
        "entries": out_entries,
        "text": text if len(raw) <= MAX_HOSTS_TEXT_IN_READ else None,
    }


def _validate_entry_fields(p: dict) -> tuple:
    from ..core import is_valid_ip
    ip = _one_line(_str(p, "ip", 64), "ip")
    hostname = _one_line(_str(p, "hostname", 1000), "hostname")
    comment = _one_line(_str(p, "comment", 300, required=False), "comment")
    if not is_valid_ip(ip):
        raise ActionError(f"Adresse IP invalide : {ip}")
    names = hostname.split()
    if not names or not all(_HOSTNAME_TOKEN_RE.match(n) for n in names) or "#" in hostname:
        raise ActionError(f"Nom d'hôte invalide : {hostname}")
    return ip, " ".join(names), comment.replace("#", " ").strip()


def _check_line(entries: list, p: dict) -> int:
    line = _int(p, "line", 0, 10_000_000)
    expect = p.get("expect")
    if line >= len(entries) or not isinstance(expect, str) or entries[line]["raw"] != expect:
        raise ActionError("Le fichier hosts a été modifié entre-temps : rechargez la page et réessayez.")
    if entries[line]["enabled"] is None:
        raise ActionError("Cette ligne n'est pas une entrée modifiable.")
    return line


@action("hosts.add_entry")
def _hosts_add_entry(p):
    ip, hostname, comment = _validate_entry_fields(p)
    enabled = p.get("enabled", True) is not False
    entries = _entries_from_lines(_hosts_lines())
    entries.append({"enabled": enabled, "ip": ip, "hostname": hostname, "comment": comment, "raw": ""})
    _save_entries(entries)
    return {"line": len(entries) - 1}


@action("hosts.update_entry")
def _hosts_update_entry(p):
    entries = _entries_from_lines(_hosts_lines())
    line = _check_line(entries, p)
    e = dict(entries[line])
    if "ip" in p or "hostname" in p:
        e["ip"], e["hostname"], e["comment"] = _validate_entry_fields(
            {"ip": p.get("ip", e["ip"]), "hostname": p.get("hostname", e["hostname"]),
             "comment": p.get("comment", e["comment"])})
    if "enabled" in p:
        e["enabled"] = _bool(p, "enabled")
    e["raw"] = ""
    entries[line] = e
    _save_entries(entries)
    return {"line": line}


@action("hosts.delete_entry")
def _hosts_delete_entry(p):
    entries = _entries_from_lines(_hosts_lines())
    line = _check_line(entries, p)
    del entries[line]
    _save_entries(entries)
    return {"line": line}


@action("hosts.save_text")
def _hosts_save_text(p):
    text = p.get("text")
    expect = _str(p, "expect_sha256", 64)
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_HOSTS_TEXT or "\x00" in text:
        raise ActionError("Contenu invalide ou trop volumineux.")
    if _sha(_hosts_bytes()) != expect:
        raise ActionError("Le fichier hosts a été modifié entre-temps : rechargez la page et réessayez.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.endswith("\n"):
        text = text[:-1]
    _save_entries(_entries_from_lines(text.split("\n") if text else []))
    return {"saved": True}


@action("hosts.backups", mutating=False)
def _hosts_backups(p):
    from ..constants import HOSTS_PATH
    from ..core import list_backups
    out = []
    for path, dt in list_backups(HOSTS_PATH):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append({"name": path.name, "date": dt.isoformat(timespec="seconds"), "size": size})
    return {"backups": out}


@action("hosts.restore_backup")
def _hosts_restore_backup(p):
    from ..constants import HOSTS_PATH
    from ..core import list_backups, restore_from_backup
    name = _str(p, "name", 64)
    if not _BACKUP_RE.match(name):
        raise ActionError("Nom de sauvegarde invalide.")
    match = next((path for path, _dt in list_backups(HOSTS_PATH) if path.name == name), None)
    if match is None:
        raise ActionError("Sauvegarde introuvable.")
    with _hosts_writable():
        _wrap_hosts_errors(restore_from_backup, HOSTS_PATH, str(match))
    return {"restored": name}


@action("dns.flush")
def _dns_flush(p):
    from ..core import flush_dns_cache
    if not flush_dns_cache():
        raise ActionError("Le vidage du cache DNS a échoué.")
    return {"flushed": True}


@action("get_state", mutating=False)
def _get_state(p):
    return build_state()


@action("agent.ping", mutating=False)
def _ping(p):
    return {"pong": True, "time": time.time()}


# --- state snapshot ---------------------------------------------------------------------------

def windows_version() -> str:
    if sys.platform != "win32":
        import platform
        return platform.platform()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            def q(name, default=""):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return default
            product = str(q("ProductName", "Windows"))
            build = str(q("CurrentBuildNumber") or q("CurrentBuild"))
            display = str(q("DisplayVersion") or q("ReleaseId"))
            ubr = q("UBR", None)
        if build.isdigit() and int(build) >= 22000:
            product = product.replace("Windows 10", "Windows 11")
        text = product
        if display:
            text += f" {display}"
        if build:
            text += f" (build {build}{'.' + str(ubr) if ubr is not None else ''})"
        return text
    except Exception:
        import platform
        return platform.platform()


def machine_id() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        except Exception:
            pass
    try:
        with open("/etc/machine-id", "r", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


def _uptime_seconds():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return int(ctypes.windll.kernel32.GetTickCount64() // 1000)
        except Exception:
            return None
    return None


def system_info() -> dict:
    from ..version import APP_VERSION
    from . import AGENT_VERSION
    info = {
        "hostname": socket.gethostname(),
        "os": windows_version(),
        "app_version": APP_VERSION,
        "agent_version": AGENT_VERSION,
        "machine_id": machine_id(),
    }
    uptime = _uptime_seconds()
    if uptime is not None:
        info["last_boot"] = int((time.time() - uptime) // 60 * 60)  # minute precision: stable between polls
    return info


def _state_system():
    info = system_info()
    info.pop("machine_id", None)
    info["uptime_s"] = _uptime_seconds()
    try:
        from ..core_antispy import _is_admin
        info["is_admin"] = bool(_is_admin())
    except Exception:
        info["is_admin"] = None
    return info


def _state_parental():
    from ..core import get_parental_active_map
    from ..parental_catalog import CATEGORIES, TELEMETRY_CATEGORY, CUSTOM_FILE, comment_for_category
    from ..resource_utils import blocklists_dir
    files = [c["file"] for c in CATEGORIES] + [TELEMETRY_CATEGORY["file"], CUSTOM_FILE]
    active = get_parental_active_map(files)
    base = blocklists_dir()
    cats = [{"file": c["file"], "label": comment_for_category(c), "icon": c["icon"], "color": c["color"],
             "active": active.get(c["file"], False),
             "available": os.path.exists(os.path.join(base, c["file"]))}
            for c in CATEGORIES]
    return {
        "categories": cats,
        "telemetry": {"file": TELEMETRY_CATEGORY["file"], "label": comment_for_category(TELEMETRY_CATEGORY),
                      "active": active.get(TELEMETRY_CATEGORY["file"], False)},
        "custom": {"file": CUSTOM_FILE, "active": active.get(CUSTOM_FILE, False),
                   "domains": read_custom_domains()},
    }


def _state_dns():
    """Family DNS filter + DNS lock, per adapter (IPv4 and IPv6)."""
    from ..core_dnslock import DnsLockManager
    return DnsLockManager.status(include_rules=False)


def _state_cf():
    dns = _state_dns()
    details = " · ".join(
        f"{a['name']}: {'DHCP ' if a['dhcp'] else ''}{', '.join(a['v4'] + a['v6']) or '—'}" for a in dns["adapters"])
    return {"active": dns["any_cf"], "complete": dns["all_ok"], "details": details,
            "lock": {"active": dns["active"], "policy": dns["policy"], "adapters": dns["adapters"]}}


def _state_browser_policies():
    from ..core_browser_policy import BrowserPolicyManager
    return BrowserPolicyManager.status()


def _state_hardening():
    from ..core_hardening import NetworkHardeningManager
    return NetworkHardeningManager.status()


def _state_safesearch():
    from ..core_safesearch import SafeSearchManager
    return SafeSearchManager.current()


def _state_applocker():
    from ..core_applocker import AppLockerManager
    return AppLockerManager.status()


def _state_networks():
    from ..core_networks import status
    return status()


def _state_compliance():
    from ..compliance import cached_report, options
    report = cached_report()
    return {"report": report, "options": options()}


def _state_protections():
    from ..core_antispy import HostsLockManager
    from ..core_appblock import AppBlockManager
    from ..core_doh import DohBlockManager, BROWSERS
    from ..core_netblock import NetBlockManager
    return {
        "hosts_lock": HostsLockManager.is_active(),
        "netblock": NetBlockManager.is_active(),
        "vpn": AppBlockManager.is_vpn_bundle_blocked(),
        "rstrui": bool(AppBlockManager.is_ifeo_blocked(RSTRUI_EXE)),
        "doh": [{"id": b["id"], "name": b["name"],
                 "installed": DohBlockManager.is_browser_installed(b["id"]),
                 "blocked": DohBlockManager.is_blocked(b["id"])} for b in BROWSERS],
    }


def _state_apps():
    from ..core_appblock import AppBlockManager
    return [{"exe_name": a.exe_name, "display_name": a.display_name, "category": a.category,
             "enabled": a.enabled, "locked_path": a.locked_path, "added_at": a.added_at}
            for a in AppBlockManager.list_blocked() if a.exe_name != RSTRUI_EXE]


def _state_privacy():
    from ..core_antispy import AntiSpyManager, ITEMS
    from ..i18n import T
    levels = []
    for level in PRIVACY_LEVELS:
        status = AntiSpyManager.get_items_status(level)
        items = []
        for it in ITEMS:
            if it["level"] != level:
                continue
            user_scope = it["kind"] == "hkcu_reg"
            items.append({
                "id": it["id"], "label": T(it["label_key"]), "desc": T(it["desc_key"]),
                "kind": it["kind"], "user_scope": user_scope,
                "active": None if user_scope else bool(status.get(it["id"])),
                "missing": False if user_scope else AntiSpyManager.is_item_missing(it["id"]),
            })
        levels.append({"id": level, "label": T(f"par_antispy_{level}_btn"), "items": items})
    return {"levels": levels}


def _state_hosts():
    from ..constants import HOSTS_PATH
    from ..core import list_backups
    entries = _entries_from_lines(_hosts_lines())
    return {
        "path": HOSTS_PATH,
        "size": len(_hosts_bytes()),
        "lines": len(entries),
        "active_entries": sum(1 for e in entries if e["enabled"] is True),
        "disabled_entries": sum(1 for e in entries if e["enabled"] is False),
        "backups": len(list_backups(HOSTS_PATH)),
    }


def _state_screentime():
    from . import monitor
    return monitor.get().screentime_status()


def _state_update():
    from . import selfupdate
    from ..compliance import options
    return dict(selfupdate.status(), auto_update=options()["auto_update"])


def _state_activity():
    from . import monitor
    return monitor.get().activity_status()


_SECTIONS = (
    ("system", _state_system),
    ("screentime", _state_screentime),
    ("activity", _state_activity),
    ("update", _state_update),
    ("parental", _state_parental),
    ("cf_family", _state_cf),
    ("browser_policies", _state_browser_policies),
    ("networks", _state_networks),
    ("applocker", _state_applocker),
    ("hardening", _state_hardening),
    ("safesearch", _state_safesearch),
    ("compliance", _state_compliance),
    ("protections", _state_protections),
    ("apps", _state_apps),
    ("privacy", _state_privacy),
    ("hosts", _state_hosts),
)


def build_state() -> dict:
    """Snapshot of every setting shown in the console. One failing section never hides the others."""
    state = {"collected_at": time.time(), "collected_at_iso": datetime.now().isoformat(timespec="seconds"),
             "errors": {}}
    for name, fn in _SECTIONS:
        try:
            state[name] = fn()
        except Exception as e:
            log.exception("state section %s failed", name)
            state["errors"][name] = f"{type(e).__name__}: {e}"
    return state
