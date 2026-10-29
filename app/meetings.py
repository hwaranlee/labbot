"""미팅: 회의록 → 요약·결정 사항·action items → Todo 등록.
정기 1:1 미팅이 끝나면 정리 알림, 매주 금요일 action items 주간 정리."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta

from . import deadlines, todo
from .ctx import Ctx, actions, button, confirm_dialog, context, divider, modal_value, section
from .llm import LLMError
from .timeparse import WEEKDAY_NAMES, fmt_due, parse_due, parse_hhmm, weekday_index

log = logging.getLogger("labbot.meetings")

KINDS = {"one_on_one": "개인 연구미팅", "group": "그룹 미팅", "lab": "랩 미팅", "other": "기타"}
MAX_NOTES = 3000  # Slack 입력창 최대 길이

HELP = """*🗓 /meeting 사용법*
• `/meeting` — 회의록을 붙여 넣으면 요약·결정 사항·action items를 정리하고, 확인 후 담당자별 Todo로 등록
• 스레드나 메시지 `⋮` → *미팅 정리* — 스레드 전체를 회의록으로 불러오기
• `/meeting recurring @김철수 tue 14:00 60 1:1 research meeting` — 정기 미팅 등록 (끝나면 정리 알림)
• `/meeting recurring` 정기 미팅 목록 · `/meeting list` 최근 미팅 · `/meeting view 12` 미팅 기록
• `/meeting weekly` 이번 주 내 action items 정리 (매주 금요일에도 자동으로 와요)"""

EXTRACT_SYSTEM = """너는 대학 연구실의 미팅 회의록을 정리하는 비서다.
- 실제로 누군가 해야 하는 일(action item)만 뽑는다. 이미 끝난 일이나 단순 의견은 제외한다.
- 각 항목은 담당자 한 명에 대해, 동사로 끝나는 짧고 구체적인 한국어 문장으로 쓴다. (예: "baseline 3개 추가 실험 후 표로 정리")
- 담당자가 명시되지 않았다면 개인 연구미팅에서는 호스트가 아닌 참석자(학생), 그 외에는 호스트로 둔다.
- '다음 미팅까지', '금요일까지' 같은 표현은 미팅 날짜를 기준으로 실제 날짜(YYYY-MM-DD)로 바꾼다. 알 수 없으면 빈 문자열로 둔다.
- summary는 핵심 논의를 3~6개의 짧은 문장으로, decisions는 합의·결정된 사항만 쓴다.
- 회의록에 없는 내용은 지어내지 않는다. 회의록이 영어여도 결과는 한국어로 쓰되 전문 용어는 원어를 유지한다."""

ACTION_HINT = re.compile(r"^\s*(?:[-*•]\s*)?(?:\[\s?\]|☐|todo\b|ai\b|action\b|액션|할\s?일|→|->|=>)\s*:?", re.I)


def _ids(csv: str) -> list[str]:
    return [x for x in (csv or "").split(",") if x]


def fmt_date(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})"


def week_range(d: date) -> tuple[date, date]:
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


# ── action item 추출 ───────────────────────────────────────
def extract(ctx: Ctx, title: str, kind: str, host: str, participants: list[str],
            mdate: date, notes: str) -> dict:
    """{'summary': [...], 'decisions': [...], 'items': [{assignee, task, due, time}], 'next_meeting', 'by'}"""
    if ctx.llm and ctx.llm.available:
        try:
            return _llm_extract(ctx, title, kind, host, participants, mdate, notes)
        except LLMError as e:
            log.warning("LLM 추출 실패, 규칙 기반으로 대체: %s", e)
            r = rule_extract(ctx, kind, host, participants, mdate, notes)
            r["by"] = "rule_fallback"
            return r
    return rule_extract(ctx, kind, host, participants, mdate, notes)


def _llm_extract(ctx, title, kind, host, participants, mdate, notes) -> dict:
    people = "\n".join(f"- {p}: {ctx.name(p)}" + (" (호스트)" if p == host else "") for p in participants)
    prompt = (f"미팅 제목: {title}\n종류: {KINDS.get(kind, kind)}\n"
              f"미팅 날짜: {mdate.isoformat()} ({WEEKDAY_NAMES[mdate.weekday()]}요일)\n"
              f"참석자 (ID: 이름):\n{people}\n\n회의록:\n<notes>\n{notes}\n</notes>")
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "action_items": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "assignee_id": {"type": "string", "enum": participants},
                    "task": {"type": "string"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD 또는 빈 문자열"},
                    "due_time": {"type": "string", "description": "HH:MM 또는 빈 문자열"},
                },
                "required": ["assignee_id", "task", "due_date", "due_time"]}},
            "next_meeting": {"type": "string", "description": "다음 미팅 일정이 언급됐으면 그 내용, 없으면 빈 문자열"},
        },
        "required": ["summary", "decisions", "action_items", "next_meeting"],
    }
    out = ctx.llm.structured(EXTRACT_SYSTEM, prompt, "save_meeting_notes",
                             "정리한 미팅 요약과 action items를 저장한다.", schema)
    items = []
    for it in out.get("action_items", []):
        a = it.get("assignee_id")
        if a not in participants or not (it.get("task") or "").strip():
            continue
        due = it.get("due_date") or ""
        if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            due = ""
        tm = it.get("due_time") or ""
        if tm and not parse_hhmm(tm):
            tm = ""
        items.append({"assignee": a, "task": it["task"].strip(), "due": due, "time": tm})
    return {"summary": [s for s in out.get("summary", []) if s], "decisions": [d for d in out.get("decisions", []) if d],
            "items": items, "next_meeting": out.get("next_meeting", ""), "by": "llm"}


def _name_keys(ctx: Ctx, uid: str) -> list[str]:
    u = ctx.user(uid)
    p = u.get("profile", {})
    keys = {ctx.name(uid), p.get("real_name", ""), p.get("display_name", ""), u.get("real_name", ""), u.get("name", "")}
    return sorted((k for k in keys if k and len(k) >= 2), key=len, reverse=True)


def find_person(ctx: Ctx, text: str, candidates: list[str]) -> tuple[str | None, str]:
    """텍스트에서 사람(멘션 또는 이름)을 찾아 (ID, 이름을 뺀 텍스트)를 돌려줍니다."""
    m = re.search(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", text)
    if m:
        return m.group(1), (text[:m.start()] + text[m.end():]).strip()
    for uid in candidates:
        for key in _name_keys(ctx, uid):
            # 영어 이름은 대소문자 무시 + 단어 경계 ('Li'가 'baseline' 안에서 잡히지 않도록)
            pre = r"(?<![A-Za-z0-9])" if key[0].isascii() else ""
            post = r"(?![A-Za-z0-9])" if key[-1].isascii() else ""
            m = re.search(r"@?" + pre + re.escape(key) + post + r"(?:님)?\s*[:：]?", text, re.I)
            if m:
                return uid, (text[:m.start()] + text[m.end():]).strip(" -:,")
    return None, text


def rule_extract(ctx: Ctx, kind: str, host: str, participants: list[str], mdate: date, notes: str) -> dict:
    others = [p for p in participants if p != host]
    default = others[0] if kind == "one_on_one" and len(others) == 1 else host
    base_now = datetime.combine(mdate, time(0, 1), ctx.tz)
    items = []
    for line in notes.splitlines():
        raw = line.strip()
        if not raw:
            continue
        hint = ACTION_HINT.match(raw)
        if not hint and "까지" not in raw and not re.search(r"\b(?:by|due)\s+\S", raw, re.I):
            continue
        body = raw[hint.end():].strip() if hint else re.sub(r"^[-*•]\s*", "", raw)
        who, body = find_person(ctx, body, participants)
        due, has_time, task = parse_due(body, base_now)
        task = task.strip(" -:,")
        if not task:
            continue
        items.append({"assignee": who or default, "task": task,
                      "due": due.date().isoformat() if due else "",
                      "time": f"{due:%H:%M}" if due and has_time else ""})
    return {"summary": [], "decisions": [], "items": items, "next_meeting": "", "by": "rule"}


def item_due(ctx: Ctx, it: dict) -> tuple[datetime | None, bool]:
    if not it.get("due"):
        return None, False
    d = date.fromisoformat(it["due"])
    t = parse_hhmm(it["time"]) if it.get("time") else None
    return datetime.combine(d, t or time(23, 59), ctx.tz), t is not None


# ── 저장/표시 ──────────────────────────────────────────────
def save_draft(ctx: Ctx, title, kind, host, participants, mdate: date, notes, result, share="dm",
               recurring_id=None) -> int:
    return ctx.db.run(
        "INSERT INTO meetings(title, kind, host, participants, meeting_date, notes, summary, decisions, items,"
        " share, recurring_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (title, kind, host, ",".join(participants), mdate.isoformat(), notes,
         "\n".join(result["summary"]), json.dumps(result["decisions"], ensure_ascii=False),
         json.dumps(result["items"], ensure_ascii=False), share, recurring_id, int(ctx.now().timestamp())))


def get_meeting(ctx: Ctx, mid: int):
    return ctx.db.one("SELECT * FROM meetings WHERE id=?", (mid,))


def _head(ctx: Ctx, m) -> str:
    d = date.fromisoformat(m["meeting_date"])
    people = ", ".join(ctx.name(p) for p in _ids(m["participants"]))
    return f"*🗓 {m['title']}* · {fmt_date(d)} · {KINDS.get(m['kind'], m['kind'])}\n참석: {people}"


def _summary_text(m) -> str:
    parts = []
    if m["summary"]:
        parts.append("*요약*\n" + "\n".join(f"• {s}" for s in m["summary"].splitlines() if s.strip()))
    decisions = json.loads(m["decisions"] or "[]")
    if decisions:
        parts.append("*결정 사항*\n" + "\n".join(f"• {d}" for d in decisions))
    return "\n\n".join(parts)


def draft_blocks(ctx: Ctx, m, by: str = "llm") -> list:
    items = json.loads(m["items"] or "[]")
    now = ctx.now()
    b = [section(_head(ctx, m))]
    st = _summary_text(m)
    if st:
        b.append(section(st))
    lines = []
    for i, it in enumerate(items, 1):
        due, has_time = item_due(ctx, it)
        lines.append(f"{i}. <@{it['assignee']}> {it['task']}" + (f" · 📅 {fmt_due(due, has_time, now)}" if due else ""))
    b.append(section(f"*✅ Action items ({len(items)})*\n" + ("\n".join(lines) or "_찾은 항목이 없어요. ✏️ 수정으로 직접 추가할 수 있어요._")))
    note = {"llm": "Claude가 정리한 초안이에요. 확인 후 등록해 주세요.",
            "rule": "규칙 기반으로 뽑은 초안이에요 (`- [ ]`, `TODO`, `→`, `~까지`가 들어간 줄). 빠진 항목은 ✏️ 수정으로 추가하세요.",
            "rule_fallback": "⚠️ Claude API 호출에 실패해 규칙 기반으로 뽑았어요. ✏️ 수정으로 확인해 주세요."}.get(by, "")
    if note:
        b.append(context(note))
    share = "참석자에게 DM으로 공유" + (" + 미팅 채널에 게시" if m["share"] == "channel" else "")
    b.append(actions(
        button(f"✅ 등록하고 공유 ({len(items)})", "meeting_confirm", str(m["id"]), style="primary"),
        button("✏️ 수정", "meeting_edit", str(m["id"])),
        button("🗑 버리기", "meeting_discard", str(m["id"]),
               confirm=confirm_dialog("미팅 정리 버리기", "이 초안을 버릴까요?"))))
    b.append(context(f"등록하면 담당자별 Todo가 만들어지고, {share}돼요."))
    return b


def record_blocks(ctx: Ctx, uid: str | None, mid: int, with_buttons: bool = True) -> list:
    m = get_meeting(ctx, mid)
    b = [section(_head(ctx, m))]
    st = _summary_text(m)
    if st:
        b.append(section(st))
    rows = ctx.db.all("SELECT * FROM tasks WHERE meeting_id=? ORDER BY assignee, id", (mid,))
    if rows:
        b.append(section(f"*✅ Action items ({len(rows)})*"))
        for t in rows:
            line = todo.task_line(ctx, t, show_assignee=True).replace(" · 🗓 미팅", "")
            if t["status"] == "done":
                b.append(context(f"~{line}~ ✔️"))
            elif with_buttons and uid in (t["assignee"], t["creator"]):
                b.append(section(line, button("완료", "todo_done", f"{t['id']}|mtg:{mid}", style="primary")))
            else:
                b.append(section(line))
    else:
        b.append(context("action item이 없어요."))
    return b[:50]


def confirm_meeting(ctx: Ctx, mid: int, uid: str) -> tuple[bool, str]:
    m = get_meeting(ctx, mid)
    if not m:
        return False, "미팅을 찾을 수 없어요."
    if m["status"] != "draft":
        return False, "이미 처리된 미팅 정리예요."
    if uid != m["host"] and uid not in _ids(m["participants"]):
        return False, "참석자만 등록할 수 있어요."
    ctx.db.run("UPDATE meetings SET status='confirmed' WHERE id=?", (mid,))
    items = json.loads(m["items"] or "[]")
    for it in items:
        due, has_time = item_due(ctx, it)
        todo.create_task(ctx, it["task"], uid, it["assignee"], due, has_time, meeting_id=mid)
    people = set(_ids(m["participants"])) | {it["assignee"] for it in items}
    for p in people:
        mine = sum(1 for it in items if it["assignee"] == p)
        ctx.dm(p, f"🗓 {m['title']} 미팅 정리" + (f" · 내 action item {mine}개" if mine else ""),
               record_blocks(ctx, p, mid))
        ctx.refresh_home(p)
    if m["share"] == "channel" and ctx.cfg.meeting_channel:
        ctx.post(ctx.cfg.meeting_channel, f"🗓 {m['title']} 미팅 정리", record_blocks(ctx, None, mid, with_buttons=False))
    return True, f"✅ action item {len(items)}개를 등록하고 참석자에게 공유했어요."


# ── 수정 모달 ──────────────────────────────────────────────
def edit_modal(ctx: Ctx, m, channel: str, ts: str) -> dict:
    items = json.loads(m["items"] or "[]")
    lines = []
    for it in items:
        due = it["due"] + (f" {it['time']}" if it.get("time") else "") if it.get("due") else ""
        lines.append(f"{ctx.name(it['assignee'])} | {it['task']}" + (f" | {due}" if due else ""))
    el_items = {"type": "plain_text_input", "action_id": "v", "multiline": True, "max_length": 3000}
    if lines:
        el_items["initial_value"] = "\n".join(lines)[:3000]
    el_sum = {"type": "plain_text_input", "action_id": "v", "multiline": True, "max_length": 3000}
    if m["summary"]:
        el_sum["initial_value"] = m["summary"][:3000]
    names = ", ".join(ctx.name(p) for p in _ids(m["participants"]))
    return {
        "type": "modal", "callback_id": "meeting_edit_modal",
        "private_metadata": json.dumps({"id": m["id"], "channel": channel, "ts": ts}),
        "title": {"type": "plain_text", "text": "Action items 수정"},
        "submit": {"type": "plain_text", "text": "저장"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "input", "block_id": "items", "optional": True,
             "label": {"type": "plain_text", "text": "한 줄에 하나씩:  이름 | 할 일 | 마감(선택)"},
             "hint": {"type": "plain_text", "text": f"참석자: {names} · 마감 예: 2026-10-09, fri, tomorrow 14:00"[:150]},
             "element": el_items},
            {"type": "input", "block_id": "summary", "optional": True,
             "label": {"type": "plain_text", "text": "요약 (한 줄에 하나씩)"}, "element": el_sum},
        ],
    }


def parse_edit_lines(ctx: Ctx, text: str, participants: list[str], mdate: date) -> tuple[list[dict], list[str]]:
    items, errors = [], []
    base_now = datetime.combine(mdate, time(0, 1), ctx.tz)
    pool = list(dict.fromkeys(participants + ctx.members()))
    for n, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[1]:
            errors.append(f"{n}번째 줄: `이름 | 할 일` 형식이 아니에요")
            continue
        who, rest = find_person(ctx, parts[0], pool)
        if not who or rest.strip():
            errors.append(f"{n}번째 줄: '{parts[0]}' 님을 찾을 수 없어요")
            continue
        due_s, tm_s = "", ""
        if len(parts) >= 3 and parts[2]:
            due, has_time, left = parse_due(parts[2], base_now)
            if not due or left:
                errors.append(f"{n}번째 줄: 마감 '{parts[2]}'을(를) 이해하지 못했어요")
                continue
            due_s, tm_s = due.date().isoformat(), (f"{due:%H:%M}" if has_time else "")
        items.append({"assignee": who, "task": parts[1], "due": due_s, "time": tm_s})
    return items, errors


# ── 회의록 입력 모달 ───────────────────────────────────────
def meeting_modal(ctx: Ctx, host: str, notes: str = "", participants: list[str] | None = None,
                  title: str = "", kind: str = "one_on_one", recurring_id: int | None = None) -> dict:
    kind_opts = [{"text": {"type": "plain_text", "text": v}, "value": k} for k, v in KINDS.items()]
    initial_kind = next(o for o in kind_opts if o["value"] == kind)
    notes_el = {"type": "plain_text_input", "action_id": "v", "multiline": True, "max_length": MAX_NOTES,
                "placeholder": {"type": "plain_text", "text": "회의록을 자유롭게 붙여 넣으세요. 형식은 상관없어요."}}
    if notes:
        notes_el["initial_value"] = notes[-MAX_NOTES:]
    blocks = [
        {"type": "input", "block_id": "title", "label": {"type": "plain_text", "text": "제목"},
         "element": {"type": "plain_text_input", "action_id": "v", "max_length": 100,
                     "initial_value": title or "개인 연구미팅"}},
        {"type": "input", "block_id": "kind", "label": {"type": "plain_text", "text": "종류"},
         "element": {"type": "static_select", "action_id": "v", "options": kind_opts, "initial_option": initial_kind}},
        {"type": "input", "block_id": "participants", "label": {"type": "plain_text", "text": "참석자"},
         "element": {"type": "multi_users_select", "action_id": "v",
                     "initial_users": list(dict.fromkeys([host] + (participants or [])))[:30]}},
        {"type": "input", "block_id": "date", "label": {"type": "plain_text", "text": "미팅 날짜"},
         "element": {"type": "datepicker", "action_id": "v", "initial_date": ctx.today().isoformat()}},
        {"type": "input", "block_id": "notes", "label": {"type": "plain_text", "text": "회의록"}, "element": notes_el},
    ]
    if ctx.cfg.meeting_channel:
        opts = [{"text": {"type": "plain_text", "text": "참석자에게만 (DM)"}, "value": "dm"},
                {"text": {"type": "plain_text", "text": "미팅 채널에도 게시"}, "value": "channel"}]
        blocks.append({"type": "input", "block_id": "share", "label": {"type": "plain_text", "text": "공유 범위"},
                       "element": {"type": "radio_buttons", "action_id": "v", "options": opts,
                                   "initial_option": opts[0]}})
    return {"type": "modal", "callback_id": "meeting_modal",
            "private_metadata": json.dumps({"recurring_id": recurring_id}),
            "title": {"type": "plain_text", "text": "미팅 정리"},
            "submit": {"type": "plain_text", "text": "정리하기"},
            "close": {"type": "plain_text", "text": "취소"}, "blocks": blocks}


# ── 정기 미팅 ──────────────────────────────────────────────
def add_recurring(ctx: Ctx, owner: str, text: str) -> str:
    people = re.findall(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", text)
    rest = re.sub(r"<@[^>]+>", " ", text).split()
    wd = st = None
    dur, title = 60, []
    for tok in rest:
        if wd is None and weekday_index(tok) is not None:
            wd = weekday_index(tok)
        elif st is None and parse_hhmm(tok):
            st = tok if len(tok) == 5 else "0" + tok
        elif tok.isdigit() and 10 <= int(tok) <= 480 and st is not None and not title:
            dur = int(tok)
        else:
            title.append(tok)
    if not people or wd is None or st is None:
        return "형식: `/meeting recurring @김철수 tue 14:00 [분] [제목]`"
    rid = ctx.db.run("INSERT INTO recurring_meetings(owner, participants, weekday, start, duration_min, title)"
                     " VALUES (?,?,?,?,?,?)", (owner, ",".join(people), wd, st, dur,
                                                " ".join(title) or "개인 연구미팅"))
    names = ", ".join(ctx.name(p) for p in people)
    return (f"🔁 정기 미팅 `#{rid}` 등록: 매주 {WEEKDAY_NAMES[wd]} {st} ({dur}분) · {names}\n"
            f"미팅이 끝나면 참석자에게 정리 알림을 보내요.")


def recurring_blocks(ctx: Ctx, uid: str) -> list:
    rows = ctx.db.all("SELECT * FROM recurring_meetings WHERE active=1 AND (owner=? OR ','||participants||',' LIKE ?)"
                      " ORDER BY weekday, start", (uid, f"%,{uid},%"))
    b = [section("*🔁 정기 미팅*")]
    for r in rows:
        txt = (f"`#{r['id']}` 매주 {WEEKDAY_NAMES[r['weekday']]} {r['start']} ({r['duration_min']}분) · {r['title']} · "
               + ", ".join(ctx.name(p) for p in [r["owner"]] + _ids(r["participants"])))
        b.append(section(txt, button("삭제", "meeting_rec_delete", str(r["id"]),
                                     confirm=confirm_dialog("정기 미팅 삭제", "이 정기 미팅을 삭제할까요?"))))
    if not rows:
        b.append(context("등록된 정기 미팅이 없어요. `/meeting recurring @김철수 tue 14:00 60` 처럼 등록하세요."))
    return b


def recurring_prompt_job(ctx: Ctx) -> None:
    now = ctx.now()
    today = now.date()
    if not ctx.is_workday(today):
        return
    rows = ctx.db.all("SELECT * FROM recurring_meetings WHERE active=1 AND weekday=?"
                      " AND (last_prompted IS NULL OR last_prompted<>?)", (today.weekday(), today.isoformat()))
    for r in rows:
        end = datetime.combine(today, parse_hhmm(r["start"]), ctx.tz) + timedelta(minutes=r["duration_min"])
        if not end <= now < end + timedelta(hours=3):
            continue
        ctx.db.run("UPDATE recurring_meetings SET last_prompted=? WHERE id=?", (today.isoformat(), r["id"]))
        people = list(dict.fromkeys([r["owner"]] + _ids(r["participants"])))
        names = ", ".join(ctx.name(p) for p in people)
        for p in people:
            ctx.dm(p, f"📝 {r['title']} 정리 알림", [
                section(f"📝 *{r['title']}* ({names}) 끝났나요? 기억이 생생할 때 action items를 정리해 두세요."),
                actions(button("📝 지금 정리하기", "meeting_open", str(r["id"]), style="primary")),
                context("참석자 중 한 명만 정리하면 모두에게 공유돼요."),
            ])


def recorded_today(ctx: Ctx, rid: int, d: date):
    return ctx.db.one("SELECT * FROM meetings WHERE recurring_id=? AND meeting_date=? AND status<>'discarded'",
                      (rid, d.isoformat()))


# ── 주간 정리 ──────────────────────────────────────────────
def weekly_blocks(ctx: Ctx, uid: str, _arg: str = "") -> tuple[str, list]:
    start, end = week_range(ctx.today())
    s_ts = int(datetime.combine(start, time(0), ctx.tz).timestamp())
    meetings = ctx.db.all("SELECT * FROM meetings WHERE status='confirmed' AND meeting_date>=? AND meeting_date<=?"
                          " AND ','||participants||',' LIKE ? ORDER BY meeting_date",
                          (start.isoformat(), end.isoformat(), f"%,{uid},%"))
    done = ctx.db.all("SELECT * FROM tasks WHERE assignee=? AND meeting_id IS NOT NULL AND status='done'"
                      " AND done_at>=? ORDER BY done_at", (uid, s_ts))
    open_rows = ctx.db.all("SELECT * FROM tasks WHERE assignee=? AND meeting_id IS NOT NULL AND status='open'"
                           " ORDER BY due_at IS NULL, due_at, id", (uid,))
    now_ts = ctx.now().timestamp()
    overdue = sum(1 for t in open_rows if t["due_at"] and t["due_at"] < now_ts)

    title = f"🗓 이번 주 정리 ({fmt_date(start)}~{fmt_date(min(end, start + timedelta(days=4)))})"
    b = [section(f"*{title}*\n미팅 {len(meetings)}회 · 완료한 action item {len(done)}개 · 남은 항목 {len(open_rows)}개"
                 + (f" (🔴 기한 지남 {overdue})" if overdue else ""))]
    goal = deadlines.weekly_line(ctx, uid)
    if goal:
        b.append(context(goal))
    if meetings:
        b.append(context("이번 주 미팅: " + ", ".join(f"{fmt_date(date.fromisoformat(m['meeting_date']))} {m['title']}"
                                                      for m in meetings)))
    if done:
        b.append(context("✔️ 완료: " + " · ".join(f"~{t['title']}~" for t in done[:15])))
    if open_rows:
        b += todo.task_rows_blocks(ctx, open_rows, "weekly", show_creator=False)
    # 이번 주 정리 안 된 정기 미팅
    missing = []
    for r in ctx.db.all("SELECT * FROM recurring_meetings WHERE active=1 AND (owner=? OR ','||participants||',' LIKE ?)",
                        (uid, f"%,{uid},%")):
        d = start + timedelta(days=r["weekday"])
        if d <= ctx.today() and ctx.is_workday(d) and not recorded_today(ctx, r["id"], d):
            missing.append((r, d))
    for r, d in missing[:5]:
        b.append(section(f"⚠️ {fmt_date(d)} {r['start']} *{r['title']}* 정리가 아직 없어요.",
                         button("📝 정리하기", "meeting_open", str(r["id"]))))
    b.append(actions(button("📝 다른 미팅 정리하기", "meeting_open", "0")))
    return title, b


def weekly_review_job(ctx: Ctx) -> None:
    start, _ = week_range(ctx.today())
    s_ts = int(datetime.combine(start, time(0), ctx.tz).timestamp())
    users = set(ctx.members())
    users |= {r["assignee"] for r in ctx.db.all("SELECT DISTINCT assignee FROM tasks WHERE meeting_id IS NOT NULL"
                                                " AND (status='open' OR done_at>=?)", (s_ts,))}
    lines = []
    for uid in sorted(users):
        text, blocks = weekly_blocks(ctx, uid)
        ctx.dm(uid, text, blocks)
        n_meet = ctx.db.one("SELECT COUNT(*) c FROM meetings WHERE status='confirmed' AND meeting_date>=?"
                            " AND ','||participants||',' LIKE ?", (start.isoformat(), f"%,{uid},%"))["c"]
        n_done = ctx.db.one("SELECT COUNT(*) c FROM tasks WHERE assignee=? AND meeting_id IS NOT NULL"
                            " AND status='done' AND done_at>=?", (uid, s_ts))["c"]
        open_rows = ctx.db.all("SELECT due_at FROM tasks WHERE assignee=? AND meeting_id IS NOT NULL AND status='open'",
                               (uid,))
        if n_meet or n_done or open_rows:
            late = sum(1 for r in open_rows if r["due_at"] and r["due_at"] < ctx.now().timestamp())
            lines.append(f"• {ctx.name(uid)} · 미팅 {n_meet} · 완료 {n_done} · 남음 {len(open_rows)}"
                         + (f" (기한 지남 {late})" if late else ""))
    if ctx.cfg.meeting_channel and lines:
        ctx.post(ctx.cfg.meeting_channel, "이번 주 미팅 action items 현황",
                 [section("*🗓 이번 주 미팅 action items 현황*\n" + "\n".join(lines)),
                  context("각자 DM으로 이번 주 정리를 보냈어요. 남은 항목은 다음 미팅 전까지 챙겨 주세요!")])


todo.EXTRA_KINDS["weekly"] = weekly_blocks
todo.EXTRA_KINDS["mtg"] = lambda ctx, uid, arg: ("미팅 정리", record_blocks(ctx, uid, int(arg)))


# ── Slack 핸들러 ───────────────────────────────────────────
def _thread_text(ctx: Ctx, client, channel: str, msg: dict) -> tuple[str, list[str]]:
    ts = msg.get("thread_ts") or msg.get("ts")
    msgs = [msg]
    if msg.get("thread_ts") or msg.get("reply_count"):
        try:
            msgs = client.conversations_replies(channel=channel, ts=ts, limit=200)["messages"]
        except Exception:  # noqa: BLE001
            log.warning("스레드 조회 실패 (봇이 채널에 없거나 history 권한이 없음)")
    lines, people = [], []
    for m in msgs:
        u = m.get("user")
        if u and not m.get("bot_id"):
            people.append(u)
        txt = (m.get("text") or "").strip()
        if txt:
            lines.append(f"{ctx.name(u) if u else '봇'}: {txt}" if len(msgs) > 1 else txt)
    return "\n".join(lines), list(dict.fromkeys(people))


def register(app, ctx: Ctx) -> None:
    @app.command("/meeting")
    def cmd_meeting(ack, command, respond, client):
        ack()
        uid, text = command["user_id"], (command.get("text") or "").strip()
        sub, _, rest = text.partition(" ")
        sub, rest = sub.lower(), rest.strip()
        # 하위 명령어는 형식이 맞을 때만 인식하고, 그 밖의 텍스트는 회의록으로 받습니다.
        bare = not rest
        if sub in ("help", "도움말") and bare:
            return respond(HELP)
        if sub in ("recurring", "repeat", "정기") and (bare or "<@" in rest):
            if rest:
                return respond(add_recurring(ctx, uid, rest))
            return respond(text="정기 미팅", blocks=recurring_blocks(ctx, uid))
        if sub in ("list", "목록") and bare:
            rows = ctx.db.all("SELECT * FROM meetings WHERE status='confirmed' AND ','||participants||',' LIKE ?"
                              " ORDER BY meeting_date DESC, id DESC LIMIT 10", (f"%,{uid},%",))
            lines = [f"`#{m['id']}` {fmt_date(date.fromisoformat(m['meeting_date']))} *{m['title']}* · "
                     f"{(m['summary'] or '').splitlines()[0][:60] if m['summary'] else ''}" for m in rows]
            return respond("*🗓 최근 미팅*\n" + ("\n".join(lines) or "정리된 미팅이 없어요.")
                           + "\n`/meeting view 번호` 로 자세히 볼 수 있어요.")
        if sub in ("view", "show", "보기") and re.fullmatch(r"#?\d+", rest):
            m_id = re.search(r"\d+", rest)
            m = get_meeting(ctx, int(m_id.group())) if m_id else None
            if not m or (uid not in _ids(m["participants"]) and m["share"] != "channel" and not ctx.is_admin(uid)):
                return respond("볼 수 있는 미팅이 아니에요.")
            return respond(text=m["title"], blocks=record_blocks(ctx, uid, m["id"]))
        if sub in ("weekly", "week", "주간") and bare:
            t, b = weekly_blocks(ctx, uid)
            return respond(text=t, blocks=b)
        notes = rest if sub in ("", "new", "정리") else text
        client.views_open(trigger_id=command["trigger_id"], view=meeting_modal(ctx, uid, notes.strip()))

    @app.shortcut("meeting_from_message")
    def sc_meeting(ack, body, client):
        ack()
        uid = body["user"]["id"]
        ch = body.get("channel", {}).get("id", "")
        notes, people = _thread_text(ctx, client, ch, body.get("message", {}))
        kind = "one_on_one" if len(set(people) | {uid}) <= 2 else "group"
        client.views_open(trigger_id=body["trigger_id"],
                          view=meeting_modal(ctx, uid, notes, people, title="미팅" if kind == "group" else "", kind=kind))

    @app.action("meeting_open")
    def act_open(ack, body, client, respond):
        ack()
        uid = body["user"]["id"]
        rid = int(body["actions"][0]["value"] or 0)
        if rid:
            r = ctx.db.one("SELECT * FROM recurring_meetings WHERE id=?", (rid,))
            if not r:
                return respond(replace_original=False, response_type="ephemeral", text="정기 미팅을 찾을 수 없어요.")
            done = recorded_today(ctx, rid, ctx.today())
            if done:
                return respond(replace_original=False, response_type="ephemeral",
                               text=f"이미 {ctx.name(done['host'])} 님이 오늘 미팅을 정리했어요. `/meeting view {done['id']}`")
            view = meeting_modal(ctx, uid, participants=[r["owner"]] + _ids(r["participants"]),
                                 title=r["title"], kind="one_on_one", recurring_id=rid)
        else:
            view = meeting_modal(ctx, uid)
        if (body.get("view") or {}).get("type") == "modal":
            client.views_push(trigger_id=body["trigger_id"], view=view)
        else:
            client.views_open(trigger_id=body["trigger_id"], view=view)

    @app.view("meeting_modal")
    def view_meeting(ack, body, view):
        notes = modal_value(view, "notes")
        participants = modal_value(view, "participants")
        uid = body["user"]["id"]
        if not notes:
            return ack(response_action="errors", errors={"notes": "회의록을 입력해 주세요."})
        participants = list(dict.fromkeys([uid] + participants))
        ack()  # 정리에 몇 초 걸릴 수 있어 먼저 응답
        ctx.dm(uid, "🗓 회의록을 정리하고 있어요… 잠시만요.")
        meta = json.loads(view.get("private_metadata") or "{}")
        mdate = date.fromisoformat(modal_value(view, "date") or ctx.today().isoformat())
        title = modal_value(view, "title") or "미팅"
        kind = modal_value(view, "kind") or "one_on_one"
        result = extract(ctx, title, kind, uid, participants, mdate, notes)
        mid = save_draft(ctx, title, kind, uid, participants, mdate, notes, result,
                         share=modal_value(view, "share") or "dm", recurring_id=meta.get("recurring_id"))
        ctx.dm(uid, f"🗓 {title} 정리 초안", draft_blocks(ctx, get_meeting(ctx, mid), result["by"]))

    @app.action("meeting_confirm")
    def act_confirm(ack, body, respond):
        ack()
        mid = int(body["actions"][0]["value"])
        ok, msg = confirm_meeting(ctx, mid, body["user"]["id"])
        if ok:
            respond(replace_original=True, text=msg,
                    blocks=[context(msg)] + record_blocks(ctx, body["user"]["id"], mid, with_buttons=False))
        else:
            respond(replace_original=False, response_type="ephemeral", text=msg)

    @app.action("meeting_discard")
    def act_discard(ack, body, respond):
        ack()
        mid = int(body["actions"][0]["value"])
        m = get_meeting(ctx, mid)
        if m and m["status"] == "draft":
            ctx.db.run("UPDATE meetings SET status='discarded' WHERE id=?", (mid,))
        respond(replace_original=True, text="🗑 미팅 정리 초안을 버렸어요.")

    @app.action("meeting_edit")
    def act_edit(ack, body, client, respond):
        ack()
        m = get_meeting(ctx, int(body["actions"][0]["value"]))
        if not m or m["status"] != "draft":
            return respond(replace_original=False, response_type="ephemeral", text="이미 처리된 미팅 정리예요.")
        client.views_open(trigger_id=body["trigger_id"],
                          view=edit_modal(ctx, m, body["channel"]["id"], body["message"]["ts"]))

    @app.view("meeting_edit_modal")
    def view_edit(ack, body, view, client):
        meta = json.loads(view["private_metadata"])
        m = get_meeting(ctx, meta["id"])
        items, errors = parse_edit_lines(ctx, modal_value(view, "items"), _ids(m["participants"]),
                                         date.fromisoformat(m["meeting_date"]))
        if errors:
            return ack(response_action="errors", errors={"items": " / ".join(errors)[:150]})
        ack()
        summary = "\n".join(s.strip(" •-") for s in (modal_value(view, "summary") or "").splitlines() if s.strip())
        ctx.db.run("UPDATE meetings SET items=?, summary=? WHERE id=?",
                   (json.dumps(items, ensure_ascii=False), summary, m["id"]))
        try:
            client.chat_update(channel=meta["channel"], ts=meta["ts"], text="미팅 정리 초안 (수정됨)",
                               blocks=draft_blocks(ctx, get_meeting(ctx, m["id"]), by="edited"))
        except Exception:  # noqa: BLE001
            ctx.dm(body["user"]["id"], "수정된 초안", draft_blocks(ctx, get_meeting(ctx, m["id"]), by="edited"))

    @app.action("meeting_rec_delete")
    def act_rec_delete(ack, body, respond):
        ack()
        uid = body["user"]["id"]
        rid = int(body["actions"][0]["value"])
        r = ctx.db.one("SELECT * FROM recurring_meetings WHERE id=?", (rid,))
        if r and (r["owner"] == uid or uid in _ids(r["participants"]) or ctx.is_admin(uid)):
            ctx.db.run("UPDATE recurring_meetings SET active=0 WHERE id=?", (rid,))
        respond(replace_original=True, text="정기 미팅", blocks=recurring_blocks(ctx, uid))
