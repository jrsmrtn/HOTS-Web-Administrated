"""Entry points of the remote agent (called by hosts_editor_launcher.pyw and the installer).

  --agent-run                             run the agent (scheduled task, as SYSTEM, at boot)
  --agent-enroll URL CODE [RESULT_FILE]   connect this PC to a console, register + start the task
  --agent-install-task                    re-register + start the task if this PC is connected
  --agent-stop                            stop the running agent (before files are replaced)
  --agent-uninstall                       stop, unregister, notify the server, forget credentials
  --agent-applocker-off                   emergency: lift the anti-portable mode (admin prompt)
  --agent-user-helper                     started by the agent in a child's session (activity journal)

The agent runs as a scheduled task rather than a Windows service: no extra dependency,
restart-on-failure handled by the Task Scheduler, and a standard (child) account cannot stop it.
"""

import logging
import logging.handlers
import os
import subprocess
import sys
import tempfile
import time
from xml.sax.saxutils import escape

from . import AGENT_EXE_NAME, TASK_NAME, config

log = logging.getLogger("HOTS.remote")

_IS_COMPILED = "__compiled__" in globals()
_CREATE_NO_WINDOW = 0x08000000
_SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
SCHTASKS = os.path.join(_SYSTEM32, "schtasks.exe")
TASKKILL = os.path.join(_SYSTEM32, "taskkill.exe")
_MUTEX_NAME = "Global\\HOTS_RemoteAgent_SingleInstance"
_mutex = None

EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_NETWORK, EXIT_NOT_ADMIN = 0, 1, 2, 3, 4


