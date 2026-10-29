"""학회 데드라인 테스트 (Slack은 가짜, 시각은 고정)."""
import json
from datetime import date, datetime, timedelta

import pytest
import yaml

from app import deadlines as D
from app import meetings
from app.ctx import Ctx
from app.db import DB
from tests.test_labbot import BOSS, KIM, ME, TZ, _load, env  # noqa: F401

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)  # 목요일
OTHER = "U0OTHER"


@pytest.fixture()
def dl(env, monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(env.ctx, "now", lambda: clock["now"])
    monkeypatch.setattr(env.ctx, "today", lambda: clock["now"].date())
    env.clock = clock
    return env


def _dms(e, uid):
    return [a for a in e.client.of("chat.postMessage") if a["channel"] == "D" + uid]


def _text(msg) -> str:
    return msg["text"] + json.dumps(_load(msg.get("blocks") or []), ensure_ascii=False)


def _conf(e, name):
    return D.find_conf(e.ctx, name)[0]


def _conf_values(name, group="ML", url="", rt="direct", tz="AoE", deadlines="", commit=()):
    v = {"name": {"type": "plain_text_input", "value": name},
         "group": {"type": "plain_text_input", "value": group},
         "url": {"type": "plain_text_input", "value": url},
         "review_type": {"type": "static_select", "selected_option": {"value": rt}},
         "tz": {"type": "plain_text_input", "value": tz},
         "deadlines": {"type": "plain_text_input", "value": deadlines}}
    if commit:
        v["commit_to"] = {"type": "multi_static_select", "selected_options": [{"value": str(c)} for c in commit]}
    return v


# ── 시간대 ─────────────────────────────────────────────────
@pytest.mark.parametrize("local,tz,expect", [
    (datetime(2026, 10, 1, 23, 59), "AoE", "10/2(금) 20:59 KST (AoE 10/1 23:59)"),     # 날짜 넘어감
    (datetime(2026, 10, 1, 23, 59), "UTC-7", "10/2(금) 15:59 KST (UTC-7 10/1 23:59)"),  # 날짜 넘어감
    (datetime(2026, 10, 1, 8, 0), "UTC-7", "10/2(금) 00:00 KST (UTC-7 10/1 08:00)"),    # 자정 경계
    (datetime(2026, 10, 1, 7, 59), "UTC-7", "10/1(목) 23:59 KST (UTC-7 10/1 07:59)"),   # 안 넘어감
    (datetime(2026, 12, 31, 23, 59), "AoE", "1/1(금) 20:59 KST (AoE 12/31 23:59)"),     # 연도 넘어감
    (datetime(2026, 10, 1, 20, 0), "KST", "10/1(목) 20:00 KST"),
    (datetime(2026, 10, 1, 12, 0), "UTC", "10/1(목) 21:00 KST (UTC 10/1 12:00)"),
])
def test_tz_to_kst(dl, local, tz, expect):
    assert D.when_text(dl.ctx, D.to_epoch(local, D.norm_tz(tz)), D.norm_tz(tz)) == expect


def test_norm_tz():
    assert [D.norm_tz(x) for x in ["aoe", "utc-07:00", "GMT+5:30", "kst", "America/Los_Angeles", "Mars/Base"]] == \
        ["AoE", "UTC-7", "UTC+5:30", "KST", "America/Los_Angeles", None]


def test_deadline_line_format(dl):
    iclr = _conf(dl, "ICLR")
    D.set_deadline(dl.ctx, iclr, 2027, "paper", datetime(2026, 11, 4, 23, 59), "AoE", BOSS)
    d = dl.ctx.db.one("SELECT * FROM conf_deadlines WHERE conf_id=? AND year=2027 AND kind='paper'", (iclr["id"],))
    assert D.deadline_line(dl.ctx, iclr, d) == "ICLR 2027 논문 마감 · 11/5(목) 20:59 KST (AoE 11/4 23:59) · *D-7*"


# ── yaml 초기 목록 ─────────────────────────────────────────
def test_seed_from_yaml(dl):
    names = {c["name"] for c in D.all_confs(dl.ctx)}
    assert {"ACL", "EMNLP", "NAACL", "EACL", "AACL", "NeurIPS", "ICLR", "ICML", "Interspeech", "ICASSP"} <= names
    naacl = _conf(dl, "naacl")
    assert naacl["review_type"] == "arr" and naacl["created_by"] == "yaml"
    assert [c["name"] for c in D.linked(dl.ctx, naacl)] == ["ARR 2026 Oct"]
    assert [c["name"] for c in D.linked(dl.ctx, _conf(dl, "ACL"))] == ["ARR 2027 Jan"]


def test_yaml_read_only_on_first_run(dl, tmp_path):
    """재시작해도 Slack에서 고친 내용이 유지되고, yaml은 다시 읽지 않음"""
    D.quick_add(dl.ctx, ME, "KDD group:DM url:https://kdd.org")
    D.cmd_set(dl.ctx, BOSS, "ICLR 2028 paper 2027-09-20 23:59")
    iclr = _conf(dl, "ICLR")
    D.update_conf(dl.ctx, iclr["id"], "ICLR", "Deep Learning", "https://iclr.cc", "direct", "AoE")
    rm = _conf(dl, "ICML")
    D.delete_conf(dl.ctx, rm["id"])

    seed = tmp_path / "other.yaml"
    seed.write_text(yaml.safe_dump({"conferences": [{"name": "ICML", "group": "ML"}, {"name": "CVPR"}]}))
    ctx2 = Ctx(dl.ctx.cfg, DB(dl.ctx.cfg.db_path), dl.client)  # 재시작
    assert D.seed(ctx2, str(seed)) is False
    names = {c["name"] for c in D.all_confs(ctx2)}
    assert "KDD" in names and "ICML" not in names and "CVPR" not in names
    assert D.find_conf(ctx2, "iclr")[0]["grp"] == "Deep Learning"
    assert any(d["year"] == 2028 for d in D.conf_deadlines(ctx2, iclr["id"]))

    # 빈 DB에서는 처음 한 번만 읽음
    fresh = Ctx(dl.ctx.cfg, DB(str(tmp_path / "new.db")), dl.client)
    assert D.seed(fresh, str(seed)) is True
    assert {c["name"] for c in D.all_confs(fresh)} == {"ICML", "CVPR"}
    assert D.seed(fresh, str(seed)) is False


def test_export_round_trip(dl, tmp_path):
    D.quick_add(dl.ctx, ME, "KDD group:DM url:<https://kdd.org> tz:UTC-7")
    D.cmd_set(dl.ctx, ME, "KDD 2027 paper 2027-02-01 23:59 PT")
    text = D.export_yaml(dl.ctx)
    data = yaml.safe_load(text)
    kdd = next(c for c in data["conferences"] if c["name"] == "KDD")
    assert kdd["url"] == "https://kdd.org" and kdd["timezone"] == "UTC-7"
    assert kdd["deadlines"] == [{"year": 2027, "kind": "paper", "at": "2027-02-01 23:59", "timezone": "PT"}]
    ctx2 = Ctx(dl.ctx.cfg, DB(str(tmp_path / "copy.db")), dl.client)
    path = tmp_path / "export.yaml"
    path.write_text(text)
    D.seed(ctx2, str(path))
    assert D.export_data(ctx2) == D.export_data(dl.ctx)

    dl.command("/deadline", "conf export")
    assert "DM" in dl.responded[-1]["text"]  # 가짜 클라이언트에서는 업로드 실패 → DM에 내용


# ── 학회 추가·수정·삭제와 권한 ────────────────────────────
def test_conf_add_modal_and_quick(dl):
    dl.command("/deadline", "conf add")
    view = _load(dl.client.of("views.open")[-1]["view"])
    assert view["callback_id"] == "deadline_conf_modal"
    assert [b["block_id"] for b in view["blocks"]][:5] == ["name", "group", "url", "review_type", "tz"]

    r = dl.submit("deadline_conf_modal", _conf_values("iclr"), metadata=json.dumps({"id": None}))
    assert "이미" in json.loads(r.body)["errors"]["name"]
    r = dl.submit("deadline_conf_modal", _conf_values("KDD", tz="Mars"), metadata=json.dumps({"id": None}))
    assert "시간대" in json.loads(r.body)["errors"]["tz"]
    r = dl.submit("deadline_conf_modal", _conf_values("KDD", deadlines="2027 paper 2027-02-01"),
                  user=KIM, metadata=json.dumps({"id": None}))
    assert r.status == 200 and not r.body
    kdd = _conf(dl, "kdd")
    assert kdd["created_by"] == KIM and kdd["grp"] == "ML" and len(D.conf_deadlines(dl.ctx, kdd["id"])) == 1

    dl.command("/deadline", "conf add WWW group:Web url:https://www2027.org tz:UTC-7", user=OTHER)
    assert "추가했어요" in dl.responded[-1]["text"]
    www = _conf(dl, "www")
    assert (www["grp"], www["official_url"], www["default_tz"], www["created_by"]) == \
        ("Web", "https://www2027.org", "UTC-7", OTHER)
    dl.command("/deadline", "conf add www")
    assert "이미" in dl.responded[-1]["text"]


@pytest.mark.parametrize("who,allowed", [(KIM, True), (BOSS, True), (OTHER, False)])
def test_conf_permissions(dl, who, allowed):
    """추가한 사람(KIM)과 관리자(BOSS)만 수정·삭제·watch·set 가능"""
    D.quick_add(dl.ctx, KIM, "KDD group:ML")
    kdd = _conf(dl, "KDD")
    meta = json.dumps({"id": kdd["id"]})

    n_views = len(dl.client.of("views.open"))
    dl.command("/deadline", "conf edit kdd", user=who)
    assert (len(dl.client.of("views.open")) > n_views) == allowed
    if allowed:
        view = _load(dl.client.of("views.open")[-1]["view"])
        assert view["blocks"][0]["element"]["initial_value"] == "KDD"
    else:
        assert "관리자" in dl.responded[-1]["text"]

    r = dl.submit("deadline_conf_modal", _conf_values("KDD", group="DM"), user=who, metadata=meta)
    assert ("errors" in (r.body or "")) != allowed
    assert (_conf(dl, "KDD")["grp"] == "DM") == allowed

    dl.command("/deadline", "set KDD 2027 paper 2027-02-01 23:59", user=who)
    assert ("추가했어요" in dl.responded[-1]["text"]) == allowed

    dl.command("/deadline", "conf unwatch KDD", user=who)
    assert (_conf(dl, "KDD")["watch"] == 0) == allowed

    dl.submit("deadline_conf_remove_modal", {}, user=who, metadata=meta)
    assert (_conf(dl, "KDD") is None) == allowed


def test_seeded_confs_admin_only(dl):
    dl.command("/deadline", "set ICLR 2028 paper 2027-09-20", user=KIM)
    assert "관리자" in dl.responded[-1]["text"]
    dl.command("/deadline", "set ICLR 2028 paper 2027-09-20", user=BOSS)
    assert "추가했어요" in dl.responded[-1]["text"]


def test_conf_edit_modal_deadlines(dl):
    """수정 모달에서 마감 목록을 보고 고치기: 바꾼 줄은 수정, 지운 줄은 삭제, 새 줄은 추가"""
    iclr = _conf(dl, "ICLR")
    D.set_deadline(dl.ctx, iclr, 2028, "abstract", datetime(2027, 9, 10, 23, 59), None, BOSS)
    D.set_deadline(dl.ctx, iclr, 2028, "paper", datetime(2027, 9, 17, 23, 59), None, BOSS)
    D.add_target(dl.ctx, KIM, "ICLR")
    dl.command("/deadline", "conf edit ICLR", user=BOSS)
    view = _load(dl.client.of("views.open")[-1]["view"])
    lines = next(b for b in view["blocks"] if b["block_id"] == "deadlines")["element"]["initial_value"]
    assert "2028 abstract 2027-09-10 23:59 AoE" in lines and "2028 paper 2027-09-17 23:59 AoE" in lines

    meta = json.dumps({"id": iclr["id"]})
    r = dl.submit("deadline_conf_modal", _conf_values("ICLR", deadlines="2028 paper 9/17"), user=BOSS, metadata=meta)
    assert "형식" in json.loads(r.body)["errors"]["deadlines"]

    new = ("2027 abstract 2026-09-18 23:59 AoE\n2027 paper 2026-09-25 23:59 AoE\n"
           "2028 paper 2027-09-19 23:59 AoE | https://iclr.cc/2028 | 연장됨\n2028 supplementary 2027-09-26 12:00 UTC-7")
    dl.client.calls.clear()
    r = dl.submit("deadline_conf_modal", _conf_values("ICLR", url="https://iclr.cc", deadlines=new),
                  user=BOSS, metadata=meta)
    assert r.status == 200 and not r.body
    rows = {(d["year"], d["kind"]): d for d in D.conf_deadlines(dl.ctx, iclr["id"])}
    assert set(rows) == {(2027, "abstract"), (2027, "paper"), (2028, "paper"), (2028, "supplementary")}
    assert rows[(2028, "paper")]["note"] == "연장됨" and rows[(2028, "supplementary")]["tz"] == "UTC-7"
    kim = " ".join(_text(m) for m in _dms(dl, KIM))
    assert "ICLR 2028 논문 마감 이 9/18 → 9/20으로 바뀌었어요" in kim
    assert "ICLR 2028 초록 마감 이 목록에서 삭제됐어요" in kim
    assert "ICLR 2028 부록 마감 날짜가 등록됐어요" in kim


def test_conf_remove_cleans_targets_and_alerts(dl):
    D.quick_add(dl.ctx, ME, "KDD group:ML")
    kdd = _conf(dl, "KDD")
    D.set_deadline(dl.ctx, kdd, 2027, "paper", datetime(2026, 11, 4, 23, 59), "AoE", ME)   # KST 11/5 = D-7
    D.add_target(dl.ctx, KIM, "KDD 그래프 논문")
    D.add_target(dl.ctx, BOSS, "KDD")
    assert D.reminder_job(dl.ctx) == 2   # D-7 알림 → 알림 기록 2개
    assert dl.ctx.db.one("SELECT COUNT(*) c FROM deadline_alerts")["c"] == 2

    dl.command("/deadline", "conf remove kdd")
    view = _load(dl.client.of("views.open")[-1]["view"])
    assert view["callback_id"] == "deadline_conf_remove_modal"
    shown = json.dumps(view["blocks"], ensure_ascii=False)
    assert f"<@{KIM}>" in shown and f"<@{BOSS}>" in shown and "그래프 논문" in shown

    dl.client.calls.clear()
    dl.submit("deadline_conf_remove_modal", {}, metadata=json.dumps({"id": kdd["id"]}))
    for table in ("conferences WHERE name='KDD'", "conf_deadlines WHERE conf_id=?", "conf_targets WHERE conf_id=?",
                  "deadline_alerts"):
        params = (kdd["id"],) if "?" in table else ()
        assert dl.ctx.db.one(f"SELECT COUNT(*) c FROM {table}", params)["c"] == 0, table
    assert any("삭제" in _text(m) for m in _dms(dl, KIM))
    dl.clock["now"] = NOW + timedelta(days=4)
    assert D.reminder_job(dl.ctx) == 0


def test_remove_cycle_cleans_links(dl):
    cyc = _conf(dl, "ARR 2027 Jan")
    D.delete_conf(dl.ctx, cyc["id"])
    assert D.linked(dl.ctx, _conf(dl, "ACL")) == []


# ── 이름 찾기 ──────────────────────────────────────────────
def test_name_lookup_and_suggestion(dl):
    assert _conf(dl, "iclr")["name"] == "ICLR"
    assert _conf(dl, "Interspeech")["name"] == _conf(dl, "INTERSPEECH")["name"]
    assert _conf(dl, "arr 2027 jan")["name"] == "ARR 2027 Jan"
    assert D.find_conf(dl.ctx, "icrl") == (None, "ICLR")
    assert D.find_conf(dl.ctx, "neurip") == (None, "NeurIPS")
    assert D.find_conf(dl.ctx, "zzzz") == (None, None)
    dl.command("/deadline", "target icrl")
    assert "혹시 *ICLR*?" in dl.responded[-1]["text"]
    dl.command("/deadline", "conf edit Interspech", user=BOSS)
    assert "혹시 *Interspeech*?" in dl.responded[-1]["text"]


# ── set / unset ────────────────────────────────────────────
def test_set_and_unset(dl):
    dl.command("/deadline", "set iclr 2028 paper 2027-10-01 23:59 AoE", user=BOSS)
    assert "ICLR 2028 논문 마감 · 10/2(토) 20:59 KST (AoE 10/1 23:59)" in dl.responded[-1]["text"]
    dl.command("/deadline", "set ICLR 2028 abstract 2027-09-24", user=BOSS)   # 시각·시간대 생략 → 23:59 AoE
    d = next(x for x in D.conf_deadlines(dl.ctx, _conf(dl, "ICLR")["id"]) if x["year"] == 2028 and x["kind"] == "abstract")
    assert d["tz"] == "AoE" and D.when_text(dl.ctx, d["due_at"], d["tz"]).startswith("9/25(토) 20:59 KST")
    dl.command("/deadline", "set ARR 2027 Jan arr_cycle 2027-01-15 12:00 UTC", user=BOSS)  # 이름에 연도, 연도 생략
    assert "ARR 2027 Jan 투고 마감" in dl.responded[-1]["text"]
    dl.command("/deadline", "set ICLR paper 2027-10-01", user=BOSS)
    assert "연도" in dl.responded[-1]["text"]
    dl.command("/deadline", "set ICLR 2028 poster 2027-10-01", user=BOSS)
    assert "종류" in dl.responded[-1]["text"]
    dl.command("/deadline", "set ICLR 2028 paper 2027-10-01 23:59 Mars", user=BOSS)
    assert "시간대" in dl.responded[-1]["text"]
    dl.command("/deadline", "unset ICLR 2028 abstract", user=BOSS)
    assert "지웠어요" in dl.responded[-1]["text"]
    assert not any(x["kind"] == "abstract" and x["year"] == 2028 for x in D.conf_deadlines(dl.ctx, _conf(dl, "ICLR")["id"]))


# ── 표시 ───────────────────────────────────────────────────
def test_upcoming_list(dl):
    iclr = _conf(dl, "ICLR")
    D.set_deadline(dl.ctx, iclr, 2028, "paper", datetime(2026, 11, 20, 23, 59), None, BOSS)
    D.set_deadline(dl.ctx, iclr, 2028, "abstract", datetime(2026, 11, 13, 23, 59), None, BOSS)
    D.set_deadline(dl.ctx, iclr, 2029, "paper", datetime(2027, 6, 1, 23, 59), None, BOSS)  # 90일 밖
    dl.command("/deadline")
    out = json.dumps(dl.responded[-1]["blocks"], ensure_ascii=False)
    abstract = out.index("📝 *ICLR 2028 초록 마감*")
    assert abstract < out.index("ICLR 2028 논문 마감") < out.index("NAACL 2027 commitment 마감")
    assert "2029" not in out and "날짜 미정: " in out and "ICML" in out and "/deadline set" in out
    assert D.CHECK_NOTE in out

    dl.command("/deadline", "conf list")
    out = json.dumps(dl.responded[-1]["blocks"], ensure_ascii=False)
    assert "*NLP*" in out and "*Speech*" in out and "날짜 미정" in out and "ARR 2026 Oct" in out
    assert "종료 (투고 마감 10/13(화))" in out and "다음 일정 미정 (지난 마감 9/24(목))" in out  # ICASSP
    dl.command("/deadline")
    assert "ARR 2026 Oct" not in json.dumps(dl.responded[-1]["blocks"], ensure_ascii=False)  # 끝난 주기
    dl.command("/deadline", "help")
    assert "/deadline conf add" in dl.responded[-1]["text"] and "/deadline targets" in dl.responded[-1]["text"]


# ── 목표 학회 ──────────────────────────────────────────────
def test_target_direct_and_mine(dl):
    iclr = _conf(dl, "ICLR")
    D.set_deadline(dl.ctx, iclr, 2028, "paper", datetime(2026, 11, 20, 23, 59), None, BOSS)
    dl.command("/deadline", "target iclr Efficient Diffusion Transformers", user=KIM)
    t = dl.ctx.db.one("SELECT * FROM conf_targets WHERE user_id=?", (KIM,))
    assert t["conf_id"] == iclr["id"] and t["title"] == "Efficient Diffusion Transformers"
    dl.command("/deadline", "target ICML", user=KIM)
    assert "날짜 미정" in json.dumps(dl.responded[-1]["blocks"], ensure_ascii=False)
    dl.command("/deadline", "mine", user=KIM)
    out = json.dumps(dl.responded[-1]["blocks"], ensure_ascii=False)
    assert out.index("ICLR") < out.index("ICML") and "Efficient Diffusion" in out
    dl.command("/deadline", "targets")
    out = json.dumps(dl.responded[-1]["blocks"], ensure_ascii=False)
    assert "김철수" in out and "논문 마감 D-23" in out
    dl.command("/deadline", "untarget icml", user=KIM)
    assert dl.ctx.db.one("SELECT COUNT(*) c FROM conf_targets WHERE user_id=?", (KIM,))["c"] == 1


def test_arr_target_registers_cycle_and_commitment(dl):
    acl = _conf(dl, "ACL")
    D.cmd_set(dl.ctx, BOSS, "ARR 2027 Jan arr_cycle 2026-11-29 23:59")       # D-30 (KST 11/30)
    D.cmd_set(dl.ctx, BOSS, "ACL 2027 commitment 2027-03-21 23:59")
    # 더 늦은 주기도 연결 → 가장 가까운 주기만
    D.quick_add(dl.ctx, BOSS, "ARR 2027 Mar type:arr_cycle commit:ACL")
    D.cmd_set(dl.ctx, BOSS, "ARR 2027 Mar arr_cycle 2027-03-01 23:59")

    dl.command("/deadline", "target ACL", user=KIM)
    out = json.dumps(dl.responded[-1]["blocks"], ensure_ascii=False)
    assert "ARR 2027 Jan 투고 마감" in out and "ACL 2027 commitment 마감" in out and "ARR 2027 Mar" not in out
    items = D.alert_deadlines(dl.ctx, acl)
    assert [(c["name"], d["kind"]) for c, d, _k in items] == [("ARR 2027 Jan", "arr_cycle"), ("ACL", "commitment")]

    dl.clock["now"] = NOW + timedelta(days=2)   # 10/31 → 주기 마감(KST 11/30) D-30
    assert D.reminder_job(dl.ctx) == 1
    dl.clock["now"] = datetime(2027, 2, 19, 9, 0, tzinfo=TZ)  # commitment 3/22 KST → D-31, 주기 Mar → D-10
    assert D.reminder_job(dl.ctx) == 0
    dl.clock["now"] = datetime(2027, 2, 20, 9, 0, tzinfo=TZ)  # commitment D-30
    assert D.reminder_job(dl.ctx) == 1
    msgs = [_text(m) for m in _dms(dl, KIM)]
    assert "ARR 2027 Jan 투고 마감" in msgs[-2] and "ACL 2027 commitment 마감" in msgs[-1]

    # 주기·commitment 날짜 미정이면 '날짜 미정'으로 둘 다 안내
    dl.command("/deadline", "target EMNLP", user=ME)
    out = json.dumps(dl.responded[-1]["blocks"], ensure_ascii=False)
    assert "EMNLP commitment 마감 · _날짜 미정" in out


def test_date_change_notifies_targeters(dl):
    iclr = _conf(dl, "ICLR")
    D.set_deadline(dl.ctx, iclr, 2028, "paper", datetime(2027, 9, 30, 23, 59), None, BOSS)
    D.add_target(dl.ctx, KIM, "ICLR")
    D.add_target(dl.ctx, ME, "NAACL")   # 다른 학회
    dl.client.calls.clear()
    dl.command("/deadline", "set ICLR 2028 paper 2027-10-02 23:59", user=BOSS)
    kim = _dms(dl, KIM)
    assert len(kim) == 1
    body = _text(kim[0])
    assert "ICLR 2028 논문 마감 이 10/1 → 10/3으로 바뀌었어요" in body
    assert "https://iclr.cc/" in body and D.CHECK_NOTE in body
    assert not _dms(dl, ME)
    dl.command("/deadline", "set ICLR 2028 paper 2027-10-02 23:59", user=BOSS)   # 같은 날짜 → 알림 없음
    assert len(_dms(dl, KIM)) == 1

    # ARR 주기 날짜가 바뀌면 연결된 학회 목표자에게
    D.add_target(dl.ctx, KIM, "ACL")
    D.cmd_set(dl.ctx, BOSS, "ARR 2027 Jan arr_cycle 2027-01-15")
    D.cmd_set(dl.ctx, BOSS, "ARR 2027 Jan arr_cycle 2027-01-12")
    assert "ARR 2027 Jan 투고 마감 이 1/16 → 1/13으로 바뀌었어요" in _text(_dms(dl, KIM)[-1])


# ── 알림 ───────────────────────────────────────────────────
def test_reminders_stages_no_duplicates_no_past(dl):
    iclr = _conf(dl, "ICLR")
    # 마감 KST 11/30 20:59 (AoE 11/29) → 10/29 기준 D-32
    D.set_deadline(dl.ctx, iclr, 2028, "abstract", datetime(2026, 11, 22, 23, 59), None, BOSS)  # KST 11/23
    D.set_deadline(dl.ctx, iclr, 2028, "paper", datetime(2026, 11, 29, 23, 59), None, BOSS)     # KST 11/30
    D.add_target(dl.ctx, KIM, "ICLR")
    sent = {}
    day = NOW
    while day.date() <= date(2026, 12, 2):
        dl.clock["now"] = day
        D.reminder_job(dl.ctx)
        D.reminder_job(dl.ctx)    # 같은 날 두 번 돌아도 한 번만
        dl.clock["now"] = day.replace(hour=15)
        D.reminder_job(dl.ctx)
        for m in _dms(dl, KIM)[sum(len(v) for v in sent.values()):]:
            sent.setdefault(day.date(), []).append(m["text"])
        day += timedelta(days=1)
    abstract_days = [d for d, ts in sent.items() for t in ts if "초록 마감" in t]
    paper_days = [d for d, ts in sent.items() for t in ts if "논문 마감" in t]
    assert abstract_days == [date(2026, 11, 9), date(2026, 11, 16), date(2026, 11, 20),
                             date(2026, 11, 22), date(2026, 11, 23)]       # D-30은 이미 지남(10/24)
    assert paper_days == [date(2026, 10, 31), date(2026, 11, 16), date(2026, 11, 23), date(2026, 11, 27),
                          date(2026, 11, 29), date(2026, 11, 30)]
    assert all(len(ts) <= 2 for ts in sent.values())
    assert "오늘 마감" in sent[date(2026, 11, 30)][0]
    assert not any(d > date(2026, 11, 30) for d in sent)          # 지난 마감은 알리지 않음
    last = _dms(dl, KIM)[-1]
    assert "https://iclr.cc/" in _text(last) and D.CHECK_NOTE in _text(last)


def test_reminder_resends_after_date_change(dl):
    iclr = _conf(dl, "ICLR")
    D.set_deadline(dl.ctx, iclr, 2028, "paper", datetime(2026, 11, 4, 23, 59), None, BOSS)   # KST 11/5 → D-7
    D.add_target(dl.ctx, KIM, "ICLR")
    assert D.reminder_job(dl.ctx) == 1
    D.set_deadline(dl.ctx, iclr, 2028, "paper", datetime(2026, 11, 5, 23, 59), None, BOSS)   # 하루 연장
    assert D.reminder_job(dl.ctx) == 0           # 같은 날 같은 마감은 한 번만
    dl.clock["now"] = NOW + timedelta(days=1)    # 새 날짜 기준 D-7
    assert D.reminder_job(dl.ctx) == 1


def test_weekly_channel_summary(dl):
    dl.ctx.cfg.deadline_channel = "CDL"
    iclr, icml = _conf(dl, "ICLR"), _conf(dl, "ICML")
    D.set_deadline(dl.ctx, iclr, 2028, "paper", datetime(2026, 11, 20, 23, 59), None, BOSS)
    D.set_deadline(dl.ctx, icml, 2027, "paper", datetime(2026, 11, 21, 23, 59), None, BOSS)
    D.set_deadline(dl.ctx, iclr, 2029, "paper", datetime(2027, 1, 20, 23, 59), None, BOSS)   # 6주 밖
    dl.command("/deadline", "conf unwatch ICML", user=BOSS)
    assert D.weekly_job(dl.ctx)
    post = _text([a for a in dl.client.of("chat.postMessage") if a["channel"] == "CDL"][-1])
    assert "ICLR 2028 논문 마감" in post and "NAACL 2027 commitment" not in post  # NAACL KST 12/24 = D-56
    assert "ICML" not in post and "2029" not in post and D.CHECK_NOTE in post


def test_weekly_review_and_home(dl):
    iclr = _conf(dl, "ICLR")
    D.set_deadline(dl.ctx, iclr, 2028, "paper", datetime(2026, 11, 20, 23, 59), None, BOSS)
    D.add_target(dl.ctx, KIM, "ICLR")
    _t, blocks = meetings.weekly_blocks(dl.ctx, KIM)
    assert "🎯 목표 학회까지 ICLR 2028 논문 마감 D-23" in json.dumps(blocks, ensure_ascii=False)
    _t, blocks = meetings.weekly_blocks(dl.ctx, ME)
    assert "목표 학회" not in json.dumps(blocks, ensure_ascii=False)

    dl.ctx.refresh_home(KIM)
    view = _load(dl.client.of("views.publish")[-1]["view"])
    out = json.dumps(view["blocks"], ensure_ascii=False)
    assert "내 목표 학회" in out and "ICLR 2028 논문 마감" in out and "D-23" in out
    dl.action("deadline_upcoming_open", user=KIM)
    assert _load(dl.client.of("views.open")[-1]["view"])["title"]["text"] == "학회 마감"
