"""App Home 탭: 오늘 근태 + 내 할 일 + GPU 현황을 한 화면에."""
from __future__ import annotations

import logging

from . import attendance as att
from . import todo
from .ctx import Ctx, actions, button, context, divider, header, section
from .servers import Monitor

log = logging.getLogger("labbot.home")


def home_view(ctx: Ctx, mon: Monitor, uid: str) -> dict:
    today = ctx.today()
    b: list = [header("🧪 LabBot")]
    hol = ctx.holiday_name(today)
    b.append(context(f"{att.fmt_day(today)}" + (f" · {hol}" if hol else "")
                     + " · 명령어: `/todo` `/in` `/out` `/vacation` `/attendance` `/meeting` `/papers` `/gpu` `/server`"))

    # 근태
    st = att.status_of(ctx, uid)
    if st["state"] == "in":
        icon, label = att.MODES[st["rec"]["mode"]]
        txt = f"{icon} {label} 중 · {att.hm(ctx, st['first_in'])}부터"
        btns = [button("🏁 퇴근", "att_out", style="primary")]
    elif st["state"] == "out":
        txt = f"🏁 퇴근함 · {att.hm(ctx, st['first_in'])} ~ {att.hm(ctx, st['rec']['out_at'])}"
        btns = [button("🏢 다시 출근", "att_in_office", "office")]
    elif st["state"] == "leave":
        txt = f"🌴 휴가 중 ({att.LEAVE_KINDS.get(st['leave']['kind'])})"
        btns = []
    else:
        txt = "⏳ 아직 출근 기록이 없어요"
        btns = [button("🏢 출근", "att_in_office", "office", style="primary"),
                button("🏠 재택", "att_in_remote", "remote")]
    if st.get("leave") and st["state"] in ("in", "out"):
        txt += f" · 🌴 {att.LEAVE_KINDS.get(st['leave']['kind'])}"
    b.append(section(f"*🕘 오늘 근태*\n{txt}"))
    btns += [button("🌴 휴가 신청", "leave_open"), button("내 휴가", "leave_list_open"),
             button("팀 현황", "att_board_open")]
    b.append(actions(*btns))
    b.append(divider())

    # 할 일
    rows = todo.open_tasks_for(ctx, uid)
    b.append(section(f"*📝 내 할 일* ({len(rows)})", button("➕ 추가", "todo_open_add", style="primary")))
    if rows:
        b += todo.task_rows_blocks(ctx, rows[:15], "home")
        if len(rows) > 15:
            b.append(context(f"외 {len(rows) - 15}개 · 전체 목록은 `/todo`"))
    else:
        b.append(context("남은 할 일이 없어요. 🎉"))
    sent = todo.sent_tasks(ctx, uid)
    if sent:
        b.append(context(f"📤 내가 맡긴 진행 중인 일 {len(sent)}개 · `/todo sent`"))
    b.append(divider())

    # 미팅 · 논문
    mtg_open = ctx.db.one("SELECT COUNT(*) c FROM tasks WHERE assignee=? AND meeting_id IS NOT NULL AND status='open'",
                          (uid,))["c"]
    b.append(section(f"*🗓 미팅 · 📚 논문*\n미팅 action items 남은 것 {mtg_open}개"))
    b.append(actions(button("📝 미팅 정리", "meeting_open", "0", style="primary"),
                     button("이번 주 정리", "weekly_open"), button("논문 주제", "papers_topics_open")))
    b.append(divider())

    # 서버
    free, total = mon.gpu_counts()
    lines = []
    for host, d, online in mon.hosts():
        if not online:
            lines.append(f"🔴 {host} 응답 없음")
            continue
        disks = d.get("disks") or []
        worst = max((dk["used_gb"] / dk["total_gb"] * 100 for dk in disks if dk.get("total_gb")), default=None)
        g = d.get("gpus") or []
        part = f"• *{host}*"
        if g:
            avg = sum(x.get("util") or 0 for x in g) / len(g)
            part += f" · GPU {len(g)}장 평균 {avg:.0f}%"
        if worst is not None:
            part += f" · 디스크 최대 {worst:.0f}%" + (" ⚠️" if worst >= ctx.cfg.disk_alert_pct else "")
        lines.append(part)
    mine = mon.my_reservations_text(uid)
    txt = f"*🖥 서버* · 🟢 사용 가능한 GPU {free}/{total}\n" + ("\n".join(lines) or "아직 받은 데이터가 없어요.")
    if mine:
        txt += "\n" + mine
    b.append(section(txt, button("GPU 현황", "gpu_status_open")))
    return {"type": "home", "blocks": b[:100]}


def publish(ctx: Ctx, mon: Monitor, uid: str) -> None:
    try:
        ctx.client.views_publish(user_id=uid, view=home_view(ctx, mon, uid))
    except Exception:  # noqa: BLE001
        log.exception("홈 갱신 실패: %s", uid)


def _modal(title: str, blocks: list) -> dict:
    return {"type": "modal", "title": {"type": "plain_text", "text": title[:24]},
            "close": {"type": "plain_text", "text": "닫기"}, "blocks": blocks[:100]}


def register(app, ctx: Ctx, mon: Monitor) -> None:
    @app.event("app_home_opened")
    def on_home(event):
        if event.get("tab", "home") == "home":
            publish(ctx, mon, event["user"])

    @app.action("gpu_status_open")
    def act_gpu(ack, body, client):
        ack()
        client.views_open(trigger_id=body["trigger_id"], view=_modal("GPU 현황", mon.gpu_blocks()))

    @app.action("att_board_open")
    def act_board(ack, body, client):
        ack()
        client.views_open(trigger_id=body["trigger_id"],
                          view=_modal("근태 현황", att.board_blocks(ctx) + att.team_leaves_blocks(ctx)))

    @app.action("weekly_open")
    def act_weekly(ack, body, client):
        ack()
        from .meetings import weekly_blocks
        _t, blocks = weekly_blocks(ctx, body["user"]["id"])
        client.views_open(trigger_id=body["trigger_id"], view=_modal("이번 주 정리", blocks))

    @app.action("papers_topics_open")
    def act_topics(ack, body, client):
        ack()
        from .papers import topics_blocks
        client.views_open(trigger_id=body["trigger_id"], view=_modal("논문 주제", topics_blocks(ctx, body["user"]["id"])))

    @app.action("leave_list_open")
    def act_leaves(ack, body, client):
        ack()
        client.views_open(trigger_id=body["trigger_id"],
                          view=_modal("내 휴가", att.my_leaves_blocks(ctx, body["user"]["id"])))
