"""Child sessions monitor, run by the agent (SYSTEM) in its own thread:

  - screen time: counts use, warns (10, 5, 1 min left) and locks / logs off the child's session
    when time is up (core_screentime.Enforcer, core_sessions);
  - activity journal: starts the helper in each child session (foreground app, title, idle time),
    reads the DNS Client log for the sites, the AppLocker log for blocked programs (core_activity).

The day summaries are sent to the console with the agent's polls (Agent outbox, every 5 minutes)
or at once with the `activity.sync` action.
"""

import json
import logging
import threading
import time
from datetime import datetime

from .. import core_activity, core_screentime, core_sessions

log = logging.getLogger("HOTS.remote.monitor")

TICK = 5
DNS_EVERY = 60
APPLOCKER_EVERY = 300
SAVE_EVERY = 300
HELPER_RETRY = 10
NAMES_EVERY = 600

_instance = None
_instance_lock = threading.Lock()


def get() -> "Monitor":
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Monitor()
        return _instance


def children() -> list:
    """[{"sid", "name"}] of the accounts marked as children, with their names when known."""
    from ..compliance import cached_report, child_sids
    sids = child_sids()
    names = {}
    report = cached_report() or {}
    for acc in report.get("accounts") or []:
        if isinstance(acc, dict) and acc.get("sid"):
            names[acc["sid"]] = acc.get("full_name") or acc.get("name") or ""
    return [{"sid": sid, "name": names.get(sid) or sid} for sid in sids]


