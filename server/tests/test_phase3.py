"""Phase 3: screen time (slots, quotas, bonus, pause, enforcement) and the activity journal."""

import sys
import types
from datetime import datetime

import pytest

sys.modules.setdefault("winreg", types.ModuleType("winreg"))

from hosts_editor import core_activity as act, core_screentime as st  # noqa: E402

KID = "S-1-5-21-111-222-333-1001"
WEEK = [{"ranges": [["07:30", "12:00"], ["13:30", "20:30"]], "quota": 120}] * 5 + \
       [{"ranges": [["09:00", "21:00"]], "quota": 180}, {"ranges": [], "quota": None}]


def policy(**kw):
    return dict(st.clean_policy({"enabled": True, "days": WEEK}), bonus={}, pause_until=0, **kw)


@pytest.fixture()
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "POLICY_FILE", str(tmp_path / "policy.json"))
    monkeypatch.setattr(st, "USAGE_FILE", str(tmp_path / "usage.json"))
    return tmp_path


def test_policy_validation_merges_ranges_and_rejects_bad_input():
    day = st.clean_day({"ranges": [["14:00", "16:00"], ["08:00", "12:00"], ["11:00", "13:00"]], "quota": 90})
    assert day == {"ranges": [["08:00", "13:00"], ["14:00", "16:00"]], "quota": 90}
    assert st.clean_day({"ranges": [["00:00", "24:00"]], "quota": None})["ranges"] == [["00:00", "24:00"]]
    for bad in ({"ranges": [["10:00", "09:00"]]}, {"ranges": [["25:00", "26:00"]]}, {"ranges": [["9h", "10h"]]},
                {"ranges": [], "quota": 2000}, {"ranges": [], "quota": True}):
        with pytest.raises(ValueError):
            st.clean_day(bad)
    with pytest.raises(ValueError):
        st.clean_policy({"enabled": True, "days": WEEK[:6]})


def test_evaluate_slots_quota_and_next_start():
    p = policy()
    wed_10 = datetime(2026, 11, 18, 10, 0)  # a Wednesday
    ev = st.evaluate(p, 30 * 60, wed_10)
    assert ev["allowed"] and ev["remaining_min"] == 90 and ev["until"] == "11:30"  # quota is the limit
    ev = st.evaluate(p, 0, datetime(2026, 11, 18, 11, 50))
    assert ev["allowed"] and ev["remaining_min"] == 10 and ev["until"] == "12:00"  # end of the slot
    ev = st.evaluate(p, 0, datetime(2026, 11, 18, 12, 30))
    assert not ev["allowed"] and ev["reason"] == "outside" and ev["next_allowed"] == "à 13:30"
    ev = st.evaluate(p, 120 * 60, wed_10)
    assert not ev["allowed"] and ev["reason"] == "quota" and ev["next_allowed"] == "demain à 07:30"
    ev = st.evaluate(p, 0, datetime(2026, 11, 22, 15, 0))  # Sunday: no computer at all
    assert not ev["allowed"] and ev["reason"] == "no_time_today" and ev["next_allowed"] == "demain à 07:30"
    ev = st.evaluate(p, 0, datetime(2026, 11, 20, 21, 0))  # Friday evening
    assert ev["reason"] == "outside" and ev["next_allowed"] == "demain à 09:00"
    assert st.evaluate(dict(p, enabled=False), 10 ** 6, wed_10)["allowed"]
    unlimited = dict(st.default_policy(), enabled=True)
    assert st.evaluate(unlimited, 10 ** 5, wed_10)["remaining_min"] is None