def setup_logging(to_file: bool = True) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_hots_agent", False) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    handler = None
    if to_file:
        try:
            os.makedirs(config.data_dir(), exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                config.log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        except Exception:
            handler = None
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(fmt)
    handler._hots_agent = True
    root.addHandler(handler)


def _is_admin() -> bool:
    if sys.platform != "win32":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _acquire_mutex() -> bool:
    """Single agent instance. The mutex only admits SYSTEM/admins, and a mutex pre-created by a
    standard (child) account to keep the agent from starting is ignored (owner check)."""
    global _mutex
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.LocalFree.restype = ctypes.c_void_p
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        adv.GetSecurityInfo.restype = ctypes.c_ulong
        adv.GetSecurityInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p),
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]

        class _SA(ctypes.Structure):
            _fields_ = [("nLength", ctypes.c_ulong), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", ctypes.c_int)]

        sd = ctypes.c_void_p()
        sa = None
        if adv.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:(A;;GA;;;SY)(A;;GA;;;BA)", 1, ctypes.byref(sd), None):
            sa = _SA(ctypes.sizeof(_SA), sd, 0)
        handle = k32.CreateMutexW(ctypes.byref(sa) if sa else None, False, _MUTEX_NAME)
        error = ctypes.get_last_error()
        if sd:
            k32.LocalFree(sd)
        if not handle:
            return True
        if error != 183:  # not ERROR_ALREADY_EXISTS: we own it
            _mutex = handle
            return True
        owner, desc, sid_text = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_wchar_p()
        trusted = False
        if adv.GetSecurityInfo(handle, 6, 1, ctypes.byref(owner), None, None, None, ctypes.byref(desc)) == 0:
            if owner and adv.ConvertSidToStringSidW(owner, ctypes.byref(sid_text)):
                trusted = sid_text.value in ("S-1-5-18", "S-1-5-32-544")
                k32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
            k32.LocalFree(desc)
        k32.CloseHandle(handle)
        if not trusted:
            log.warning("agent mutex held by an untrusted account: ignored")
        return not trusted
    except Exception:
        return True


def _run(args: list, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, errors="replace", check=False,
                          creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                          timeout=timeout)


def _write_result(path: str, message: str) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(message)
    except Exception:
        pass


# --- scheduled task ---------------------------------------------------------------------------

def agent_command(launcher_script: str = "") -> tuple:
    """(program, arguments) the scheduled task must run."""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if _IS_COMPILED or getattr(sys, "frozen", False):
        agent_exe = os.path.join(exe_dir, AGENT_EXE_NAME)
        program = agent_exe if os.path.isfile(agent_exe) else os.path.abspath(sys.executable)
        return program, "--agent-run"
    script = launcher_script or os.path.abspath(sys.argv[0])
    return os.path.abspath(sys.executable), f'"{script}" --agent-run'


def task_xml(program: str, arguments: str) -> str:
    workdir = os.path.dirname(program)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>HOTS Hosts - agent d'administration a distance (console web)</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled><Delay>PT20S</Delay></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_task(launcher_script: str = "") -> bool:
    program, arguments = agent_command(launcher_script)
    fd, xml_path = tempfile.mkstemp(prefix="hots_agent_task_", suffix=".xml")
    os.close(fd)
    try:
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(task_xml(program, arguments))
        r = _run([SCHTASKS, "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
        if r.returncode != 0:
            log.error("schtasks /Create failed: %s", (r.stderr or r.stdout).strip())
            return False
        return True
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass


def start_task() -> bool:
    r = _run([SCHTASKS, "/Run", "/TN", TASK_NAME])
    return r.returncode == 0


def stop_agent() -> None:
    _run([SCHTASKS, "/End", "/TN", TASK_NAME])
    _run([TASKKILL, "/F", "/T", "/IM", AGENT_EXE_NAME])


def remove_task() -> None:
    _run([SCHTASKS, "/Delete", "/TN", TASK_NAME, "/F"])


def task_exists() -> bool:
    return _run([SCHTASKS, "/Query", "/TN", TASK_NAME]).returncode == 0


# --- agent runtime ----------------------------------------------------------------------------

def _state_builder(lang: str) -> dict:
    from . import actions
    with actions.language(lang):
        return actions.build_state()


COMPLIANCE_EVERY = 3 * 3600
_last_compliance = 0.0


def periodic_checks(agent) -> bool:
    """Every 30 min: keep the console reachable through the Internet block, auto-repair undone
    protections (if enabled from the console), refresh the compliance report every 3 h."""
    global _last_compliance
    changed = False
    ips = agent.transport.resolve_server_ips()
    if ips and config.update_server_ips(ips):
        log.info("console server IPs changed: %s", ", ".join(ips))
    from ..core_netblock import NetBlockManager
    if NetBlockManager.exclusions_outdated():
        log.info("re-applying the Internet block with the current server exclusions")
        NetBlockManager.enable()
        changed = True
    from .. import compliance
    if compliance.options()["auto_repair"]:
        repaired = compliance.repair_protections()
        if repaired:
            config.write_status(last_repair={"at": time.time(), "items": repaired})
            changed = True
    else:
        drift = NetBlockManager.check_drift()
        if drift:
            log.info("Internet block drift: %s", drift)
    try:
        from .actions import refresh_safesearch
        if refresh_safesearch():
            changed = True
    except Exception:
        log.exception("SafeSearch refresh failed")
    from ..core_applocker import AppLockerManager
    if AppLockerManager.current()["mode"] != "off":
        try:
            AppLockerManager.refresh_events()
            changed = True
        except Exception:
            log.exception("AppLocker events could not be read")
    if time.monotonic() - _last_compliance >= COMPLIANCE_EVERY or not compliance.cached_report():
        _last_compliance = time.monotonic()
        compliance.build_report()
        changed = True
    return changed


_last_dns_reapply = 0.0
DNS_REAPPLY_EVERY = 120


def fast_checks(agent) -> bool:
    """Every 30 s: trusted networks and captive portals (core_networks), proxy of the current
    network; every 2 min: the DNS lock covers adapters that just appeared (USB tethering...)."""
    global _last_dns_reapply
    changed = False
    from .. import core_networks
    from ..core_browser_policy import BrowserPolicyManager
    from ..core_dnslock import DnsLockManager
    from ..dns_utils import has_dns_backup
    if DnsLockManager.is_active() or has_dns_backup() or BrowserPolicyManager.current()["level"] != "off" \
            or core_networks.load_policy()["trusted"]:
        result = core_networks.tick()
        changed = result["changed"]
        proxy = result.get("proxy") or {}
        agent.transport.set_proxy(proxy.get("server") if proxy.get("mode") == "fixed" else None)
    try:
        if BrowserPolicyManager.apply_child_policies():  # child logged in: per-child browser policies
            changed = True
    except Exception:
        log.exception("per-child browser policies failed")
    if DnsLockManager.is_active() and time.monotonic() - _last_dns_reapply >= DNS_REAPPLY_EVERY:
        _last_dns_reapply = time.monotonic()
        changed = bool(DnsLockManager.reapply()) or changed
    return changed


def on_console_config(agent, resp: dict) -> None:
    """Settings pushed by the console with every poll (account network policy)."""
    if "network_policy" in resp:
        from .. import core_networks
        if core_networks.save_policy(resp.get("network_policy")):
            log.info("network policy updated by the console")
            agent.run_fast_checks_soon()


def helper_command(launcher_script: str = "") -> str:
    """Command line of the activity helper, started by the agent in the children's sessions."""
    program, arguments = agent_command(launcher_script)
    return f'"{program}" ' + arguments.replace("--agent-run", "--agent-user-helper")


def run_agent(launcher_script: str = "") -> int:
    setup_logging()
    if not config.load():
        log.info("this PC is not connected to a console: nothing to do")
        return EXIT_OK
    if not _is_admin():
        log.error("the remote agent needs administrator / SYSTEM rights")
        return EXIT_NOT_ADMIN
    if not _acquire_mutex():
        log.info("another agent instance is already running")
        return EXIT_OK
    from . import AGENT_VERSION, actions
    from .agent import Agent
    log.info("HOTS remote agent %s starting", AGENT_VERSION)
    try:
        from ..core_antispy import run_startup_seed
        run_startup_seed()
    except Exception:
        log.exception("privacy cache seeding failed")
    try:
        from ..secure_storage import secure_app_registry_key
        secure_app_registry_key()  # app password hash: not readable by standard accounts
    except Exception:
        log.exception("could not secure the app registry key")
    try:
        # A policy written by an older version is re-applied (e.g. the Store apps rule of version 2).
        from ..core_applocker import AppLockerManager
        if AppLockerManager.repair():
            log.info("anti-portable policy updated")
    except Exception:
        log.exception("anti-portable policy could not be checked")
    while True:
        cfg = config.load()
        if not cfg:
            log.info("this PC is not connected to a console: nothing to do")
            config.write_status(connected=False)
            return EXIT_OK
        from . import monitor
        mon = monitor.get()
        mon.start(helper_command(launcher_script))
        agent = Agent(cfg, executor=actions.execute, state_builder=_state_builder,
                      info_builder=actions.system_info, periodic=periodic_checks, periodic_fast=fast_checks,
                      on_config=on_console_config, outbox=mon.journal.outbox, outbox_ack=mon.journal.ack)
        try:
            agent.run()
        except Exception:
            log.exception("agent crashed, restarting in 30 s")
            time.sleep(30)
            continue
        if agent.revoked:
            mon.stop()
            _activity_off()
            return EXIT_OK


# --- CLI used by the installer / desktop app ----------------------------------------------------

def connect(server_url: str, code: str, launcher_script: str = "") -> tuple:
    """Enroll + register/start the task. Returns (exit_code, message). Used by CLI and GUI."""
    from . import actions
    from .agent import EnrollError, enroll
    if not _is_admin():
        return EXIT_NOT_ADMIN, "Droits administrateur requis."
    try:
        cfg = enroll(server_url, code, actions.system_info())
    except ValueError as e:
        return EXIT_REFUSED, f"Adresse du serveur invalide : {e}"
    except EnrollError as e:
        return (EXIT_NETWORK if e.network else EXIT_REFUSED), str(e)
    log.info("enrolled as device %s on %s", cfg["device_id"], cfg["server_url"])
    try:
        from ..core_netblock import NetBlockManager
        if NetBlockManager.exclusions_outdated():
            NetBlockManager.enable()
    except Exception:
        log.exception("could not refresh the Internet block exclusions")
    stop_agent()
    if not install_task(launcher_script):
        return EXIT_ERROR, "PC connecté, mais la tâche planifiée n'a pas pu être créée."
    start_task()
    return EXIT_OK, f"Connecté à {cfg['server_url']}"


def disconnect(notify_server: bool = True) -> None:
    from .agent import goodbye
    stop_agent()
    remove_task()
    cfg = config.load()
    if cfg and notify_server:
        goodbye(cfg)
    config.clear()
    _activity_off()
    try:
        from ..core_netblock import NetBlockManager
        if NetBlockManager.exclusions_outdated():
            NetBlockManager.enable()  # close the hole that was kept open for the console
    except Exception:
        log.exception("could not refresh the Internet block exclusions")


def _activity_off() -> None:
    """Remote administration ends: the activity journal (a console feature) restores the DNS log
    and browser settings it changed. Screen time stops with the agent."""
    try:
        from ..core_activity import disable, load_settings
        data = load_settings()
        if data["enabled"] or data["originals"]:
            disable()
    except Exception:
        log.exception("activity journal settings could not be restored")


def main(argv: list, launcher_script: str = "") -> int:
    """Dispatch --agent-* flags. Returns the process exit code."""
    flag = argv[1] if len(argv) > 1 else ""
    if flag == "--agent-run":
        return run_agent(launcher_script)
    if flag == "--agent-user-helper":
        # Runs with a child's rights: no log file (ProgramData is not writable), nothing else.
        from ..core_activity import run_user_helper
        return run_user_helper()
    setup_logging()
    if flag == "--agent-enroll" and len(argv) >= 4:
        code, message = connect(argv[2], argv[3], launcher_script)
        _write_result(argv[4] if len(argv) > 4 else "", message)
        log.info("enroll: %s (%s)", message, code)
        return code
    if flag == "--agent-install-task":
        if not config.is_enrolled():
            return EXIT_OK
        stop_agent()
        ok = install_task(launcher_script)
        if ok:
            start_task()
        return EXIT_OK if ok else EXIT_ERROR
    if flag == "--agent-stop":
        stop_agent()
        return EXIT_OK
    if flag == "--agent-applocker-off":
        # Emergency: lift the anti-portable mode from the PC itself (administrator command prompt).
        from ..core_applocker import AppLockerManager
        return EXIT_OK if AppLockerManager.set_mode("off") else EXIT_ERROR
    if flag == "--agent-uninstall":
        disconnect(notify_server=True)
        return EXIT_OK
    log.error("unknown agent command: %s", argv[1:])
    return EXIT_ERROR
