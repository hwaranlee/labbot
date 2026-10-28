"""미팅 action items · 논문 다이제스트 테스트 (Slack·arXiv·HF·Claude API는 모두 가짜)."""
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import meetings, papers, todo
from tests.test_labbot import BOSS, KIM, ME, _load, env, validate_blocks  # noqa: F401

TZ = ZoneInfo("Asia/Seoul")


class FakeLLM:
    available = True

    def __init__(self, structured=None, text="*한 줄 요약*\n테스트 요약"):
        self._structured = structured or {}
        self._text = text
        self.calls = []

    def structured(self, system, prompt, name, description, schema, max_tokens=4000):
        self.calls.append((name, prompt, schema))
        out = self._structured[name]
        return out(schema) if callable(out) else out

    def text(self, system, prompt, max_tokens=1500):
        self.calls.append(("text", prompt, None))
        return self._text


# ── 미팅 ───────────────────────────────────────────────────
NOTES = """오늘은 논문 방향 논의함. 실험 결과는 괜찮은 편.
- [ ] 김철수: baseline 3개 추가 실험 금요일까지
TODO 관련 연구 survey 정리
→ 교수님 리뷰 코멘트 전달 10/7
그냥 메모 줄"""


def test_rule_extract(env):
    r = meetings.rule_extract(env.ctx, "group", ME, [ME, KIM, BOSS], date(2026, 10, 1), NOTES)
    items = r["items"]
    assert [i["assignee"] for i in items] == [KIM, ME, BOSS]
    assert items[0]["task"] == "baseline 3개 추가 실험" and items[0]["due"] == "2026-10-02"
    assert items[1]["task"] == "관련 연구 survey 정리" and items[1]["due"] == ""
    assert items[2]["task"] == "리뷰 코멘트 전달" and items[2]["due"] == "2026-10-07"
    # 개인 미팅에서 담당자가 없으면 학생에게
    r = meetings.rule_extract(env.ctx, "one_on_one", BOSS, [BOSS, KIM], date(2026, 10, 1), "TODO 데이터 정리")
    assert r["items"][0]["assignee"] == KIM


def test_llm_extract_sanitizes(env):
    env.ctx.llm = FakeLLM({"save_meeting_notes": {
        "summary": ["방향 합의", ""], "decisions": ["CVPR 투고"],
        "action_items": [
            {"assignee_id": KIM, "task": "ablation 실험", "due_date": "2026-10-09", "due_time": "14:00"},
            {"assignee_id": "UNKNOWN", "task": "x", "due_date": "", "due_time": ""},
            {"assignee_id": ME, "task": "초록 쓰기", "due_date": "다음주", "due_time": "아무때나"}],
        "next_meeting": "다음 주 목요일"}})
    r = meetings.extract(env.ctx, "개인 미팅", "one_on_one", BOSS, [BOSS, KIM, ME], date(2026, 10, 1), NOTES)
    assert r["by"] == "llm" and r["summary"] == ["방향 합의"]
    assert r["items"] == [{"assignee": KIM, "task": "ablation 실험", "due": "2026-10-09", "time": "14:00"},
                          {"assignee": ME, "task": "초록 쓰기", "due": "", "time": ""}]
    name, prompt, schema = env.ctx.llm.calls[0]
    assert "2026-10-01 (목요일)" in prompt and schema["properties"]["action_items"]["items"]["properties"][
        "assignee_id"]["enum"] == [BOSS, KIM, ME]


def _submit_meeting(env, notes=NOTES, participants=(KIM, BOSS), kind="group", meta=None):
    return env.submit("meeting_modal", {
        "title": {"type": "plain_text_input", "value": "주간 미팅"},
        "kind": {"type": "static_select", "selected_option": {"value": kind}},
        "participants": {"type": "multi_users_select", "selected_users": list(participants)},
        "date": {"type": "datepicker", "selected_date": "2026-10-01"},
        "notes": {"type": "plain_text_input", "value": notes},
        "share": {"type": "radio_buttons", "selected_option": {"value": "channel"}}},
        metadata=json.dumps(meta or {"recurring_id": None}))


