"""LabBot 테스트: Slack API는 가짜 클라이언트로 대체하고, Bolt 디스패치까지 실제로 통과시킵니다."""
import json
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from slack_bolt.request import BoltRequest
from slack_sdk import WebClient
from slack_sdk.web import SlackResponse
from slack_sdk.webhook import WebhookClient, WebhookResponse

from app import attendance as att
from app import todo
from app.config import Config
from app.main import create_app
from app.timeparse import parse_due, parse_duration

TZ = ZoneInfo("Asia/Seoul")
ME, BOSS, KIM = "U00ME", "U0BOSS", "U00KIM"


# ── 가짜 Slack ─────────────────────────────────────────────
class FakeClient(WebClient):
    def __init__(self):
        super().__init__(token="xoxb-test")
        self.calls = []
        self.members = [ME, BOSS, KIM, "UBOT"]

    def fake_call(self, api_method, *, http_verb="POST", files=None, data=None, params=None,
                  json=None, headers=None, auth=None):  # noqa: A002
        args = {**(data or {}), **(params or {}), **(json or {})}
        self.calls.append((api_method, args))
        if "blocks" in args:
            validate_blocks(_load(args["blocks"]))
        if "view" in args:
            validate_view(_load(args["view"]))
        res = {"ok": True}
        if api_method == "users.info":
            u = args["user"]
            res["user"] = {"id": u, "name": u.lower(), "is_bot": u == "UBOT",
                           "profile": {"display_name": {ME: "나", BOSS: "교수님", KIM: "김철수"}.get(u, u)}}
        elif api_method == "conversations.members":
            res["members"] = self.members
        elif api_method == "conversations.open":
            res["channel"] = {"id": "D" + args["users"]}
        elif api_method == "chat.postMessage":
            res.update(channel=args["channel"], ts=f"{time.time():.6f}")
        elif api_method == "chat.getPermalink":
            res["permalink"] = "https://slack.example/p1"
        return SlackResponse(client=self, http_verb="POST", api_url=api_method, req_args={},
                             data=res, headers={}, status_code=200)

    def of(self, method):
        return [a for m, a in self.calls if m == method]


def _load(x):
    return json.loads(x) if isinstance(x, str) else x


def validate_blocks(blocks):
    assert len(blocks) <= 100
    for b in blocks:
        assert b["type"] in {"section", "actions", "context", "header", "divider", "input"}, b
        if b["type"] == "section":
            assert 0 < len(b["text"]["text"]) <= 3000
        if b["type"] == "header":
            assert len(b["text"]["text"]) <= 150
        els = b.get("elements", []) if b["type"] == "actions" else []
        if "accessory" in b:
            els = [b["accessory"]]
        ids = [e["action_id"] for e in els if "action_id" in e]
        assert len(ids) == len(set(ids)), f"블록 안에 action_id 중복: {ids}"
        for e in els:
            if e.get("type") == "button":
                assert len(e["text"]["text"]) <= 75 and len(e["value"]) <= 2000


def validate_view(v):
    if v["type"] == "modal":
        assert len(v["title"]["text"]) <= 24
    validate_blocks(v["blocks"])
    if v["type"] == "modal":
        assert len(v.get("private_metadata", "")) <= 3000


@pytest.fixture()
def env(tmp_path, monkeypatch):
    responded = []

    def fake_send(self, body, headers=None):
        responded.append(body)
        return WebhookResponse(url=self.url, status_code=200, body="ok", headers={})
    monkeypatch.setattr(WebhookClient, "send_dict", fake_send)

    cfg = Config(bot_token="xoxb-test", db_path=str(tmp_path / "t.db"), admin_ids=[BOSS],
                 leave_approver_ids=[BOSS], attendance_channel="CATT", server_channel="CSRV",
                 metrics_token="secret")
    client = FakeClient()
    # Bolt는 요청마다 새 WebClient를 만들어 핸들러에 넘기므로, 클래스 수준에서 가로챕니다.
    monkeypatch.setattr(WebClient, "api_call", lambda self, api_method, **kw: client.fake_call(api_method, **kw))
    app, ctx, mon = create_app(cfg, verify_token=False, client=client, sync=True)

    class E:
        pass
    e = E()
    e.app, e.ctx, e.mon, e.client, e.responded = app, ctx, mon, client, responded

    def command(cmd, text="", user=ME, channel="CGEN"):
        body = urlencode({"command": cmd, "text": text, "user_id": user, "channel_id": channel,
                          "team_id": "T1", "trigger_id": "trig", "response_url": "https://hooks.example/r"})
        r = app.dispatch(BoltRequest(body=body, mode="socket_mode"))
        assert r.status == 200, r.body
        return r

    def action(action_id, value="", user=ME, container=None, view=None, channel="D00ME"):
        payload = {"type": "block_actions", "user": {"id": user}, "team": {"id": "T1"},
                   "trigger_id": "trig", "response_url": "https://hooks.example/r",
                   "container": container or {"type": "message", "channel_id": channel, "message_ts": "1.1"},
                   "channel": {"id": channel}, "message": {"ts": "1.1", "text": ""},
                   "actions": [{"action_id": action_id, "value": value, "block_id": "b", "type": "button"}]}
        if view:
            payload["view"] = view
        r = app.dispatch(BoltRequest(body=urlencode({"payload": json.dumps(payload)}), mode="socket_mode"))
        assert r.status == 200, r.body
        return r

    def submit(callback_id, values, user=ME, metadata=""):
        state = {"values": {bid: {"v": v} for bid, v in values.items()}}
        payload = {"type": "view_submission", "user": {"id": user}, "team": {"id": "T1"},
                   "view": {"id": "V1", "callback_id": callback_id, "state": state,
                            "private_metadata": metadata, "type": "modal"}}
        r = app.dispatch(BoltRequest(body=urlencode({"payload": json.dumps(payload)}), mode="socket_mode"))
        return r

    e.command, e.action, e.submit = command, action, submit
    return e


