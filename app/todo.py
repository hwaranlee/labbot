"""Todo: 할 일 생성·위임·마감·리마인더·메시지에서 할 일 만들기."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta

from .ctx import (Ctx, actions, button, context, divider, modal_value, section)
from .timeparse import fmt_due, parse_due

log = logging.getLogger("labbot.todo")

MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
MAX_LIST = 20

HELP = """*📝 /todo 사용법*
• `/todo review paper tomorrow 14:00` — 내 할 일 추가 (`/todo add ...` 도 같아요)
• `/todo clean up results @홍길동 by friday` — 다른 사람에게 맡기기 (여러 명 멘션 가능)
• `/todo add` — 입력 창으로 추가
• `/todo` 또는 `/todo list` — 내 할 일 · `/todo sent` — 내가 맡긴 일 · `/todo channel` — 이 채널의 할 일
• `/todo done 12` · `/todo delete 12` (여러 개: `/todo done 12 13`)
• 메시지 `⋮` 메뉴 → *Todo로 만들기* 로 메시지를 바로 할 일로 저장
• 마감 표현: `today` `tomorrow` `fri` `by friday` `next tue` `10/5` `2026-10-05` `14:00` `3pm` (한국어 `내일` `금요일까지` 도 돼요)
• 알림: 시각이 있으면 30분 전, 날짜만 있으면 그날 오전 9시. 평일 아침엔 오늘 마감·지난 일을 모아 보내 드려요."""


# ── 데이터 ─────────────────────────────────────────────────
def compute_remind_at(due: datetime | None, has_time: bool) -> int | None:
    if not due:
        return None
    r = due - timedelta(minutes=30) if has_time else due.replace(hour=9, minute=0)
    return int(r.timestamp())


def create_task(ctx: Ctx, title: str, creator: str, assignee: str, due: datetime | None,
                has_time: bool, channel_id: str | None = None, permalink: str | None = None,
                meeting_id: int | None = None) -> int:
    remind_at = compute_remind_at(due, has_time)
    now = int(ctx.now().timestamp())
    reminded = 1 if remind_at is not None and remind_at <= now else 0
    return ctx.db.run(
        "INSERT INTO tasks(title, creator, assignee, channel_id, permalink, due_at, due_has_time,"
        " remind_at, reminded, created_at, meeting_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (title, creator, assignee, channel_id, permalink,
         int(due.timestamp()) if due else None, int(has_time), remind_at, reminded, now, meeting_id))


def get_task(ctx: Ctx, task_id: int):
    return ctx.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))


_ORDER = "ORDER BY due_at IS NULL, due_at, id"


def open_tasks_for(ctx: Ctx, uid: str):
    return ctx.db.all(f"SELECT * FROM tasks WHERE assignee=? AND status='open' {_ORDER}", (uid,))


def sent_tasks(ctx: Ctx, uid: str):
    return ctx.db.all(
        f"SELECT * FROM tasks WHERE creator=? AND assignee<>? AND status='open' {_ORDER}", (uid, uid))


def channel_tasks(ctx: Ctx, channel: str):
    return ctx.db.all(f"SELECT * FROM tasks WHERE channel_id=? AND status='open' {_ORDER}", (channel,))


def due_soon_tasks(ctx: Ctx, uid: str):
    end = datetime.combine(ctx.today(), time(23, 59, 59), ctx.tz)
    return ctx.db.all(
        f"SELECT * FROM tasks WHERE assignee=? AND status='open' AND due_at IS NOT NULL AND due_at<=? {_ORDER}",
        (uid, int(end.timestamp())))


def complete_task(ctx: Ctx, task_id: int, uid: str) -> tuple[bool, str]:
    t = get_task(ctx, task_id)
    if not t:
        return False, f"#{task_id} 할 일을 찾을 수 없어요."
    if t["status"] == "done":
        return False, f"#{task_id}는 이미 완료된 할 일이에요."
    if uid not in (t["assignee"], t["creator"]) and not ctx.is_admin(uid):
        return False, "담당자나 요청자만 완료 처리할 수 있어요."
    ctx.db.run("UPDATE tasks SET status='done', done_at=?, done_by=? WHERE id=?",
               (int(ctx.now().timestamp()), uid, task_id))
    for other in {t["creator"], t["assignee"]} - {uid}:
        ctx.dm(other, f"✅ <@{uid}>님이 할 일을 완료했어요: *{t['title']}*")
    for u in {t["creator"], t["assignee"]}:
        ctx.refresh_home(u)
    return True, f"✅ 완료: ~{t['title']}~"


def delete_task(ctx: Ctx, task_id: int, uid: str) -> tuple[bool, str]:
    t = get_task(ctx, task_id)
    if not t:
        return False, f"#{task_id} 할 일을 찾을 수 없어요."
    if uid not in (t["assignee"], t["creator"]) and not ctx.is_admin(uid):
        return False, "담당자나 요청자만 삭제할 수 있어요."
    ctx.db.run("DELETE FROM tasks WHERE id=?", (task_id,))
    for u in {t["creator"], t["assignee"]}:
        ctx.refresh_home(u)
    return True, f"🗑 삭제했어요: {t['title']}"


def snooze_task(ctx: Ctx, task_id: int, how: str) -> str:
    now = ctx.now()
    if how == "tomorrow":
        when = datetime.combine(now.date() + timedelta(days=1), time(9, 0), ctx.tz)
    else:
        when = now + timedelta(minutes=int(how))
    ctx.db.run("UPDATE tasks SET remind_at=?, reminded=0 WHERE id=?", (int(when.timestamp()), task_id))
    return f"{when:%m/%d %H:%M}"


# ── 표시 ───────────────────────────────────────────────────
def task_line(ctx: Ctx, t, show_assignee=False, show_creator=False) -> str:
    now = ctx.now()
    parts = [f"*{t['title']}*"]
    if t["due_at"]:
        due = ctx.local(t["due_at"])
        label = fmt_due(due, bool(t["due_has_time"]), now)
        parts.append(f"🔴 {label} (기한 지남)" if due < now else f"📅 {label}")
    if show_assignee:
        parts.append(f"👤 <@{t['assignee']}>")
    if show_creator and t["creator"] != t["assignee"]:
        parts.append(f"요청: <@{t['creator']}>")
    if "meeting_id" in t.keys() and t["meeting_id"]:
        parts.append("🗓 미팅")
    if t["permalink"]:
        parts.append(f"<{t['permalink']}|원문>")
    return f"`#{t['id']}` " + " · ".join(parts)


def task_rows_blocks(ctx: Ctx, rows, kind: str, show_assignee=False, show_creator=True) -> list:
    blocks = []
    for t in rows[:MAX_LIST]:
        blocks.append(section(task_line(ctx, t, show_assignee, show_creator),
                              button("완료", "todo_done", f"{t['id']}|{kind}", style="primary")))
    if len(rows) > MAX_LIST:
        blocks.append(context(f"외 {len(rows) - MAX_LIST}개 더 있어요."))
    return blocks


# 다른 모듈이 할 일 목록 메시지 종류를 추가할 때 사용 (예: 미팅 주간 정리)
EXTRA_KINDS: dict = {}


def list_blocks(ctx: Ctx, kind: str, uid: str) -> tuple[str, list]:
    base, _, arg = kind.partition(":")
    if base in EXTRA_KINDS:  # fn(ctx, uid, arg) -> (text, blocks)
        return EXTRA_KINDS[base](ctx, uid, arg)
    if kind == "sent":
        rows, title, empty = sent_tasks(ctx, uid), "📤 내가 맡긴 할 일", "다른 사람에게 맡긴 진행 중인 일이 없어요."
        show_assignee, show_creator = True, False
    elif kind.startswith("channel:"):
        ch = kind.split(":", 1)[1]
        rows, title, empty = channel_tasks(ctx, ch), f"📋 <#{ch}> 채널의 할 일", "이 채널에 남은 할 일이 없어요."
        show_assignee, show_creator = True, False
    elif kind == "digest":
        rows, title, empty = due_soon_tasks(ctx, uid), "☀️ 오늘 마감이거나 기한이 지난 할 일", "오늘 챙길 일이 없어요. 🎉"
        show_assignee, show_creator = False, True
    else:
        rows, title, empty = open_tasks_for(ctx, uid), "📝 내 할 일", "남은 할 일이 없어요. 🎉"
        show_assignee, show_creator = False, True
    blocks = [section(f"*{title}* ({len(rows)})")]
    blocks += task_rows_blocks(ctx, rows, kind, show_assignee, show_creator) or [context(empty)]
    blocks.append(actions(button("➕ 할 일 추가", "todo_open_add")))
    return f"{title} {len(rows)}개", blocks


def task_notice_blocks(ctx: Ctx, t, headline: str) -> list:
    return [section(f"{headline}\n{task_line(ctx, t, show_creator=True)}"),
            actions(button("완료", "todo_done", f"{t['id']}|msg", style="primary"),
                    button("1시간 뒤 알림", "todo_snooze_60", f"{t['id']}|60"),
                    button("내일 9시 알림", "todo_snooze_tmr", f"{t['id']}|tomorrow"))]


# ── 모달 ───────────────────────────────────────────────────
def add_modal(initial_title: str = "", assignee: str | None = None,
              channel_id: str = "", permalink: str = "") -> dict:
    assignee_el = {"type": "users_select", "action_id": "v",
                   "placeholder": {"type": "plain_text", "text": "담당자"}}
    if assignee:
        assignee_el["initial_user"] = assignee
    title_el = {"type": "plain_text_input", "action_id": "v", "multiline": True, "max_length": 2000}
    if initial_title:
        title_el["initial_value"] = initial_title[:2000]
    return {
        "type": "modal", "callback_id": "todo_add_modal",
        "private_metadata": json.dumps({"channel": channel_id, "permalink": permalink}),
        "title": {"type": "plain_text", "text": "할 일 추가"},
        "submit": {"type": "plain_text", "text": "추가"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "input", "block_id": "title", "label": {"type": "plain_text", "text": "할 일"},
             "element": title_el},
            {"type": "input", "block_id": "assignee", "label": {"type": "plain_text", "text": "담당자"},
             "element": assignee_el},
            {"type": "input", "block_id": "due_date", "optional": True,
             "label": {"type": "plain_text", "text": "마감일"},
             "element": {"type": "datepicker", "action_id": "v"}},
            {"type": "input", "block_id": "due_time", "optional": True,
             "label": {"type": "plain_text", "text": "마감 시각"},
             "element": {"type": "timepicker", "action_id": "v"}},
        ],
    }


# ── 알림 ───────────────────────────────────────────────────
def notify_assignee(ctx: Ctx, task_id: int) -> None:
    t = get_task(ctx, task_id)
    if t and t["assignee"] != t["creator"]:
        ctx.dm(t["assignee"], f"📝 <@{t['creator']}>님이 할 일을 맡겼어요: {t['title']}",
               task_notice_blocks(ctx, t, f"📝 <@{t['creator']}>님이 할 일을 맡겼어요."))


def reminder_job(ctx: Ctx) -> None:
    now = int(ctx.now().timestamp())
    rows = ctx.db.all("SELECT * FROM tasks WHERE status='open' AND reminded=0 AND remind_at<=?", (now,))
    for t in rows:
        ctx.db.run("UPDATE tasks SET reminded=1 WHERE id=?", (t["id"],))
        ctx.dm(t["assignee"], f"⏰ 할 일 알림: {t['title']}", task_notice_blocks(ctx, t, "⏰ *할 일 알림*"))


def digest_job(ctx: Ctx) -> None:
    if not ctx.is_workday(ctx.today()):
        return
    end = datetime.combine(ctx.today(), time(23, 59, 59), ctx.tz)
    users = ctx.db.all("SELECT DISTINCT assignee FROM tasks WHERE status='open' AND due_at IS NOT NULL"
                       " AND due_at<=?", (int(end.timestamp()),))
    for r in users:
        text, blocks = list_blocks(ctx, "digest", r["assignee"])
        ctx.dm(r["assignee"], text, blocks)


# ── Slack 핸들러 ───────────────────────────────────────────
def _add_from_text(ctx: Ctx, text: str, creator: str, channel_id: str) -> tuple[list[int], str | None]:
    assignees = list(dict.fromkeys(MENTION.findall(text))) or [creator]
    due, has_time, title = parse_due(MENTION.sub(" ", text), ctx.now())
    if not title:
        return [], "할 일 내용을 적어 주세요. 예: `/todo review paper tomorrow`"
    ch = channel_id if channel_id and not channel_id.startswith("D") else None
    ids = [create_task(ctx, title, creator, a, due, has_time, ch) for a in assignees]
    return ids, None


def register(app, ctx: Ctx) -> None:
    @app.command("/todo")
    def cmd_todo(ack, command, respond, client):
        ack()
        uid, text, channel = command["user_id"], (command.get("text") or "").strip(), command["channel_id"]
        sub, _, rest = text.partition(" ")
        sub_l, rest = sub.lower(), rest.strip()
        # 하위 명령어는 형식이 맞을 때만 인식합니다. ('/todo list of baselines' 는 할 일로 추가)
        bare = not rest
        numbers = bool(rest) and bool(re.fullmatch(r"(?:#?\d+[\s,]*)+", rest))

        if sub_l in ("", "list", "ls", "목록") and bare:
            t, b = list_blocks(ctx, "mine", uid)
            return respond(text=t, blocks=b)
        if sub_l in ("sent", "요청", "보낸") and bare:
            t, b = list_blocks(ctx, "sent", uid)
            return respond(text=t, blocks=b)
        if sub_l in ("channel", "채널") and bare:
            t, b = list_blocks(ctx, f"channel:{channel}", uid)
            return respond(text=t, blocks=b)
        if sub_l in ("help", "도움말", "?") and bare:
            return respond(HELP)
        if sub_l in ("done", "complete", "완료", "delete", "del", "remove", "삭제") and (bare or numbers):
            ids = [int(x) for x in re.findall(r"\d+", rest)]
            if not ids:
                return respond("번호를 알려 주세요. 예: `/todo done 12`")
            fn = complete_task if sub_l in ("done", "complete", "완료") else delete_task
            return respond("\n".join(fn(ctx, i, uid)[1] for i in ids))
        if sub_l in ("add", "new", "추가"):
            text = rest
            if not text:
                return client.views_open(trigger_id=command["trigger_id"],
                                          view=add_modal(assignee=uid, channel_id=channel))

        ids, err = _add_from_text(ctx, text, uid, channel)
        if err:
            return respond(err)
        ctx.refresh_home(uid)
        lines = []
        delegated = False
        for i in ids:
            t = get_task(ctx, i)
            lines.append(task_line(ctx, t, show_assignee=t["assignee"] != uid))
            if t["assignee"] != uid:
                delegated = True
                notify_assignee(ctx, i)
                ctx.refresh_home(t["assignee"])
        if delegated:
            respond(response_type="in_channel", text=f"<@{uid}>님이 할 일을 맡겼어요.\n" + "\n".join(lines))
        else:
            respond("➕ 추가했어요\n" + "\n".join(lines))

    @app.action("todo_open_add")
    def act_open_add(ack, body, client):
        ack()
        ch = body.get("channel", {}).get("id", "")
        client.views_open(trigger_id=body["trigger_id"], view=add_modal(assignee=body["user"]["id"], channel_id=ch))

    @app.shortcut("todo_from_message")
    def sc_from_message(ack, body, client):
        ack()
        msg, ch = body.get("message", {}), body.get("channel", {}).get("id", "")
        permalink = ""
        try:
            permalink = client.chat_getPermalink(channel=ch, message_ts=msg["ts"])["permalink"]
        except Exception:  # noqa: BLE001
            log.warning("permalink 조회 실패 (봇이 채널에 없을 수 있음)")
        title = (msg.get("text") or "").strip() or "(메시지)"
        client.views_open(trigger_id=body["trigger_id"],
                          view=add_modal(title, body["user"]["id"], ch, permalink))

    @app.view("todo_add_modal")
    def view_add(ack, body, view, client):
        title = modal_value(view, "title")
        if not title:
            return ack(response_action="errors", errors={"title": "할 일 내용을 입력해 주세요."})
        ack()
        uid = body["user"]["id"]
        meta = json.loads(view.get("private_metadata") or "{}")
        assignee = modal_value(view, "assignee") or uid
        d, tm = modal_value(view, "due_date"), modal_value(view, "due_time")
        due, has_time = None, False
        if d or tm:
            day = datetime.strptime(d, "%Y-%m-%d").date() if d else ctx.today()
            clock = datetime.strptime(tm, "%H:%M").time() if tm else time(23, 59)
            due, has_time = datetime.combine(day, clock, ctx.tz), bool(tm)
        ch = meta.get("channel") or None
        if ch and ch.startswith("D"):
            ch = None
        tid = create_task(ctx, title, uid, assignee, due, has_time, ch, meta.get("permalink") or None)
        notify_assignee(ctx, tid)
        t = get_task(ctx, tid)
        msg = "➕ 할 일을 추가했어요\n" + task_line(ctx, t, show_assignee=assignee != uid)
        try:
            if ch:
                client.chat_postEphemeral(channel=ch, user=uid, text=msg)
            else:
                raise RuntimeError
        except Exception:  # noqa: BLE001
            ctx.dm(uid, msg)
        ctx.refresh_home(uid)
        if assignee != uid:
            ctx.refresh_home(assignee)

    def _rerender(body, respond, uid: str, kind: str, result: str):
        view = body.get("view") or {}
        if view.get("type") == "modal":  # App Home에서 연 창 안의 버튼
            t, b = list_blocks(ctx, kind, uid)
            ctx.client.views_update(view_id=view["id"], view={
                "type": "modal", "title": view["title"], "close": {"type": "plain_text", "text": "닫기"},
                "blocks": ([context(result)] + b)[:100]})
            ctx.refresh_home(uid)
            return
        if body.get("container", {}).get("type") == "view":
            ctx.refresh_home(uid)
            return
        if kind == "msg":
            respond(replace_original=True, text=result, blocks=[section(result)])
        else:
            t, b = list_blocks(ctx, kind, uid)
            respond(replace_original=True, text=t, blocks=[context(result)] + b)

    @app.action("todo_done")
    def act_done(ack, body, respond):
        ack()
        uid = body["user"]["id"]
        task_id, _, kind = body["actions"][0]["value"].partition("|")
        _ok, result = complete_task(ctx, int(task_id), uid)
        _rerender(body, respond, uid, kind or "msg", result)

    @app.action(re.compile(r"^todo_snooze_"))
    def act_snooze(ack, body, respond):
        ack()
        task_id, _, how = body["actions"][0]["value"].partition("|")
        t = get_task(ctx, int(task_id))
        if not t or t["status"] != "open":
            return respond(replace_original=True, text="이미 처리된 할 일이에요.")
        when = snooze_task(ctx, int(task_id), how)
        respond(replace_original=True, text=f"⏰ {when}에 다시 알려 드릴게요: {t['title']}")