def test_meeting_flow(env):
    env.ctx.cfg.meeting_channel = "CMTG"
    env.command("/meeting")
    view = _load(env.client.of("views.open")[-1]["view"])
    assert view["callback_id"] == "meeting_modal"

    assert "errors" in env.submit("meeting_modal", {"notes": {"type": "plain_text_input", "value": ""},
                                                    "participants": {"type": "multi_users_select",
                                                                     "selected_users": []}}).body
    r = _submit_meeting(env)
    assert r.status == 200
    m = env.ctx.db.one("SELECT * FROM meetings")
    assert m["status"] == "draft" and m["participants"].split(",") == [ME, KIM, BOSS] and m["share"] == "channel"
    draft = [a for a in env.client.of("chat.postMessage") if a["channel"] == "D" + ME][-1]
    assert "Action items (3)" in json.dumps(_load(draft["blocks"]), ensure_ascii=False)

    env.client.calls.clear()
    env.action("meeting_confirm", str(m["id"]))
    rows = env.ctx.db.all("SELECT * FROM tasks WHERE meeting_id=?", (m["id"],))
    assert len(rows) == 3 and {r["assignee"] for r in rows} == {KIM, ME, BOSS}
    dm_targets = {a["channel"] for a in env.client.of("chat.postMessage")}
    assert {"D" + ME, "D" + KIM, "D" + BOSS, "CMTG"} <= dm_targets
    channel_post = [a for a in env.client.of("chat.postMessage") if a["channel"] == "CMTG"][0]
    assert "todo_done" not in json.dumps(_load(channel_post["blocks"])), "채널 게시물에는 버튼이 없어야 함"

    # 두 번 등록 방지
    env.action("meeting_confirm", str(m["id"]))
    assert len(env.ctx.db.all("SELECT * FROM tasks")) == 3

    # 미팅 DM에서 완료 → 같은 미팅 기록으로 다시 그림
    kim_task = [r for r in rows if r["assignee"] == KIM][0]
    env.action("todo_done", f"{kim_task['id']}|mtg:{m['id']}", user=KIM)
    out = env.responded[-1]
    assert out["replace_original"] and "주간 미팅" in json.dumps(out["blocks"], ensure_ascii=False)

    env.command("/meeting", "list")
    assert "주간 미팅" in env.responded[-1]["text"]
    env.command("/meeting", f"view {m['id']}", user="USTRANGER")
    assert "주간 미팅" in json.dumps(env.responded[-1], ensure_ascii=False)  # 채널 공유 미팅은 볼 수 있음


def test_meeting_edit_and_discard(env):
    _submit_meeting(env)
    m = env.ctx.db.one("SELECT * FROM meetings")
    meta = json.dumps({"id": m["id"], "channel": "D" + ME, "ts": "1.1"})
    r = env.submit("meeting_edit_modal", {"items": {"type": "plain_text_input", "value": "없는사람 | 실험"},
                                          "summary": {"type": "plain_text_input", "value": ""}}, metadata=meta)
    assert "찾을 수 없어요" in json.loads(r.body)["errors"]["items"]
    r = env.submit("meeting_edit_modal", {"items": {"type": "plain_text_input", "value": "김철수 | 실험 | 이상한날"},
                                          "summary": {"type": "plain_text_input", "value": ""}}, metadata=meta)
    assert "이해하지 못했어요" in json.loads(r.body)["errors"]["items"]
    r = env.submit("meeting_edit_modal", {
        "items": {"type": "plain_text_input", "value": "김철수 | ablation 실험 | fri 14:00\n교수님 | 초안 리뷰"},
        "summary": {"type": "plain_text_input", "value": "• 방향 합의\n• 마감 확인"}}, metadata=meta)
    assert r.status == 200 and "errors" not in (r.body or "")
    m = env.ctx.db.one("SELECT * FROM meetings")
    items = json.loads(m["items"])
    assert items == [{"assignee": KIM, "task": "ablation 실험", "due": "2026-10-02", "time": "14:00"},
                     {"assignee": BOSS, "task": "초안 리뷰", "due": "", "time": ""}]
    assert m["summary"] == "방향 합의\n마감 확인"
    assert env.client.of("chat.update")
    env.action("meeting_discard", str(m["id"]))
    assert env.ctx.db.one("SELECT status FROM meetings")["status"] == "discarded"