# ── 시간 파싱 ──────────────────────────────────────────────
NOW = datetime(2026, 10, 1, 19, 0, tzinfo=TZ)  # 목요일


@pytest.mark.parametrize("text,expect_date,expect_time,rest", [
    ("논문 리뷰 내일", date(2026, 10, 2), None, "논문 리뷰"),
    ("실험 정리 금요일까지", date(2026, 10, 2), None, "실험 정리"),
    ("발표 준비 다음주 화요일 14:00", date(2026, 10, 6), "14:00", "발표 준비"),
    ("보고서 10/5", date(2026, 10, 5), None, "보고서"),
    ("작년 일 9/1", date(2027, 9, 1), None, "작년 일"),
    ("마감 2026-12-31 오후 3시", date(2026, 12, 31), "15:00", "마감"),
    ("세미나 목요일 3시 반", date(2026, 10, 1), "15:30", "세미나"),
    ("3시간 실험 돌리기", None, None, "3시간 실험 돌리기"),
    ("그냥 할 일", None, None, "그냥 할 일"),
    ("회의 20:00", date(2026, 10, 1), "20:00", "회의"),
    ("회의 18:00", date(2026, 10, 2), "18:00", "회의"),  # 이미 지난 시각 → 내일
    # 영어 (NOW = 2026-10-01 목 19:00)
    ("review paper tomorrow 14:00", date(2026, 10, 2), "14:00", "review paper"),
    ("run baselines by friday", date(2026, 10, 2), None, "run baselines"),
    ("draft next tue 3pm", date(2026, 10, 6), "15:00", "draft"),
    ("slides fri at 3:30pm", date(2026, 10, 2), "15:30", "slides"),
    ("report by 10/5", date(2026, 10, 5), None, "report"),
    ("call 9pm", date(2026, 10, 1), "21:00", "call"),
    ("lunch 12pm", date(2026, 10, 2), "12:00", "lunch"),
    ("submit today", date(2026, 10, 1), None, "submit"),
    ("read sunday paper", date(2026, 10, 4), None, "read paper"),
    ("fix monitor script", None, None, "fix monitor script"),
    ("check sun dataset", None, None, "check sun dataset"),
    ("satellite images", None, None, "satellite images"),
])
def test_parse_due(text, expect_date, expect_time, rest):
    due, has_time, remaining = parse_due(text, NOW)
    assert remaining == rest
    if expect_date is None:
        assert due is None
    else:
        assert due.date() == expect_date
        assert has_time == (expect_time is not None)
        if expect_time:
            assert f"{due:%H:%M}" == expect_time


def test_parse_duration():
    assert parse_duration("3h") == timedelta(hours=3)
    assert parse_duration("1h30m") == timedelta(minutes=90)
    assert parse_duration("2일") == timedelta(days=2)
    assert parse_duration("abc") is None
    assert parse_duration("3x") is None


# ── Todo ───────────────────────────────────────────────────
def test_todo_add_list_done(env):
    env.command("/todo", "review paper tomorrow 14:00")
    t = env.ctx.db.one("SELECT * FROM tasks")
    assert t["title"] == "review paper" and t["assignee"] == ME and t["due_has_time"] == 1
    assert "추가했어요" in env.responded[-1]["text"]

    env.command("/todo", "list")
    assert "내 할 일" in env.responded[-1]["text"]
    assert any(b.get("accessory", {}).get("action_id") == "todo_done" for b in env.responded[-1]["blocks"])

    env.action("todo_done", f"{t['id']}|mine", container={"type": "message", "is_ephemeral": True})
    assert env.ctx.db.one("SELECT status FROM tasks")["status"] == "done"
    assert env.responded[-1]["replace_original"] is True


