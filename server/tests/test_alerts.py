import asyncio
import time

from hots_server import alerts, db, settings


def _enroll(client, machine_id="m-1", hostname="PC-LEA"):
    code = client.post("/api/enroll-codes", json={}).json()["code"]
    creds = client.post("/api/agent/enroll", json={"code": code, "hostname": hostname, "machine_id": machine_id}).json()
    return creds, {"Authorization": f"Bearer {creds['device_id']}.{creds['token']}"}


def _alerts(client):
    return {a["kind"]: a for a in client.get("/api/alerts").json()["alerts"] if a["resolved_at"] is None}


def test_offline_alert_and_absence_mode(admin):
    creds, auth = _enroll(admin)
    dev = creds["device_id"]
    admin.post("/api/agent/results", json={"results": [], "info": {}}, headers=auth)
    db.ex("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time() - 50 * 3600, dev))
    alerts.evaluate(settings.ONLINE_GRACE_SECONDS)
    assert "hors ligne depuis 2 jours" in _alerts(admin)["offline"]["message"]
    assert admin.get(f"/api/devices/{dev}").json()["open_alerts"] == 1

    # absence mode: muted and resolved
    r = admin.patch(f"/api/devices/{dev}", json={"alerts_muted_until": time.time() + 7 * 86400})
    assert r.status_code == 200
    alerts.evaluate(settings.ONLINE_GRACE_SECONDS)
    assert "offline" not in _alerts(admin)

    # back online after the absence: nothing reopens
    admin.patch(f"/api/devices/{dev}", json={"alerts_muted_until": None})
    admin.post("/api/agent/results", json={"results": []}, headers=auth)
    alerts.evaluate(settings.ONLINE_GRACE_SECONDS)
    assert "offline" not in _alerts(admin)
    assert admin.patch(f"/api/devices/{dev}", json={"alerts_muted_until": 12}).status_code == 400


def test_offline_threshold_is_per_account(admin):
    creds, _auth = _enroll(admin)
    assert admin.patch("/api/account/settings", json={"offline_alert_hours": 0}).json()["offline_alert_hours"] == 0
    db.ex("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time() - 900 * 3600, creds["device_id"]))
    alerts.evaluate(settings.ONLINE_GRACE_SECONDS)
    assert "offline" not in _alerts(admin)


def test_reenroll_machine_change_and_uninstall_alerts(admin):
    creds, auth = _enroll(admin, machine_id="aaa")
    _enroll(admin, machine_id="aaa")                       # same PC connected again
    assert "relié de nouveau" in _alerts(admin)["reenrolled"]["message"]
    _enroll(admin, machine_id="bbb")                       # same name, other machine id
    assert "Windows réinstallé" in _alerts(admin)["machine_changed"]["message"]
    new = _enroll(admin, machine_id="aaa")[1]
    admin.post("/api/agent/goodbye", json={}, headers=new)
    found = _alerts(admin)
    assert found["uninstalled"]["severity"] == "danger"
    assert admin.post(f"/api/alerts/{found['reenrolled']['id']}/ack").status_code == 200
    assert "reenrolled" not in _alerts(admin)
    assert admin.post(f"/api/alerts/{found['reenrolled']['id']}/ack").status_code == 404


def test_noncompliance_and_reboot_alerts(admin):
    creds, auth = _enroll(admin)
    report = {"score": 35, "checks": [{"status": "fail", "severity": "critical",
                                       "title": "Le compte enfant « Lea » est administrateur"}]}
    now = time.time()
    for i in range(6):
        admin.post("/api/agent/results", json={"results": [], "info": {"last_boot": now - i * 3600},
                                               "state": {"compliance": {"report": report}}}, headers=auth)
    alerts.evaluate(settings.ONLINE_GRACE_SECONDS)
    found = _alerts(admin)
    assert found["noncompliant"]["severity"] == "danger" and "administrateur" in found["noncompliant"]["message"]
    assert "6 redémarrages" in found["reboots"]["message"]
    device = admin.get(f"/api/devices/{creds['device_id']}").json()
    assert device["summary"]["compliance_score"] == 35

    report = {"score": 95, "checks": []}
    admin.post("/api/agent/results", json={"results": [], "state": {"compliance": {"report": report}}}, headers=auth)
    alerts.evaluate(settings.ONLINE_GRACE_SECONDS)
    assert "noncompliant" not in _alerts(admin)


def test_alert_emails(admin, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_mail", lambda to, subject, body: sent.append((to, subject, body)))
    r = admin.patch("/api/admin/settings", json={"smtp": {"smtp_host": "smtp.example.com", "smtp_port": 587,
                                                          "smtp_from": "hots@example.com", "smtp_user": "u",
                                                          "smtp_password": "secret", "smtp_security": "starttls"}})
    smtp = r.json()["smtp"]
    assert smtp["password_set"] is True and "smtp_password" not in smtp
    creds, auth = _enroll(admin)
    admin.post("/api/agent/goodbye", json={}, headers=auth)
    from hots_server.app import send_alert_emails
    asyncio.run(send_alert_emails())
    assert len(sent) == 1 and sent[0][0] == ["admin@example.com"] and "désinstallé" in sent[0][1]
    asyncio.run(send_alert_emails())
    assert len(sent) == 1  # never sent twice

    admin.patch("/api/account/settings", json={"email_alerts": False})
    _enroll(admin, machine_id="m-1")
    asyncio.run(send_alert_emails())
    assert len(sent) == 1


def test_schema_migration_adds_columns(fresh_db):
    import sqlite3
    path = settings.DB_PATH
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL, "
                       "suspended INTEGER NOT NULL DEFAULT 0, max_devices INTEGER, notes TEXT NOT NULL DEFAULT '');"
                       "INSERT INTO accounts(name, created_at) VALUES('ancien', 1);")
    conn.commit()
    conn.close()
    db.init()
    row = db.q1("SELECT offline_alert_hours, email_alerts FROM accounts WHERE name = 'ancien'")
    assert row["offline_alert_hours"] == 48 and row["email_alerts"] == 1