def test_meeting_from_thread(env):
    orig = env.client.fake_call

    def fake(api_method, **kw):
        if api_method == "conversations.replies":
            from slack_sdk.web import SlackResponse
            msgs = [{"user": ME, "ts": "1", "text": "회의록 공유"}, {"user": KIM, "ts": "2", "text": "TODO 표 정리"},
                    {"bot_id": "B1", "ts": "3", "text": "봇 메시지"}]
            return SlackResponse(client=env.client, http_verb="POST", api_url=api_method, req_args={},
                                 data={"ok": True, "messages": msgs}, headers={}, status_code=200)
        return orig(api_method, **kw)
    env.client.fake_call = fake
    payload = {"type": "message_action", "callback_id": "meeting_from_message", "trigger_id": "t",
               "user": {"id": BOSS}, "team": {"id": "T1"}, "channel": {"id": "CLAB"},
               "message": {"ts": "1", "text": "회의록 공유", "reply_count": 2}, "response_url": "https://hooks.example/r"}
    from urllib.parse import urlencode
    from slack_bolt.request import BoltRequest
    env.app.dispatch(BoltRequest(body=urlencode({"payload": json.dumps(payload)}), mode="socket_mode"))
    view = _load(env.client.of("views.open")[-1]["view"])
    blocks = {b["block_id"]: b for b in view["blocks"]}
    assert "김철수: TODO 표 정리" in blocks["notes"]["element"]["initial_value"]
    assert blocks["participants"]["element"]["initial_users"] == [BOSS, ME, KIM]
    assert blocks["kind"]["element"]["initial_option"]["value"] == "group"


def test_recurring_prompt(env, monkeypatch):
    env.command("/meeting", f"recurring <@{KIM}|kim> thu 14:00 60 1:1 research meeting")
    assert "등록" in env.responded[-1]["text"]
    r = env.ctx.db.one("SELECT * FROM recurring_meetings")
    assert (r["weekday"], r["start"], r["duration_min"], r["title"]) == (3, "14:00", 60, "1:1 research meeting")

    thursday = datetime(2026, 10, 8, 15, 30, tzinfo=TZ)
    monkeypatch.setattr(env.ctx, "now", lambda: thursday)
    monkeypatch.setattr(env.ctx, "today", lambda: thursday.date())
    env.client.calls.clear()
    meetings.recurring_prompt_job(env.ctx)
    assert {a["channel"] for a in env.client.of("chat.postMessage")} == {"D" + ME, "D" + KIM}
    env.client.calls.clear()
    meetings.recurring_prompt_job(env.ctx)
    assert not env.client.of("chat.postMessage"), "같은 날 중복 알림 금지"

    env.action("meeting_open", str(r["id"]), user=KIM)
    view = _load(env.client.of("views.open")[-1]["view"])
    assert json.loads(view["private_metadata"])["recurring_id"] == r["id"]
    _submit_meeting(env, participants=(KIM,), kind="one_on_one", meta={"recurring_id": r["id"]})
    env.ctx.db.run("UPDATE meetings SET meeting_date='2026-10-08'")
    env.action("meeting_open", str(r["id"]), user=KIM)
    assert "이미" in env.responded[-1]["text"]