def test_todo_delegate_notifies(env):
    env.command("/todo", f"clean up data <@{KIM}|kim> <@{BOSS}> by friday", user=ME, channel="CLAB")
    rows = env.ctx.db.all("SELECT * FROM tasks ORDER BY id")
    assert [r["assignee"] for r in rows] == [KIM, BOSS]
    assert all(r["channel_id"] == "CLAB" and r["title"] == "clean up data" for r in rows)
    assert env.responded[-1]["response_type"] == "in_channel"
    dms = [a["channel"] for a in env.client.of("chat.postMessage")]
    assert "D" + KIM in dms and "D" + BOSS in dms

    # 담당자가 완료하면 요청자에게 알림
    env.client.calls.clear()
    env.action("todo_done", f"{rows[0]['id']}|msg", user=KIM)
    assert any(a["channel"] == "D" + ME and "완료" in a["text"] for a in env.client.of("chat.postMessage"))
    # 남의 일은 완료 못 함
    env.ctx.cfg.admin_ids = []
    ok, _ = todo.complete_task(env.ctx, rows[1]["id"], "USTRANGER")
    assert not ok


def test_todo_modal_and_shortcut(env):
    payload = {"type": "message_action", "callback_id": "todo_from_message", "trigger_id": "trig",
               "user": {"id": ME}, "team": {"id": "T1"}, "channel": {"id": "CLAB"},
               "message": {"ts": "1.2", "text": "내일까지 그림 3 다시 그려주세요"},
               "response_url": "https://hooks.example/r"}
    env.app.dispatch(BoltRequest(body=urlencode({"payload": json.dumps(payload)}), mode="socket_mode"))
    view = _load(env.client.of("views.open")[-1]["view"])
    assert view["callback_id"] == "todo_add_modal"
    meta = view["private_metadata"]

    r = env.submit("todo_add_modal", {
        "title": {"type": "plain_text_input", "value": "그림 3 다시 그리기"},
        "assignee": {"type": "users_select", "selected_user": ME},
        "due_date": {"type": "datepicker", "selected_date": "2026-12-01"},
        "due_time": {"type": "timepicker", "selected_time": None}}, metadata=meta)
    assert r.status == 200
    t = env.ctx.db.one("SELECT * FROM tasks")
    assert t["permalink"] == "https://slack.example/p1" and t["channel_id"] == "CLAB"

    r = env.submit("todo_add_modal", {"title": {"type": "plain_text_input", "value": ""}}, metadata=meta)
    assert "errors" in r.body


def test_todo_reminder_and_snooze(env):
    past = env.ctx.now() + timedelta(minutes=10)  # 시각 있음 → 30분 전 알림 = 이미 지남
    tid = todo.create_task(env.ctx, "약 먹기", ME, ME, past, True)
    assert env.ctx.db.one("SELECT reminded FROM tasks")["reminded"] == 1  # 생성 시 이미 지난 알림
    env.ctx.db.run("UPDATE tasks SET reminded=0, remind_at=? WHERE id=?", (int(time.time()) - 5, tid))
    todo.reminder_job(env.ctx)
    assert any("알림" in a["text"] for a in env.client.of("chat.postMessage"))
    env.action("todo_snooze_60", f"{tid}|60")
    t = env.ctx.db.one("SELECT * FROM tasks")
    assert t["reminded"] == 0 and t["remind_at"] > time.time() + 3000


# ── 근태 ───────────────────────────────────────────────────
def test_clock_in_out(env):
    env.command("/in", "remote writing paper")
    r = env.ctx.db.one("SELECT * FROM attendance")
    assert r["mode"] == "remote" and r["note"] == "writing paper"
    env.command("/in")
    assert "이미" in env.responded[-1]["text"]
    env.ctx.db.run("UPDATE attendance SET in_at=in_at-3600*9")
    env.command("/out")
    assert "퇴근 기록" in env.responded[-1]["text"] and "9시간" in env.responded[-1]["text"]
    posts = [a for a in env.client.of("chat.postMessage") if a["channel"] == "CATT"]
    assert len(posts) == 2
    env.command("/attendance", "week")
    assert "근무 요약" in env.responded[-1]["text"]