def test_bonus_and_pause(files):
    now = datetime(2026, 11, 18, 20, 40).timestamp()  # after the last slot
    st.set_policy(KID, {"enabled": True, "days": WEEK})
    assert st.evaluate(st.load_policies()[KID], 0, datetime.fromtimestamp(now))["reason"] == "outside"
    st.add_bonus(KID, 30, now=now)
    ev = st.evaluate(st.load_policies()[KID], 0, datetime.fromtimestamp(now))
    assert ev["allowed"] and ev["remaining_min"] == 30 and ev["until"] == "21:10"
    later = datetime.fromtimestamp(now + 31 * 60)
    assert not st.evaluate(st.load_policies()[KID], 0, later)["allowed"]
    # quota reached during a slot: +15 min of quota
    noon = datetime(2026, 11, 18, 15, 0).timestamp()
    st.add_bonus(KID, 15, now=noon)
    ev = st.evaluate(st.load_policies()[KID], 120 * 60, datetime.fromtimestamp(noon))
    assert ev["allowed"] and ev["quota_min"] == 120 + 30 + 15
    st.set_pause(KID, 60, now=noon)
    ev = st.evaluate(st.load_policies()[KID], 0, datetime.fromtimestamp(noon))
    assert ev["reason"] == "paused" and ev["next_allowed"] == "à 16:00"
    st.set_pause(KID, 0, now=noon)
    assert st.evaluate(st.load_policies()[KID], 0, datetime.fromtimestamp(noon))["allowed"]
    st.set_pause(KID, -1, now=noon)
    assert st.evaluate(st.load_policies()[KID], 0, datetime.fromtimestamp(noon))["next_allowed"] == "demain à 00:00"


def test_enforcer_counts_warns_and_locks(files):
    pol = {KID: policy()}
    enf = st.Enforcer()
    start = datetime(2026, 11, 18, 11, 40).timestamp()
    session = {"id": 3, "sid": KID, "active": True, "locked": False, "idle": 10}
    actions = []
    t = start
    while t < start + 22 * 60:
        actions += [(round(t - start), a) for a in enf.tick([session], pol, t)]
        t += 5
    messages = [(sec, a[3]) for sec, a in actions if a[0] == "message"]
    texts = [m for _, m in messages]
    assert any("10 minutes" in m for m in texts) and any("5 minutes" in m for m in texts)
    assert any("1 minute " in m for m in texts)
    assert sum("Il reste" in m for m in texts) == 3  # each warning once
    time_up = [sec for sec, m in messages if "pas une heure autorisée" in m]
    locks = [(sec, a) for sec, a in actions if a[0] == "lock"]
    assert time_up and time_up[0] == 20 * 60  # 12:00 = end of the slot
    assert locks and locks[0][0] == 20 * 60 + 60 and locks[0][1] == ("lock", 3, "lock")  # after 1 min of grace
    assert 19 * 60 <= enf.used(KID, t) <= 22 * 60
    kinds = [e["kind"] for e in enf.events]
    assert kinds[:2] == ["time_up", "locked"]


def test_enforcer_ignores_idle_locked_and_other_accounts(files):
    pol = {KID: policy()}
    enf = st.Enforcer()
    t = datetime(2026, 11, 18, 10, 0).timestamp()
    sessions = [{"id": 1, "sid": KID, "active": True, "locked": True, "idle": 0},
                {"id": 2, "sid": "S-1-5-21-1-2-3-500", "active": True, "locked": False, "idle": 0}]
    for i in range(50):
        assert enf.tick(sessions, pol, t + i * 5) == []
    assert enf.used(KID, t) == 0
    sessions[0].update(locked=False, idle=st.IDLE_LIMIT + 1)
    for i in range(50, 60):
        enf.tick(sessions, pol, t + i * 5)
    assert enf.used(KID, t) == 0


DNS_XML = """<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>3008</EventID>
<TimeCreated SystemTime='{t}'/><EventRecordID>{rid}</EventRecordID><Execution ProcessID='{pid}' ThreadID='1'/></System>
<EventData><Data Name='QueryName'>{name}</Data><Data Name='QueryType'>1</Data><Data Name='QueryOptions'>0</Data>
<Data Name='QueryStatus'>0</Data><Data Name='QueryResults'>{res}</Data></EventData></Event>"""