def test_weekly_review(env):
    env.ctx.cfg.meeting_channel = "CMTG"
    _submit_meeting(env)
    m = env.ctx.db.one("SELECT * FROM meetings")
    env.ctx.db.run("UPDATE meetings SET meeting_date=?", (env.ctx.today().isoformat(),))
    env.action("meeting_confirm", str(m["id"]))
    t = env.ctx.db.one("SELECT * FROM tasks WHERE assignee=?", (KIM,))
    todo.complete_task(env.ctx, t["id"], KIM)
    env.client.calls.clear()
    meetings.weekly_review_job(env.ctx)
    posts = env.client.of("chat.postMessage")
    assert {"D" + ME, "D" + KIM, "D" + BOSS} <= {a["channel"] for a in posts}
    ch = [a for a in posts if a["channel"] == "CMTG"][0]
    txt = json.dumps(_load(ch["blocks"]), ensure_ascii=False)
    assert "김철수 · 미팅 1 · 완료 1 · 남음 0" in txt and "나 · 미팅 1 · 완료 0 · 남음 1" in txt

    # 주간 정리 DM의 완료 버튼 → 주간 정리를 다시 그림
    mine = env.ctx.db.one("SELECT * FROM tasks WHERE assignee=? AND status='open'", (ME,))
    env.action("todo_done", f"{mine['id']}|weekly")
    assert "이번 주 정리" in env.responded[-1]["text"]