def test_pause_breaks_reduce_work_time(env):
    env.command("/pause")
    assert "출근 기록이 없어요" in env.responded[-1]["text"]
    env.command("/in")
    rec = env.ctx.db.one("SELECT * FROM attendance")
    env.ctx.db.run("UPDATE attendance SET in_at=in_at-3600*9")
    env.command("/pause", "점심")
    assert "휴식 시작" in env.responded[-1]["text"]
    b = env.ctx.db.one("SELECT * FROM attendance_breaks")
    assert b["end_at"] is None and b["note"] == "점심"
    assert att.status_of(env.ctx, ME)["break"] is not None
    env.command("/attendance")
    assert "⏸ 휴식" in env.responded[-1]["blocks"][0]["text"]["text"]
    env.ctx.refresh_home(ME)
    assert "att_pause" in json.dumps(_load(env.client.of("views.publish")[-1]["view"]))

    # 1시간 휴식 후 /in 으로 복귀
    env.ctx.db.run("UPDATE attendance_breaks SET start_at=start_at-3600")
    env.command("/in")
    assert "복귀" in env.responded[-1]["text"] and "1시간" in env.responded[-1]["text"]
    env.command("/pause", "end")
    assert "진행 중인 휴식이 없어요" in env.responded[-1]["text"]

    env.command("/out")
    assert "근무 8시간" in env.responded[-1]["text"], env.responded[-1]["text"]  # 9시간 - 휴식 1시간
    s = att.summarize(env.ctx, ME, env.ctx.today(), env.ctx.today())
    assert 8 * 3600 - 120 <= s["secs"] <= 8 * 3600 + 120
    row = att.attendance_rows(env.ctx, env.ctx.today(), env.ctx.today())[0]
    assert row[6] == 1.0 and 7.9 <= row[7] <= 8.1
    posts = [a["text"] for a in env.client.of("chat.postMessage") if a["channel"] == "CATT"]
    assert any(t.startswith("⏸") for t in posts) and any(t.startswith("▶️") for t in posts)


def test_pause_retro_span(env):
    env.command("/in", "08:00")
    if "미래" in env.responded[-1]["text"]:
        pytest.skip("자정~08시 사이에는 과거 출근 시각을 만들 수 없음")
    rec = env.ctx.db.one("SELECT * FROM attendance")
    env.ctx.db.run("UPDATE attendance SET in_at=?", (rec["in_at"] - 1,))
    now = env.ctx.now()
    if now.hour < 9:
        pytest.skip("휴식 범위를 만들 시간이 부족함")
    env.command("/pause", "08:00-08:30 산책")
    assert "기록했어요" in env.responded[-1]["text"] and "30분" in env.responded[-1]["text"]
    env.command("/pause", "08:10-08:20")
    assert "겹쳐요" in env.responded[-1]["text"]
    env.command("/pause", "08:30-08:10")
    assert "시작이 끝보다" in env.responded[-1]["text"]


def test_parse_in_args():
    m, note, at = att.parse_in_args("office 09:10 conference talk")
    assert m == "office" and note == "conference talk" and f"{at:%H:%M}" == "09:10"
    m, note, _ = att.parse_in_args("field conference")  # 외근은 없어지고 출근 + 메모로 기록
    assert m == "office" and note == "field conference"
    assert set(att.MODES) == {"office", "remote"}


def test_field_mode_migrated(tmp_path):
    from app.db import DB
    path = str(tmp_path / "old.db")
    db = DB(path)
    db.run("INSERT INTO attendance(user_id,date,in_at,mode,note) VALUES ('U1','2026-10-01',1,'field','학회')")
    db.run("INSERT INTO attendance(user_id,date,in_at,mode) VALUES ('U1','2026-10-02',2,'field')")
    db.conn.close()
    rows = DB(path).all("SELECT mode, note FROM attendance ORDER BY id")
    assert [(r["mode"], r["note"]) for r in rows] == [("office", "외근 학회"), ("office", "외근")]


def test_leave_flow(env):
    start = date(2026, 10, 6)  # 화
    r = env.submit("leave_modal", {
        "kind": {"type": "static_select", "selected_option": {"value": "annual"}},
        "start": {"type": "datepicker", "selected_date": "2026-10-05"},
        "end": {"type": "datepicker", "selected_date": "2026-10-11"},  # 5일 개천절 대체휴일, 9일 한글날 제외
        "reason": {"type": "plain_text_input", "value": "가족 여행"}})
    assert r.status == 200
    l = env.ctx.db.one("SELECT * FROM leaves")
    assert l["status"] == "pending" and l["days"] == 3.0  # 6,7,8일
    approver_dm = [a for a in env.client.of("chat.postMessage") if a["channel"] == "D" + BOSS]
    assert approver_dm

    # 승인 권한 없는 사람
    ok, msg = att.decide_leave(env.ctx, l["id"], KIM, True)
    assert not ok
    env.action("leave_approve", str(l["id"]), user=BOSS)
    l = env.ctx.db.one("SELECT * FROM leaves")
    assert l["status"] == "approved" and l["approver_id"] == BOSS
    assert env.client.of("chat.update"), "승인자 메시지가 갱신돼야 함"
    assert att.leave_on(env.ctx, ME, start)
    assert att.used_days(env.ctx, ME, 2026) == 3.0

    # 반차는 하루만
    r = env.submit("leave_modal", {
        "kind": {"type": "static_select", "selected_option": {"value": "am"}},
        "start": {"type": "datepicker", "selected_date": "2026-10-12"},
        "end": {"type": "datepicker", "selected_date": "2026-10-13"}})
    assert "errors" in r.body
    # 주말만 고르면 거절
    r = env.submit("leave_modal", {
        "kind": {"type": "static_select", "selected_option": {"value": "annual"}},
        "start": {"type": "datepicker", "selected_date": "2026-10-10"},
        "end": {"type": "datepicker", "selected_date": "2026-10-11"}})
    assert "errors" in r.body