class Monitor:
    def __init__(self):
        self.enforcer = core_screentime.Enforcer()
        self.journal = core_activity.Journal()
        self.helpers = {}           # session id -> core_sessions.Helper
        self._helper_failed = {}    # session id -> time of the last failed start / exit
        self._samples = {}          # session id -> last helper sample
        self._session_sid = {}      # session id -> sid
        self._dns_rid = None
        self._last = {"dns": 0.0, "applocker": 0.0, "save": time.monotonic(), "names": 0.0}
        self._applocker_seen = set()
        self._block_index = ({}, 0.0)
        self._stop = threading.Event()
        self._thread = None
        self.helper_command = ""

    # lifecycle
    def start(self, helper_command: str) -> None:
        self.helper_command = helper_command
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hots-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for helper in list(self.helpers.values()):
            helper.stop()
        self.helpers.clear()
        self.enforcer.save(force=True)
        self.journal.save()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("monitor tick failed")
            self._stop.wait(TICK)

    # one round
    def tick(self, now: float = None) -> None:
        now = time.time() if now is None else now
        mono = time.monotonic()
        kids = children()
        sids = {c["sid"] for c in kids}
        if mono - self._last["names"] >= NAMES_EVERY or not self.journal.names:
            self._last["names"] = mono
            self.journal.names.update({c["sid"]: c["name"] for c in kids})
        policies = core_screentime.load_policies()
        settings = core_activity.load_settings()
        watching = settings["enabled"] or any(p.get("enabled") for s, p in policies.items() if s in sids)
        sessions = [s for s in core_sessions.list_sessions() if s.get("sid") in sids] if sids and watching else []
        self._manage_helpers(sessions if watching else [], now)
        for s in sessions:
            sample = self._samples.get(s["id"])
            s["idle"] = sample.get("idle") if sample and now - sample.get("_at", 0) < 30 else None
            s["name"] = self.journal.names.get(s["sid"], s.get("user"))
        for action in self.enforcer.tick(sessions, policies, now):
            self._perform(action)
        for ev in self.enforcer.events:
            if not ev.get("_logged"):
                ev["_logged"] = True
                self.journal.add_event(ev["sid"], ev["t"], ev["kind"], ev["text"])
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        for sid in sids:
            if settings["enabled"] or (policies.get(sid) or {}).get("enabled"):
                self.journal.set_screen(sid, today, self.enforcer.used(sid, now))
        if settings["enabled"] and sids:
            if mono - self._last["dns"] >= DNS_EVERY:
                self._last["dns"] = mono
                self._read_dns(sids)
            if mono - self._last["applocker"] >= APPLOCKER_EVERY:
                self._last["applocker"] = mono
                self._read_applocker(sids)
        if mono - self._last["save"] >= SAVE_EVERY:
            self._last["save"] = mono
            self.journal.save()

    def _perform(self, action) -> None:
        kind, session_id = action[0], action[1]
        try:
            if kind == "message":
                core_sessions.send_message(session_id, action[2], action[3])
            elif kind == "lock":
                if action[2] == "logoff":
                    core_sessions.logoff_session(session_id)
                else:
                    core_sessions.lock_session(session_id)
                log.info("screen time: session %s %s", session_id, "logged off" if action[2] == "logoff" else "locked")
        except Exception:
            log.exception("screen time action %s failed", kind)

    # helpers in the child sessions
    def _manage_helpers(self, sessions: list, now: float) -> None:
        wanted = {s["id"]: s["sid"] for s in sessions}
        for session_id in list(self.helpers):
            helper = self.helpers[session_id]
            if session_id not in wanted:
                helper.stop()
                del self.helpers[session_id]
                self._samples.pop(session_id, None)
            elif not helper.alive():
                helper.stop()
                del self.helpers[session_id]
                self._helper_failed[session_id] = now
                sid = wanted[session_id]
                if now - helper.started > 30:  # closed by someone, not a start-up failure
                    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
                    self.journal.get(day, sid)["helper"]["restarts"] += 1
                    self.journal.add_event(sid, now, "helper", "Le suivi d'activité a été fermé (redémarré)")
        for session_id, sid in wanted.items():
            self._session_sid[session_id] = sid
            if session_id in self.helpers or not self.helper_command:
                continue
            if now - self._helper_failed.get(session_id, 0) < HELPER_RETRY:
                continue
            helper = core_sessions.spawn_helper(session_id, self.helper_command, self._on_line)
            if helper:
                self.helpers[session_id] = helper
            else:
                self._helper_failed[session_id] = now

    def _on_line(self, session_id: int, line: str) -> None:
        try:
            sample = json.loads(line)
        except ValueError:
            return
        if not isinstance(sample, dict):
            return
        sample["_at"] = time.time()
        sample["t"] = sample["_at"]  # the agent's clock, not the helper's
        self._samples[session_id] = sample
        sid = self._session_sid.get(session_id)
        settings = core_activity.load_settings()
        if sid and settings["enabled"]:
            self.journal.add_sample(session_id, sid, sample, titles=settings["titles"])

    # DNS and AppLocker logs
    def _index(self) -> dict:
        from ..constants import HOSTS_PATH
        index, at = self._block_index
        if time.monotonic() - at > 300:
            try:
                with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
                    index = core_activity.hosts_block_index(f.read())
            except OSError:
                index = {}
            self._block_index = (index, time.monotonic())
        return index

    def _read_dns(self, sids: set) -> None:
        if self._dns_rid is None:
            self._dns_rid = core_activity.last_dns_record()
            return
        events = core_activity.read_dns_events(self._dns_rid)
        if not events:
            return
        self._dns_rid = events[-1]["rid"]
        procs = core_sessions.process_table()
        index = self._index()
        for ev in events:
            proc = procs.get(ev["pid"])
            if not proc or proc[2] not in sids:
                continue
            self.journal.add_dns(proc[2], ev, proc[1], index)

    def _read_applocker(self, sids: set) -> None:
        from ..core_applocker import AppLockerManager, collect_events
        if AppLockerManager.current()["mode"] != "enforce":
            return
        for ev in collect_events(days=1):
            if not isinstance(ev, dict) or ev.get("id") != 8004 or ev.get("user") not in sids:
                continue
            key = (ev.get("time"), ev.get("path"))
            if key in self._applocker_seen:
                continue
            self._applocker_seen.add(key)
            t = core_activity._parse_time(str(ev.get("time") or ""))
            self.journal.add_blocked_program(ev["user"], str(ev.get("path") or ""), t)
        if len(self._applocker_seen) > 5000:
            self._applocker_seen = set(list(self._applocker_seen)[-2000:])

    # console
    def screentime_status(self) -> list:
        return core_screentime.status(children(), self.enforcer)

    def activity_status(self) -> dict:
        settings = core_activity.load_settings()
        return {"enabled": settings["enabled"], "titles": settings["titles"],
                "helpers": len(self.helpers), "children": children()}

    def today(self) -> list:
        day = datetime.now().strftime("%Y-%m-%d")
        out = []
        for c in children():
            self.journal.names[c["sid"]] = c["name"]
            out.append(self.journal.summary(self.journal.get(day, c["sid"])))
        # plus the other days not sent yet (e.g. yesterday evening)
        out += [x for x in self.journal.outbox(limit=10) if x["day"] != day]
        return out
