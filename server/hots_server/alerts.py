"""Presence / tamper alerts and their e-mail notification.

Condition alerts (opened and resolved automatically):
  offline       PC not seen for more than the account threshold (default 48 h; muted by "absence")
  reboots       6 boots or more within 24 h (someone may be trying recovery / safe mode)
  noncompliant  compliance score < 60 or a critical check failing (child admin, protections undone…)
Event alerts (resolved by "Marquer comme lu", or automatically after 7 days):
  reenrolled    the PC was connected again with a new code (reinstall?)
  machine_changed  a "new" PC with the hostname of an existing one (Windows reinstalled?)
  uninstalled   HOTS Hosts was uninstalled from the PC
  network       filter suspended for a captive portal, or a network that blocks the filter
"""

import logging
import smtplib
import ssl
import time
from email.message import EmailMessage

from . import db

log = logging.getLogger("hots_server.alerts")

EVENT_KINDS = ("reenrolled", "machine_changed", "uninstalled", "network")
EVENT_TTL = 7 * 86400
REBOOTS_THRESHOLD = 6
NONCOMPLIANT_SCORE = 60


def open_alert(account_id: int, device_id, kind: str, message: str, severity: str = "warn") -> bool:
    """Open an alert unless the same one is already open. Returns True if created."""
    row = db.q1("SELECT id FROM alerts WHERE account_id = ? AND device_id IS ? AND kind = ? AND resolved_at IS NULL",
                (account_id, device_id, kind))
    if row:
        db.ex("UPDATE alerts SET message = ? WHERE id = ?", (message[:500], row["id"]))
        return False
    db.ex("INSERT INTO alerts(account_id, device_id, kind, severity, message, created_at) VALUES(?,?,?,?,?,?)",
          (account_id, device_id, kind, severity, message[:500], time.time()))
    return True


def resolve(device_id, kind: str) -> None:
    db.ex("UPDATE alerts SET resolved_at = ? WHERE device_id IS ? AND kind = ? AND resolved_at IS NULL",
          (time.time(), device_id, kind))


def record_boot(device, last_boot) -> None:
    """Called on each poll with the PC boot time; keeps the boots of the last days."""
    if not isinstance(last_boot, (int, float)) or isinstance(last_boot, bool):
        return
    previous = device["last_boot"]
    if previous is not None and abs(previous - last_boot) < 180:
        return
    boots = db.loads(device["boots_json"], []) or []
    boots = [b for b in boots if isinstance(b, (int, float)) and b > time.time() - 7 * 86400]
    boots.append(float(last_boot))
    db.ex("UPDATE devices SET last_boot = ?, boots_json = ? WHERE id = ?",
          (float(last_boot), db.dumps(boots[-50:]), device["id"]))


def _compliance(state) -> tuple:
    report = ((state or {}).get("compliance") or {}).get("report") if isinstance(state, dict) else None
    if not isinstance(report, dict) or not isinstance(report.get("score"), int):
        return None, []
    critical = [c.get("title", "") for c in report.get("checks") or []
                if c.get("status") == "fail" and c.get("severity") in ("critical", "high")]
    return report["score"], critical


def _ago(seconds: float) -> str:
    hours = seconds / 3600
    return f"{int(hours)} h" if hours < 48 else f"{int(hours // 24)} jours"