def test_leave_reject_and_cancel(env):
    att.request_leave(env.ctx, ME, "sick", date(2026, 10, 20), date(2026, 10, 20), "")
    lid = env.ctx.db.one("SELECT id FROM leaves")["id"]
    env.submit("leave_reject_modal", {"note": {"type": "plain_text_input", "value": "마감 주간"}},
               user=BOSS, metadata=json.dumps({"id": lid}))
    assert env.ctx.db.one("SELECT status FROM leaves")["status"] == "rejected"
    att.request_leave(env.ctx, ME, "annual", date(2026, 11, 2), date(2026, 11, 2), "")
    lid2 = env.ctx.db.one("SELECT max(id) i FROM leaves")["i"]
    env.action("leave_cancel", str(lid2), container={"type": "message", "is_ephemeral": True})
    assert env.ctx.db.one("SELECT status FROM leaves WHERE id=?", (lid2,))["status"] == "cancelled"


def test_approver_self_request_auto(env):
    ok, msg = att.request_leave(env.ctx, BOSS, "annual", date(2026, 10, 6), date(2026, 10, 6), "")
    assert ok and env.ctx.db.one("SELECT status FROM leaves")["status"] == "approved"


def test_checkin_reminder(env, monkeypatch):
    monkeypatch.setattr(env.ctx, "today", lambda: date(2026, 10, 6))
    monkeypatch.setattr(env.ctx, "now", lambda: datetime(2026, 10, 6, 10, 30, tzinfo=TZ))
    att.request_leave(env.ctx, BOSS, "annual", date(2026, 10, 6), date(2026, 10, 6), "")  # 휴가
    env.ctx.db.run("INSERT INTO attendance(user_id,date,in_at,mode) VALUES (?,?,?,?)",
                   (KIM, "2026-10-06", int(datetime(2026, 10, 6, 9, tzinfo=TZ).timestamp()), "office"))
    env.client.calls.clear()
    att.checkin_reminder_job(env.ctx)
    dms = [a["channel"] for a in env.client.of("chat.postMessage")]
    assert dms == ["D" + ME]  # 휴가자·출근자·봇 제외

    monkeypatch.setattr(env.ctx, "today", lambda: date(2026, 10, 9))  # 한글날
    env.client.calls.clear()
    att.checkin_reminder_job(env.ctx)
    assert not env.client.of("chat.postMessage")


def test_board_and_export(env):
    env.command("/in")
    env.command("/attendance")
    assert "근태 현황" in env.responded[-1]["blocks"][0]["text"]["text"]
    a, l = att.export_csv(env.ctx, env.ctx.today().year, env.ctx.today().month)
    assert a.startswith("﻿날짜") and "나" in a


class FakeWS:
    def __init__(self, title):
        self.title, self.values, self.cleared = title, [], 0

    def freeze(self, rows=None):
        pass

    def clear(self):
        self.values, self.cleared = [], self.cleared + 1

    def resize(self, rows=None, cols=None):
        pass

    def update(self, values=None, range_name=None, value_input_option=None):
        self.values = values


class FakeSheet:
    def __init__(self):
        self.tabs = {}

    def worksheets(self):
        return list(self.tabs.values())

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS(title)
        return self.tabs[title]