def test_dns_events_parse_and_journal(tmp_path):
    xml = "".join(DNS_XML.format(t=t, rid=rid, pid=pid, name=name, res=res) for t, rid, pid, name, res in (
        ("2026-11-18T09:00:05.1234567Z", 11, 40, "www.youtube.com", "142.250.1.1;"),
        ("2026-11-18T09:00:07.0000000Z", 10, 40, "rr3---sn-x.googlevideo.com", "::ffff:1.2.3.4;"),
        ("2026-11-18T09:01:00.0000000Z", 12, 40, "www.tiktok.com", "0.0.0.0;"),
        ("2026-11-18T09:01:20.0000000Z", 13, 40, "www.tiktok.com", "0.0.0.0;"),
        ("2026-11-18T09:02:00.0000000Z", 14, 40, "pornsite.example", "0.0.0.0;"),
        ("2026-11-18T09:02:30.0000000Z", 15, 77, "vortex.data.microsoft.com", "0.0.0.0;"),
        ("2026-11-18T09:03:00.0000000Z", 16, 77, "updates.vendor.example", "5.6.7.8;"),
    ))
    events = act.parse_dns_events(xml)
    assert [e["rid"] for e in events] == [10, 11, 12, 13, 14, 15, 16]
    assert events[1]["name"] == "www.youtube.com" and events[1]["pid"] == 40
    index = act.hosts_block_index("# === HOSTS_EDITOR_PARENTAL_TIKTOK_START ===\n0.0.0.0 www.tiktok.com\n"
                                  "# === HOSTS_EDITOR_PARENTAL_TIKTOK_END ===\n"
                                  "# === HOSTS_EDITOR_PARENTAL_TELEMETRY_START ===\n0.0.0.0 vortex.data.microsoft.com\n"
                                  "# === HOSTS_EDITOR_PARENTAL_TELEMETRY_END ===\n")
    j = act.Journal(str(tmp_path))
    j.names[KID] = "Léa"
    for ev in events:
        j.add_dns(KID, ev, "chrome.exe" if ev["pid"] == 40 else "vendorupdate.exe", index)
    day = datetime.fromtimestamp(events[0]["t"]).strftime("%Y-%m-%d")
    s = act.Journal.summary(j.get(day, KID))
    sites = {x["site"]: x for x in s["sites"]}
    assert set(sites) == {"youtube.com", "tiktok.com", "pornsite.example"}  # telemetry and background ignored
    assert sites["youtube.com"]["n"] == 2 and sites["youtube.com"]["blocked"] == 0
    assert sites["tiktok.com"]["blocked"] == 1 and sites["tiktok.com"]["by"] == "TikTok"  # once per minute
    assert sites["pornsite.example"]["by"] == act.FAMILY_FILTER
    assert s["user"] == "Léa" and "_m" not in sites["youtube.com"]


def test_journal_samples_apps_titles_and_site_time(tmp_path):
    j = act.Journal(str(tmp_path))
    t0 = datetime(2026, 11, 18, 16, 0).timestamp()
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    for i in range(0, 37):  # 3 minutes of Chrome, then 15 s of Minecraft, then idle
        j.add_sample(4, KID, {"t": t0 + i * 5, "exe": chrome, "title": "Fortnite - YouTube - Google Chrome", "idle": 2})
    for i in range(37, 41):
        j.add_sample(4, KID, {"t": t0 + i * 5, "exe": r"C:\Users\lea\Minecraft\javaw.exe", "title": "Minecraft",
                              "idle": 1, "desc": "Java(TM) Platform SE binary"})
    for i in range(41, 60):
        j.add_sample(4, KID, {"t": t0 + i * 5, "exe": chrome, "title": "x", "idle": act.IDLE_LIMIT + 5})
    for minute in range(0, 3):
        j.add_dns(KID, {"name": "www.youtube.com", "t": t0 + minute * 60 + 10, "results": "1.1.1.1", "status": 0},
                  "chrome.exe", {})
    s = act.Journal.summary(j.get("2026-11-18", KID))
    apps = {a["exe"]: a for a in s["apps"]}
    assert apps["chrome.exe"]["s"] == 180 and apps["chrome.exe"]["name"] == "Google Chrome"
    assert apps["javaw.exe"]["s"] == 20 and apps["javaw.exe"]["name"] == "Java (Minecraft…)"
    assert s["active_s"] == 200 and s["hours"][16] == 200
    assert s["titles"][0]["title"] == "Fortnite - YouTube" and s["titles"][0]["s"] == 180
    assert {x["site"]: x["s"] for x in s["sites"]}["youtube.com"] == 180
    j.add_blocked_program(KID, r"D:\portable\FirefoxPortable.exe", t0 + 400)
    j.add_event(KID, t0 + 500, "locked", "Session verrouillée")
    j.save()
    j2 = act.Journal(str(tmp_path))
    s2 = act.Journal.summary(j2.get("2026-11-18", KID))
    assert s2["blocked_apps"][0]["name"] == "FirefoxPortable.exe" and s2["events"][0]["kind"] == "locked"
    assert {a["exe"] for a in s2["apps"]} == {"chrome.exe", "javaw.exe"}
    out = j.outbox()
    assert out and out[0]["sid"] == KID
    j.ack(out)
    assert j.outbox() == []