def evaluate(online_grace: float) -> int:
    """Open / resolve condition alerts for every device. Returns the number of new alerts."""
    now = time.time()
    created = 0
    rows = db.q("SELECT d.*, a.offline_alert_hours, a.suspended FROM devices d JOIN accounts a ON a.id = d.account_id")
    for d in rows:
        if d["suspended"] or d["uninstalled_at"]:
            for kind in ("offline", "reboots", "noncompliant"):
                resolve(d["id"], kind)
            continue
        muted = bool(d["alerts_muted_until"] and d["alerts_muted_until"] > now)
        hours = d["offline_alert_hours"] or 0
        offline_for = now - (d["last_seen"] or d["enrolled_at"])
        if hours > 0 and not muted and offline_for > hours * 3600:
            created += open_alert(d["account_id"], d["id"], "offline",
                                  f"{d['name']} : hors ligne depuis {_ago(offline_for)}")
        elif offline_for < online_grace or muted or hours <= 0:
            resolve(d["id"], "offline")
        boots = [b for b in db.loads(d["boots_json"], []) or [] if isinstance(b, (int, float)) and b > now - 86400]
        if len(boots) >= REBOOTS_THRESHOLD and not muted:
            created += open_alert(d["account_id"], d["id"], "reboots",
                                  f"{d['name']} : {len(boots)} redémarrages en 24 h")
        else:
            resolve(d["id"], "reboots")
        score, critical = _compliance(db.loads(d["state_json"]))
        if score is not None and (score < NONCOMPLIANT_SCORE or critical):
            detail = critical[0] if critical else f"score de sécurité {score}/100"
            created += open_alert(d["account_id"], d["id"], "noncompliant",
                                  f"{d['name']} : {detail}", severity="danger" if critical else "warn")
        elif score is not None:
            resolve(d["id"], "noncompliant")
    db.ex(f"UPDATE alerts SET resolved_at = ? WHERE resolved_at IS NULL AND created_at < ? "
          f"AND kind IN ({','.join('?' * len(EVENT_KINDS))})", (now, now - EVENT_TTL, *EVENT_KINDS))
    db.ex("DELETE FROM alerts WHERE resolved_at IS NOT NULL AND resolved_at < ?", (now - 90 * 86400,))
    return created


# --- e-mail ------------------------------------------------------------------------------------------------

SMTP_KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "smtp_security")


def smtp_settings() -> dict:
    conf = {k: db.get_meta(k) or "" for k in SMTP_KEYS}
    conf["smtp_security"] = conf["smtp_security"] or "starttls"
    return conf


def smtp_configured() -> bool:
    conf = smtp_settings()
    return bool(conf["smtp_host"] and conf["smtp_from"])


def send_mail(to: list, subject: str, body: str) -> None:
    """Blocking: call from a thread. Raises on failure."""
    conf = smtp_settings()
    if not (conf["smtp_host"] and conf["smtp_from"]):
        raise RuntimeError("Serveur d'envoi (SMTP) non configuré.")
    msg = EmailMessage()
    msg["From"] = conf["smtp_from"]
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    port = int(conf["smtp_port"] or (465 if conf["smtp_security"] == "ssl" else 587))
    ctx = ssl.create_default_context()
    if conf["smtp_security"] == "ssl":
        server = smtplib.SMTP_SSL(conf["smtp_host"], port, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(conf["smtp_host"], port, timeout=20)
    try:
        if conf["smtp_security"] == "starttls":
            server.starttls(context=ctx)
        if conf["smtp_user"]:
            server.login(conf["smtp_user"], conf["smtp_password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def pending_emails() -> list:
    """[(alert_ids, recipients, subject, body)] for accounts with new, not yet e-mailed alerts."""
    if not smtp_configured():
        return []
    rows = db.q("SELECT al.*, a.name AS account_name FROM alerts al JOIN accounts a ON a.id = al.account_id "
                "WHERE al.emailed_at IS NULL AND al.resolved_at IS NULL AND a.email_alerts = 1 AND a.suspended = 0 "
                "AND al.created_at > ? ORDER BY al.account_id, al.id", (time.time() - 86400,))
    by_account = {}
    for r in rows:
        by_account.setdefault(r["account_id"], []).append(r)
    url = db.get_meta("public_url") or ""
    out = []
    for account_id, alerts in by_account.items():
        to = [u["email"] for u in db.q("SELECT email FROM users WHERE account_id = ? AND disabled = 0", (account_id,))]
        if not to:
            continue
        lines = [f"• {a['message']}" for a in alerts]
        subject = f"[HOTS] {alerts[0]['message']}" if len(alerts) == 1 else f"[HOTS] {len(alerts)} alertes"
        body = ("Bonjour,\n\nLa console HOTS Hosts signale :\n\n" + "\n".join(lines) +
                (f"\n\nOuvrir la console : {url}" if url else "") +
                "\n\nPour ne plus recevoir ces e-mails : Mon compte > Alertes.\n")
        out.append(([a["id"] for a in alerts], to, subject, body))
    return out


def mark_emailed(ids: list) -> None:
    if ids:
        db.ex(f"UPDATE alerts SET emailed_at = ? WHERE id IN ({','.join('?' * len(ids))})", (time.time(), *ids))


def public(row, device_name: str = "") -> dict:
    return {"id": row["id"], "device_id": row["device_id"], "device": device_name, "kind": row["kind"],
            "severity": row["severity"], "message": row["message"], "created_at": row["created_at"],
            "resolved_at": row["resolved_at"]}