def test_attendance_sheet_sync(env, monkeypatch):
    from app import gsheet
    sheet = FakeSheet()
    monkeypatch.setattr(gsheet, "open_sheet", lambda ctx, sid: sheet)
    env.command("/attendance", "sheet", user=BOSS)
    assert "설정되지 않았어요" in env.responded[-1]["text"]
    env.ctx.cfg.attendance_gsheet_id, env.ctx.cfg.google_sa_file = "ATT", "/tmp/sa.json"

    env.command("/in", "remote")
    env.command("/attendance", "sheet")
    assert "관리자" in env.responded[-1]["text"]
    env.command("/attendance", "sheet", user=BOSS)
    assert "동기화했어요" in env.responded[-1]["text"] and "시트 열기" in env.responded[-1]["text"]
    t = env.ctx.today()
    tab = sheet.tabs[f"출퇴근 {t.year}-{t.month:02d}"]
    assert tab.values[0] == att.ATT_HEADER and tab.values[1][1] == "나" and tab.values[1][3] == "재택"
    assert f"휴가 {t.year}" in sheet.tabs

    # 나중에 고친 기록도 다시 쓰면서 반영되고 중복되지 않음
    env.ctx.db.run("UPDATE attendance SET note='고침'")
    att.sheet_job(env.ctx)
    assert len(tab.values) == 2 and tab.values[1][8] == "고침" and tab.cleared == 1


def test_home_publish(env):
    env.command("/todo", "home test")
    env.ctx.refresh_home(ME)
    v = _load(env.client.of("views.publish")[-1]["view"])
    assert v["type"] == "home"
    env.action("att_in_office", "office", container={"type": "view", "view_id": "V"}, view={"type": "home", "id": "V"})
    assert env.ctx.db.one("SELECT * FROM attendance WHERE user_id=?", (ME,))


# ── 서버 ───────────────────────────────────────────────────
def payload(host="gpu1", util=(90, 0), users=("kim", None), mem=(30000, 100), disk_used=50):
    gpus = []
    for i, (u, who, m) in enumerate(zip(util, users, mem)):
        gpus.append({"index": i, "name": "RTX A6000", "util": u, "mem_used": m, "mem_total": 49140, "temp": 60,
                     "procs": [{"pid": 100 + i, "user": who, "mem_mb": m}] if who else []})
    return {"host": host, "interval": 60, "cpu": {"pct": 30, "cores": 64, "load1": 3},
            "mem": {"total_mb": 512000, "used_mb": 100000},
            "disks": [{"mount": "/data", "total_gb": 100, "used_gb": disk_used}], "gpus": gpus}


def test_gpu_status_and_reserve(env):
    env.mon.ingest(payload())
    env.mon.ingest({**payload("nas"), "gpus": [], "disks": [{"mount": "/volume1", "total_gb": 16000, "used_gb": 9000}]})
    assert env.mon.gpu_counts() == (1, 2)
    env.command("/gpu")
    text = json.dumps(env.responded[-1]["blocks"], ensure_ascii=False)
    assert "GPU0" in text and "kim" in text and "사용 가능 1/2" in text
    env.command("/server")
    assert "volume1" in json.dumps(env.responded[-1]["blocks"], ensure_ascii=False)

    env.command("/gpu", "reserve gpu1 1 3h paper experiment")
    assert "예약했어요" in env.responded[-1]["text"]
    assert env.mon.gpu_counts() == (0, 2)
    env.command("/gpu", "reserve gpu1 0-1 1h", user=KIM)
    assert "까지 예약했어요" in env.responded[-1]["text"]  # 충돌
    env.command("/gpu", "reserve gpu9 0 1h")
    assert "찾을 수 없어요" in env.responded[-1]["text"]
    env.command("/gpu", "release")
    assert "1건" in env.responded[-1]["text"]


def test_idle_gpu_alert_and_offline(env):
    env.ctx.db.run("INSERT INTO user_links VALUES ('lee', ?)", (KIM,))
    p = payload(util=(0, 0), users=("lee", None), mem=(20000, 0))
    env.mon.ingest(p)
    for k in env.mon.idle:
        env.mon.idle[k]["since"] -= 3700
    env.client.calls.clear()
    env.mon.watch()
    posts = env.client.of("chat.postMessage")
    assert any("💤" in a["text"] and a["channel"] == "CSRV" for a in posts)
    assert any(a["channel"] == "D" + KIM for a in posts)
    env.client.calls.clear()
    env.mon.watch()
    assert not any("💤" in a["text"] for a in env.client.of("chat.postMessage")), "중복 알림 금지"

    env.mon.latest["gpu1"]["recv"] -= 600
    env.mon.watch()
    assert any("데이터가 오지 않아요" in a["text"] for a in env.client.of("chat.postMessage"))


def test_gpu_forget_renamed_host(env):
    env.mon.ingest(payload("old-name"))
    env.mon.latest["old-name"]["recv"] -= 600
    env.mon.watch()
    env.command("/gpu", "forget old-name")
    assert "관리자만" in env.responded[-1]["text"]
    env.command("/gpu", "forget old-name", user=BOSS)
    assert "지웠어요" in env.responded[-1]["text"]
    assert "old-name" not in env.mon.latest and "old-name" not in env.mon.offline_alerted
    env.command("/gpu", "forget nope", user=BOSS)
    assert "목록에 없어요" in env.responded[-1]["text"]