# ── 논문 ───────────────────────────────────────────────────
def _today_iso(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def arxiv_xml(entries):
    items = "".join(f"""
  <entry>
    <id>http://arxiv.org/abs/{pid}v2</id>
    <published>{pub}</published>
    <title>{title}</title>
    <summary>  {abstract}  </summary>
    <author><name>Alice Kim</name></author><author><name>Bob Lee</name></author>
    <author><name>C</name></author><author><name>D</name></author>
    <arxiv:primary_category term="cs.CV"/>
  </entry>""" for pid, pub, title, abstract in entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">{items}
</feed>""".encode()


@pytest.fixture()
def fake_sources(monkeypatch):
    papers.ARXIV_DELAY = 0
    feed = arxiv_xml([
        ("2609.00001", _today_iso(0), "Fast Video Diffusion   Models", "We propose a video diffusion model. It is fast."),
        ("2609.00002", _today_iso(1), "Diffusion for Robots | Policies", "Diffusion policy for robot control."),
        ("2609.00003", _today_iso(20), "Old Diffusion Paper", "Too old."),
    ])
    hf = json.dumps([
        {"paper": {"id": "2609.00001", "title": "Fast Video Diffusion Models", "summary": "x", "upvotes": 42,
                   "authors": [{"name": "Alice Kim"}], "publishedAt": _today_iso(0)}},
        {"paper": {"id": "2609.00009", "title": "Scaling Video Diffusion Transformers", "summary": "video diffusion",
                   "upvotes": 7, "authors": [{"name": "Z"}], "publishedAt": _today_iso(0)}},
        {"paper": {"id": "2609.00010", "title": "Unrelated LLM Paper", "summary": "language", "upvotes": 99,
                   "authors": [], "publishedAt": _today_iso(0)}},
    ]).encode()
    urls = []

    def fake_get(url, timeout=30):
        urls.append(url)
        if "export.arxiv.org" in url:
            return feed
        return hf if "date=" in url and url.endswith(datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()) else b"[]"
    monkeypatch.setattr(papers, "http_get", fake_get)
    return urls


def test_query_helpers():
    assert papers.split_topic("diffusion model, video cat:cs.CV,cs.LG") == ("diffusion model, video", "cs.CV,cs.LG")
    q = papers.build_query("diffusion model, video", "cs.CV")
    assert q == '((ti:"diffusion model" OR abs:"diffusion model") OR (ti:"video" OR abs:"video")) AND (cat:cs.CV)'


def test_collect_and_rule_digest(env, fake_sources):
    env.ctx.cfg.paper_channel = "CPAPER"
    env.command("/papers", "add video diffusion cat:cs.CV")
    assert "추가" in env.responded[-1]["text"]
    t = env.ctx.db.one("SELECT * FROM paper_topics")
    assert (t["owner"], t["query"], t["categories"]) == ("lab", "video diffusion", "cs.CV")

    picks = papers.run_digest(env.ctx, "lab", 3, "📚 오늘의 논문")
    assert any("cat%3Acs.CV" in u for u in fake_sources)
    ids = [p["id"] for p in picks]
    assert ids[0] == "2609.00001"  # 제목 일치 + HF 42 upvotes
    assert "2609.00009" in ids and "2609.00003" not in ids and "2609.00010" not in ids
    post = [a for a in env.client.of("chat.postMessage") if a["channel"] == "CPAPER"][-1]
    blocks = _load(post["blocks"])
    validate_blocks(blocks)
    txt = json.dumps(blocks, ensure_ascii=False)
    assert "🔥 42" in txt and "Alice Kim, Bob Lee, C 외 1명" in txt and "ANTHROPIC_API_KEY" in txt

    # 이미 소개한 논문은 다시 보내지 않음
    env.client.calls.clear()
    assert all(p["id"] not in ids for p in papers.run_digest(env.ctx, "lab", 3, "x"))


def test_llm_select_and_weekly(env, fake_sources):
    env.ctx.cfg.paper_channel = "CPAPER"
    env.ctx.db.set_setting("lab_profile", "비디오 생성 모델 연구실")
    env.ctx.llm = FakeLLM({"pick_papers": lambda schema: {"papers": [
        {"id": "2609.00002", "relevance": 9, "summary": "로봇 정책을 diffusion으로 학습 <중요>", "why": "제어 응용"},
        {"id": "2609.00001", "relevance": 7, "summary": "빠른 비디오 diffusion", "why": "핵심 주제"},
        {"id": "2609.00009", "relevance": 3, "summary": "낮음", "why": ""},
        {"id": "2609.00002", "relevance": 9, "summary": "중복", "why": ""}]}})
    env.ctx.db.run("INSERT INTO paper_topics(owner, query, created_at) VALUES ('lab', 'video diffusion', 0)")
    picks = papers.run_digest(env.ctx, "lab", 3, "📚 오늘의 논문")
    assert [p["id"] for p in picks] == ["2609.00002", "2609.00001"]  # 관련도 6 미만·중복 제외
    _, prompt, schema = env.ctx.llm.calls[0]
    assert "비디오 생성 모델 연구실" in prompt and "upvotes 42" in prompt
    assert set(schema["properties"]["papers"]["items"]["properties"]["id"]["enum"]) >= {"2609.00001", "2609.00002"}
    post = _load([a for a in env.client.of("chat.postMessage") if a["channel"] == "CPAPER"][-1]["blocks"])
    txt = json.dumps(post, ensure_ascii=False)
    assert "관련도 9/10" in txt and "&lt;중요&gt;" in txt and "Diffusion for Robots ∣ Policies" in txt

    env.client.calls.clear()
    papers.weekly_job(env.ctx)
    weekly = [a for a in env.client.of("chat.postMessage") if a["channel"] == "CPAPER"]
    assert weekly and "이번 주 중요 논문" in weekly[0]["text"]
    assert "Diffusion for Robots" in json.dumps(_load(weekly[0]["blocks"]), ensure_ascii=False)


def test_personal_topics_dm(env, fake_sources):
    env.command("/papers", "add-mine video diffusion", user=KIM)
    env.ctx.cfg.paper_channel = ""
    monkey_today = env.ctx.today()
    if not env.ctx.is_workday(monkey_today):
        pytest.skip("휴일에는 매일 다이제스트가 쉬어요")
    papers.daily_job(env.ctx)
    assert any(a["channel"] == "D" + KIM for a in env.client.of("chat.postMessage"))


def test_bookmark_and_detail(env, fake_sources):
    env.ctx.cfg.paper_channel = "CPAPER"
    env.ctx.db.run("INSERT INTO paper_topics(owner, query, created_at) VALUES ('lab', 'video diffusion', 0)")
    papers.run_digest(env.ctx, "lab", 3, "x")
    env.action("paper_bookmark", "2609.00001", channel="CPAPER")
    t = env.ctx.db.one("SELECT * FROM tasks")
    assert t["title"].startswith("📄 논문 읽기: Fast Video Diffusion") and t["permalink"].endswith("2609.00001")
    env.action("paper_bookmark", "2609.00001", channel="CPAPER")
    assert "이미" in env.responded[-1]["text"]

    env.ctx.llm = FakeLLM(text="*한 줄 요약*\n빠른 비디오 생성")
    env.client.calls.clear()
    env.action("paper_detail", "2609.00001", channel="CPAPER")
    replies = [a for a in env.client.of("chat.postMessage") if a.get("thread_ts")]
    assert len(replies) == 1 and "빠른 비디오 생성" in replies[0]["text"]
    env.action("paper_detail", "2609.00001", channel="CPAPER")
    assert len([a for a in env.client.of("chat.postMessage") if a.get("thread_ts")]) == 1
    assert "이미" in env.responded[-1]["text"]
    assert len([c for c in env.ctx.llm.calls if c[0] == "text"]) == 1, "자세한 요약은 캐시"


def test_papers_commands(env, fake_sources):
    env.command("/papers", "profile 비디오 생성 연구", user=KIM)
    assert "관리자" in env.responded[-1]["text"]
    env.command("/papers", "profile 비디오 생성 연구", user=BOSS)
    assert env.ctx.db.get_setting("lab_profile") == "비디오 생성 연구"
    env.command("/papers", "search video diffusion")
    assert "검색 결과" in env.responded[-1]["text"]
    env.command("/papers", "summary https://arxiv.org/abs/2609.00002v1")
    assert "Diffusion for Robots" in env.responded[-1]["text"]
    env.command("/papers", "add robot policy")
    tid = env.ctx.db.one("SELECT max(id) i FROM paper_topics")["i"]
    env.command("/papers", f"delete {tid}", user=KIM)
    assert "삭제할 수" in env.responded[-1]["text"]
    env.command("/papers", f"delete {tid}")
    assert "삭제했어요" in env.responded[-1]["text"]
    env.command("/papers")
    validate_blocks(env.responded[-1]["blocks"])


def test_meeting_text_vs_subcommand(env):
    """'/meeting weekly sync notes...' 처럼 하위 명령어로 시작하는 회의록은 회의록으로 받음"""
    env.command("/meeting", "weekly")
    assert "이번 주 정리" in env.responded[-1]["text"]
    env.command("/meeting", "list of things we discussed: TODO rerun eval")
    view = _load(env.client.of("views.open")[-1]["view"])
    notes = {b["block_id"]: b for b in view["blocks"]}["notes"]["element"]["initial_value"]
    assert notes == "list of things we discussed: TODO rerun eval"
    env.command("/meeting", "new kickoff notes")
    view = _load(env.client.of("views.open")[-1]["view"])
    assert {b["block_id"]: b for b in view["blocks"]}["notes"]["element"]["initial_value"] == "kickoff notes"
    env.command("/meeting", "recurring")
    assert env.responded[-1]["text"] == "정기 미팅"
    env.command("/meeting", f"recurring <@{KIM}> saturday 10:00")
    assert "매주 토" in env.responded[-1]["text"]
    env.command("/meeting", "help")
    assert "/meeting recurring" in env.responded[-1]["text"]


def test_rule_extract_english_notes(env):
    notes = "Discussed results.\n- 김철수: rerun baselines by friday\nAction: write intro next tue\nNice weather"
    r = meetings.rule_extract(env.ctx, "group", ME, [ME, KIM], date(2026, 10, 1), notes)
    assert [(i["assignee"], i["task"], i["due"]) for i in r["items"]] == [
        (KIM, "rerun baselines", "2026-10-02"), (ME, "write intro", "2026-10-06")]


def test_find_person_english_names(env, monkeypatch):
    names = {KIM: "Li", ME: "Hwaran"}
    orig = env.ctx.user
    monkeypatch.setattr(env.ctx, "user", lambda uid: {**orig(uid), "profile": {"display_name": names.get(uid, uid)}})
    assert meetings.find_person(env.ctx, "rerun baseline experiments", [KIM, ME])[0] is None
    who, rest = meetings.find_person(env.ctx, "li: rerun baselines", [KIM, ME])
    assert who == KIM and rest == "rerun baselines"
    assert meetings.find_person(env.ctx, "@HWARAN write intro", [KIM, ME])[0] == ME