def test_site_grouping():
    assert act.site_of("r5---sn-25ge7nsl.googlevideo.com") == "youtube.com"
    assert act.site_of("pronote.ac-lyon.fr") == "ac-lyon.fr"
    assert act.site_of("www.bbc.co.uk") == "bbc.co.uk"
    for noise in ("fonts.gstatic.com", "192.168.1.10", "wpad", "printer.local", "clients2.google.com.lan"):
        assert act.site_of(noise) == ""


def test_server_stores_and_serves_activity(admin):
    from hots_server import activity as server_activity
    from hots_server.app import ACTIONS
    assert ACTIONS["screentime.set"] and not ACTIONS["activity.sync"]
    client = admin
    from hots_server import db
    code = client.post("/api/enroll-codes", json={}).json()["code"]
    agent = client.post("/api/agent/enroll", json={"code": code, "hostname": "PC-LEA", "machine_id": "m"}).json()
    auth = {"Authorization": f"Bearer {agent['device_id']}.{agent['token']}"}
    summary = {"day": "2026-11-18", "sid": KID, "user": "Léa", "screen_s": 3600, "hours": [0] * 16 + [3600] + [0] * 7,
               "apps": [{"name": "Chrome", "exe": "chrome.exe", "s": 1800, "browser": True, "evil": "<x>"}],
               "sites": [{"site": "youtube.com", "n": 4, "blocked": 0, "s": 600}, "junk"],
               "events": [{"t": 1, "kind": "locked", "text": "Session verrouillée"}]}
    bad = {"day": "2026-11-18", "sid": "not-a-sid"}
    r = client.post("/api/agent/poll", json={"activity": [summary, bad]}, headers=auth)
    assert r.status_code == 200
    data = client.get(f"/api/devices/{agent['device_id']}/activity").json()
    assert data["day"] == "2026-11-18" and data["days"] == ["2026-11-18"]
    user = data["users"][0]
    assert user["user"] == "Léa" and user["apps"] == [{"name": "Chrome", "exe": "chrome.exe", "path": "", "s": 1800,
                                                         "first": 0, "last": 0, "browser": True}]
    assert user["sites"][0]["site"] == "youtube.com" and len(user["sites"]) == 1
    assert data["week"][0]["screen_s"] == 3600
    # a newer summary of the same day replaces it; another account cannot read it
    client.post("/api/agent/poll", json={"activity": [dict(summary, screen_s=4000)]}, headers=auth)
    assert client.get(f"/api/devices/{agent['device_id']}/activity").json()["users"][0]["screen_s"] == 4000
    db.ex("UPDATE activity_days SET day = '2020-01-01'")
    server_activity.purge()
    assert client.get(f"/api/devices/{agent['device_id']}/activity").json()["users"] == []