def test_disk_alert(env):
    env.mon.ingest(payload(disk_used=95))
    env.mon.watch()
    assert any("💾" in a["text"] for a in env.client.of("chat.postMessage"))


def test_daily_summary(env):
    for _ in range(3):
        env.mon.ingest(payload(util=(80, 0), users=("kim", "lee"), mem=(30000, 20000)))
    blocks = env.mon.summary_blocks(env.ctx.today())
    text = json.dumps(blocks, ensure_ascii=False)
    assert "GPU 평균 사용률" in text and "kim" in text and "놀고" not in text.split("kim")[1].split("\\n")[0]
    validate_blocks(blocks)


def _week_fixture(env):
    """지난주 월요일 10시부터 2시간: gpu1 GPU0은 kim이 80% 사용, GPU1은 비어 있음.
    KIM이 GPU 0,1을 4시간 예약했다가 2시간 만에 해제."""
    from app import gpureport
    start, end = gpureport.last_week(env.ctx)
    s = int(datetime.combine(start, datetime.min.time(), TZ).timestamp()) + 10 * 3600
    env.ctx.db.run("INSERT INTO user_links VALUES ('kim', ?)", (KIM,))
    env.ctx.db.run("INSERT INTO gpu_reservations(host,gpus,user_id,start_at,end_at,note,released,released_at)"
                   " VALUES ('gpu1','0,1',?,?,?,'ablation',1,?)", (KIM, s, s + 4 * 3600, s + 2 * 3600))
    g, u = [], []
    for t in range(s, s + 2 * 3600, 60):
        g += [(t, "gpu1", 0, 80, 30000, 49140, 60), (t, "gpu1", 1, 0, 0, 49140, 60)]
        u.append((t, "gpu1", 0, "kim", 30000, 60))
    env.ctx.db.many("INSERT INTO gpu_samples VALUES (?,?,?,?,?,?,?)", g)
    env.ctx.db.many("INSERT INTO gpu_user_samples VALUES (?,?,?,?,?,?)", u)
    return gpureport.Report(env.ctx, env.mon, start, end)


def test_weekly_gpu_report(env):
    rep = _week_fixture(env)
    (r,) = rep.reservations
    assert r["reserved_h"] == pytest.approx(4)            # 해제 시각까지만, GPU 2장 × 2시간
    assert r["busy_pct"] == pytest.approx(50) and r["util"] == pytest.approx(40)
    assert r["own_h"] == pytest.approx(2)
    (u,) = rep.users
    assert u["user"] == "kim" and u["gpu_h"] == pytest.approx(2) and u["util"] == pytest.approx(80)
    blocks = rep.blocks("https://docs.google.com/spreadsheets/d/X")
    validate_blocks(blocks)
    text = json.dumps(blocks, ensure_ascii=False)
    assert "예약 vs 실제 사용" in text and "실제 사용 50%" in text and "본인 프로세스 2.0h" in text
    assert "Google Sheet" in text

    env.command("/gpu", "week sheet")
    assert "관리자만" in env.responded[-1]["text"]


def test_weekly_job_writes_sheet(env, monkeypatch):
    from app import gpureport
    _week_fixture(env)

    class WS:
        def __init__(self, title):
            self.title, self.rows = title, []

        def append_row(self, row):
            self.rows.append(row)

        def append_rows(self, rows, value_input_option=None):
            self.rows += rows

    class Sheet:
        def __init__(self):
            self.tabs = {}

        def worksheets(self):
            return list(self.tabs.values())

        def add_worksheet(self, title, rows, cols):
            self.tabs[title] = WS(title)
            return self.tabs[title]
    sheet = Sheet()
    monkeypatch.setattr(gpureport, "_open_sheet", lambda ctx: sheet)
    env.ctx.cfg.gsheet_id, env.ctx.cfg.google_sa_file = "SHEET", "/tmp/sa.json"
    env.client.calls.clear()
    gpureport.weekly_job(env.ctx, env.mon)
    assert any(a["channel"] == "CSRV" and a["text"] == "주간 GPU 리포트" for a in env.client.of("chat.postMessage"))
    assert set(sheet.tabs) == set(gpureport.SHEET_TABS)
    res = sheet.tabs["예약 내역"].rows
    assert res[0] == gpureport.SHEET_TABS["예약 내역"] and res[1][2] == "김철수" and res[1][13] == "ablation"
    assert len(sheet.tabs["GPU 주간"].rows) == 3                  # 헤더 + GPU 2장
    env.command("/gpu", "week sheet", user=BOSS)
    assert "이미" in env.responded[-1]["text"]
    env.command("/gpu", "week sheet force", user=BOSS)
    assert "행을 기록" in env.responded[-1]["text"] and len(sheet.tabs["GPU 주간"].rows) == 5


def test_metrics_http(env):
    import urllib.request
    import urllib.error
    from app.servers import start_receiver
    env.ctx.cfg.metrics_port = 18787
    env.ctx.cfg.metrics_host = "127.0.0.1"
    srv = start_receiver(env.ctx, env.mon)
    try:
        req = urllib.request.Request("http://127.0.0.1:18787/metrics", data=json.dumps(payload("gpu2")).encode(),
                                     headers={"Authorization": "Bearer secret", "Content-Type": "application/json"})
        assert urllib.request.urlopen(req).status == 200
        assert "gpu2" in env.mon.latest
        bad = urllib.request.Request("http://127.0.0.1:18787/metrics", data=b"{}",
                                     headers={"Authorization": "Bearer nope"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 401
    finally:
        srv.shutdown()


def test_weekday_index():
    from app.timeparse import weekday_index
    assert [weekday_index(x) for x in ["tue", "Thurs", "friday", "sat", "sun", "화", "목요일", "monitor", "x"]] == \
        [1, 3, 4, 5, 6, 1, 3, None, None]


def test_todo_subcommand_guard(env):
    """영어 하위 명령어와 겹치는 할 일 내용은 할 일로 추가해야 함"""
    env.command("/todo", "list of baselines to try")
    env.command("/todo", "done with figure 3? double-check")
    env.command("/todo", "channel pruning experiment fri")
    titles = [r["title"] for r in env.ctx.db.all("SELECT title FROM tasks ORDER BY id")]
    assert titles == ["list of baselines to try", "done with figure 3? double-check", "channel pruning experiment"]
    env.command("/todo", "done 1 2")
    assert env.ctx.db.one("SELECT COUNT(*) c FROM tasks WHERE status='done'")["c"] == 2
    env.command("/todo", "delete #3")
    assert env.ctx.db.one("SELECT COUNT(*) c FROM tasks")["c"] == 2
    env.command("/todo", "help")
    assert "/todo done 12" in env.responded[-1]["text"]


def test_english_commands_everywhere(env):
    env.command("/todo", "sent")
    assert "내가 맡긴" in env.responded[-1]["text"]
    env.command("/todo", "channel")
    assert "채널의 할 일" in env.responded[-1]["text"]
    env.command("/vacation", "list")
    assert env.responded[-1]["text"] == "내 휴가"
    env.command("/vacation", "team")
    assert env.responded[-1]["text"] == "팀 휴가"
    env.command("/vacation", "nonsense")
    assert "/vacation list" in env.responded[-1]["text"]
    env.command("/attendance", "today")
    assert env.responded[-1]["text"] == "오늘 근태"
    env.command("/attendance", "month")
    assert env.responded[-1]["text"] == "근무 요약"
    env.command("/in", "office conference")
    assert env.ctx.db.one("SELECT mode FROM attendance")["mode"] == "office"
    env.mon.ingest({"host": "gpu1", "interval": 60, "gpus": [
        {"index": 0, "name": "A6000", "util": 0, "mem_used": 0, "mem_total": 49140, "procs": []}]})
    for sub, expect in [("status", "GPU 현황"), ("free", "사용 가능한 GPU"), ("summary", "사용 요약"),
                        ("today", "오늘 사용 요약"), ("week", "주간 GPU 리포트"),
                        ("week now", "이번 주 GPU 리포트")]:
        env.command("/gpu", sub)
        assert env.responded[-1]["text"] == expect, sub
    env.command("/gpu", "link kim")
    assert "연결했어요" in env.responded[-1]["text"]
    env.command("/gpu", "reserve gpu1 0 2h")
    env.command("/gpu", "mine")
    assert "gpu1 GPU 0" in env.responded[-1]["text"]
    env.command("/gpu", "help")
    assert "/gpu reserve" in env.responded[-1]["text"]
    env.command("/server", "help")
    assert "/gpu link" in env.responded[-1]["text"]


def test_korean_aliases_still_work(env):
    """이미 쓰던 한국어 하위 명령어도 계속 동작"""
    env.command("/todo", "논문 리뷰 내일")
    env.command("/todo", "완료 1")
    assert env.ctx.db.one("SELECT status FROM tasks")["status"] == "done"
    env.command("/in", "재택")
    assert env.ctx.db.one("SELECT mode FROM attendance")["mode"] == "remote"
    env.command("/attendance", "주간")
    assert env.responded[-1]["text"] == "근무 요약"
    env.command("/vacation", "내역")
    assert env.responded[-1]["text"] == "내 휴가"
