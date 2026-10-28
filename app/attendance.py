"""근태: 출퇴근 기록(출근/재택), 휴가 신청·승인, 미출근/미퇴근 리마인더, 리포트/CSV."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date, datetime, timedelta

from .ctx import (Ctx, actions, button, confirm_dialog, context, divider, header,
                  modal_value, section)
from .timeparse import WEEKDAY_NAMES, fmt_duration, parse_hhmm

log = logging.getLogger("labbot.attendance")

MODES = {"office": ("🏢", "출근"), "remote": ("🏠", "재택")}
MODE_WORDS = {"remote": "remote", "wfh": "remote", "home": "remote", "office": "office",
              # 한국어 별칭
              "재택": "remote", "집": "remote", "사무실": "office"}
LEAVE_KINDS = {"annual": "연차", "am": "오전 반차", "pm": "오후 반차",
               "sick": "병가", "official": "공가", "other": "기타"}
OPEN_WINDOW = 20 * 3600  # 출근 후 20시간이 지나면 열린 기록으로 보지 않음

HELP = """*🕘 근태 명령어*
• `/in` 출근 · `/in remote` 재택 · `/in office 학회 발표` 출근(메모) · `/in 09:10` (오늘 출근 시각 정정)
• `/out` 퇴근 · `/out 18:30` (오늘 퇴근 시각 정정)
• `/leave` 휴가 신청 · `/leave list` 내 휴가 · `/leave team` 앞으로 2주 팀 휴가
• `/attendance` 오늘 현황 · `/attendance week` · `/attendance month @홍길동`
• `/attendance export 2026-09` 월별 CSV (관리자)"""


def fmt_day(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})"


def hm(ctx: Ctx, epoch: int) -> str:
    return f"{ctx.local(epoch):%H:%M}"


# ── 출퇴근 데이터 ───────────────────────────────────────────
def open_record(ctx: Ctx, uid: str):
    since = int(ctx.now().timestamp()) - OPEN_WINDOW
    return ctx.db.one("SELECT * FROM attendance WHERE user_id=? AND out_at IS NULL AND in_at>=?"
                      " ORDER BY in_at DESC LIMIT 1", (uid, since))


def records_on(ctx: Ctx, d: date):
    return ctx.db.all("SELECT * FROM attendance WHERE date=? ORDER BY in_at", (d.isoformat(),))


def _at(ctx: Ctx, t) -> int:
    return int(datetime.combine(ctx.today(), t, ctx.tz).timestamp())


def clock_in(ctx: Ctx, uid: str, mode: str = "office", note: str = "", at=None) -> tuple[bool, str]:
    now = int(ctx.now().timestamp())
    ts = _at(ctx, at) if at else now
    if ts > now:
        return False, "미래 시각으로는 기록할 수 없어요."
    cur = open_record(ctx, uid)
    if cur and cur["date"] == ctx.today().isoformat():
        if at:  # 출근 시각 정정
            ctx.db.run("UPDATE attendance SET in_at=? WHERE id=?", (ts, cur["id"]))
            ctx.refresh_home(uid)
            return True, f"✏️ 오늘 출근 시각을 {hm(ctx, ts)}로 고쳤어요."
        return False, f"이미 {hm(ctx, cur['in_at'])}에 출근했어요. 퇴근은 `/out`"
    ctx.db.run("INSERT INTO attendance(user_id, date, in_at, mode, note) VALUES (?,?,?,?,?)",
               (uid, ctx.today().isoformat(), ts, mode, note or None))
    icon, label = MODES[mode]
    msg = f"{icon} <@{uid}> {label} {hm(ctx, ts)}" + (f" · {note}" if note else "")
    ctx.post(ctx.cfg.attendance_channel, msg)
    ctx.refresh_home(uid)
    return True, f"{icon} {label} 기록했어요 ({hm(ctx, ts)})" + (f" · {note}" if note else "")


def clock_out(ctx: Ctx, uid: str, note: str = "", at=None) -> tuple[bool, str]:
    now = int(ctx.now().timestamp())
    cur = open_record(ctx, uid)
    if not cur:
        last = ctx.db.one("SELECT * FROM attendance WHERE user_id=? AND date=? ORDER BY in_at DESC LIMIT 1",
                          (uid, ctx.today().isoformat()))
        if last and at:  # 이미 퇴근했는데 시각 정정
            ts = _at(ctx, at)
            if ts <= last["in_at"] or ts > now:
                return False, "퇴근 시각은 출근 시각 이후, 현재 이전이어야 해요."
            ctx.db.run("UPDATE attendance SET out_at=? WHERE id=?", (ts, last["id"]))
            ctx.refresh_home(uid)
            return True, f"✏️ 오늘 퇴근 시각을 {hm(ctx, ts)}로 고쳤어요."
        return False, "열려 있는 출근 기록이 없어요. 출근 기록부터 `/in 09:00` 처럼 남겨 주세요."
    ts = _at(ctx, at) if at else now
    if at and cur["date"] != ctx.today().isoformat():
        # 어제 출근 기록에 시각만 준 경우: 출근일 기준으로 해석
        d = date.fromisoformat(cur["date"])
        ts = int(datetime.combine(d, at, ctx.tz).timestamp())
    if ts <= cur["in_at"] or ts > now:
        return False, "퇴근 시각은 출근 시각 이후, 현재 이전이어야 해요."
    ctx.db.run("UPDATE attendance SET out_at=?, note=COALESCE(?, note) WHERE id=?",
               (ts, note or None, cur["id"]))
    worked = fmt_duration(ts - cur["in_at"])
    ctx.post(ctx.cfg.attendance_channel, f"🏁 <@{uid}> 퇴근 {hm(ctx, ts)} · 근무 {worked}")
    ctx.refresh_home(uid)
    return True, f"🏁 퇴근 기록했어요 ({hm(ctx, ts)}) · 오늘 근무 {worked}"


def parse_in_args(text: str) -> tuple[str, str, object]:
    mode, at, rest = "office", None, []
    for tok in text.split():
        if tok.lower() in MODE_WORDS and mode == "office" and not rest:
            mode = MODE_WORDS[tok.lower()]
        elif parse_hhmm(tok) and at is None:
            at = parse_hhmm(tok)
        else:
            rest.append(tok)
    return mode, " ".join(rest), at


def status_of(ctx: Ctx, uid: str, d: date | None = None) -> dict:
    """한 사람의 오늘 상태: {'state': in|out|leave|none, ...}"""
    d = d or ctx.today()
    lv = leave_on(ctx, uid, d)
    rows = ctx.db.all("SELECT * FROM attendance WHERE user_id=? AND date=? ORDER BY in_at", (uid, d.isoformat()))
    if rows:
        last = rows[-1]
        if last["out_at"] is None:
            return {"state": "in", "rec": last, "first_in": rows[0]["in_at"], "leave": lv}
        return {"state": "out", "rec": last, "first_in": rows[0]["in_at"], "leave": lv}
    if lv:
        return {"state": "leave", "leave": lv}
    return {"state": "none", "leave": None}


# ── 휴가 데이터 ─────────────────────────────────────────────
def leave_on(ctx: Ctx, uid: str, d: date):
    return ctx.db.one("SELECT * FROM leaves WHERE user_id=? AND status='approved' AND start_date<=? AND end_date>=?",
                      (uid, d.isoformat(), d.isoformat()))


def count_leave_days(ctx: Ctx, kind: str, start: date, end: date) -> float:
    if kind in ("am", "pm"):
        return 0.5 if ctx.is_workday(start) else 0.0
    n, d = 0, start
    while d <= end:
        n += ctx.is_workday(d)
        d += timedelta(days=1)
    return float(n)


def used_days(ctx: Ctx, uid: str, year: int) -> float:
    r = ctx.db.one("SELECT COALESCE(SUM(days),0) s FROM leaves WHERE user_id=? AND status='approved'"
                   " AND kind IN ('annual','am','pm') AND substr(start_date,1,4)=?", (uid, str(year)))
    return float(r["s"])


def leave_text(l) -> str:
    s, e = date.fromisoformat(l["start_date"]), date.fromisoformat(l["end_date"])
    period = fmt_day(s) if s == e else f"{fmt_day(s)} ~ {fmt_day(e)}"
    days = l["days"]
    days_s = f"{days:g}일"
    return f"{LEAVE_KINDS.get(l['kind'], l['kind'])} · {period} · {days_s}"


def approvers_for(ctx: Ctx, uid: str) -> list[str]:
    return [a for a in ctx.cfg.leave_approver_ids if a != uid]


def leave_modal() -> dict:
    opts = [{"text": {"type": "plain_text", "text": v}, "value": k} for k, v in LEAVE_KINDS.items()]
    return {
        "type": "modal", "callback_id": "leave_modal",
        "title": {"type": "plain_text", "text": "휴가 신청"},
        "submit": {"type": "plain_text", "text": "신청"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "input", "block_id": "kind", "label": {"type": "plain_text", "text": "종류"},
             "element": {"type": "static_select", "action_id": "v", "options": opts, "initial_option": opts[0]}},
            {"type": "input", "block_id": "start", "label": {"type": "plain_text", "text": "시작일"},
             "element": {"type": "datepicker", "action_id": "v"}},
            {"type": "input", "block_id": "end", "optional": True,
             "label": {"type": "plain_text", "text": "종료일 (하루면 비워 두세요)"},
             "element": {"type": "datepicker", "action_id": "v"}},
            {"type": "input", "block_id": "reason", "optional": True,
             "label": {"type": "plain_text", "text": "사유"},
             "element": {"type": "plain_text_input", "action_id": "v", "multiline": True, "max_length": 500}},
        ],
    }


def request_leave(ctx: Ctx, uid: str, kind: str, start: date, end: date, reason: str) -> tuple[bool, str]:
    days = count_leave_days(ctx, kind, start, end)
    if days == 0:
        return False, "선택한 기간이 모두 주말·공휴일이에요."
    lid = ctx.db.run("INSERT INTO leaves(user_id, kind, start_date, end_date, days, reason, created_at)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (uid, kind, start.isoformat(), end.isoformat(), days, reason or None,
                      int(ctx.now().timestamp())))
    l = ctx.db.one("SELECT * FROM leaves WHERE id=?", (lid,))
    approvers = approvers_for(ctx, uid)
    if not approvers:  # 승인자가 없거나 본인이 유일한 승인자
        decide_leave(ctx, lid, uid, approve=True, note="자동 승인")
        return True, f"🌴 휴가가 등록됐어요: {leave_text(l)}"
    blocks = [
        section(f"🌴 *휴가 승인 요청*\n<@{uid}> · {leave_text(l)}"
                + (f"\n> {reason}" if reason else "")
                + f"\n_올해 사용: {used_days(ctx, uid, start.year):g}일_"),
        actions(button("승인", "leave_approve", str(lid), style="primary"),
                button("반려", "leave_reject", str(lid), style="danger")),
    ]
    for a in approvers:
        r = ctx.dm(a, f"휴가 승인 요청: {ctx.name(uid)} {leave_text(l)}", blocks)
        if r:
            ctx.db.run("INSERT INTO leave_msgs(leave_id, channel, ts) VALUES (?,?,?)", (lid, r["channel"], r["ts"]))
    return True, f"📨 휴가 신청을 보냈어요 (승인 대기): {leave_text(l)}"


def _update_approver_msgs(ctx: Ctx, lid: int, text: str) -> None:
    for m in ctx.db.all("SELECT * FROM leave_msgs WHERE leave_id=?", (lid,)):
        try:
            ctx.client.chat_update(channel=m["channel"], ts=m["ts"], text=text, blocks=[section(text)])
        except Exception:  # noqa: BLE001
            log.exception("승인 메시지 갱신 실패")


def decide_leave(ctx: Ctx, lid: int, approver: str, approve: bool, note: str = "") -> tuple[bool, str]:
    l = ctx.db.one("SELECT * FROM leaves WHERE id=?", (lid,))
    if not l:
        return False, "휴가 신청을 찾을 수 없어요."
    if l["status"] != "pending":
        who = f" (<@{l['approver_id']}>)" if l["approver_id"] else ""
        return False, f"이미 처리된 신청이에요: {l['status']}{who}"
    if approver != l["user_id"] and approver not in ctx.cfg.leave_approver_ids and not ctx.is_admin(approver):
        return False, "승인 권한이 없어요."
    status = "approved" if approve else "rejected"
    ctx.db.run("UPDATE leaves SET status=?, approver_id=?, decision_note=?, decided_at=? WHERE id=?",
               (status, approver, note or None, int(ctx.now().timestamp()), lid))
    desc = leave_text(l)
    if approve:
        _update_approver_msgs(ctx, lid, f"✅ <@{approver}>님이 승인했어요 · <@{l['user_id']}> {desc}")
        if approver != l["user_id"]:
            ctx.dm(l["user_id"], f"✅ 휴가가 승인됐어요: {desc}")
        ctx.post(ctx.cfg.attendance_channel, f"🌴 <@{l['user_id']}> 휴가 · {desc}")
    else:
        _update_approver_msgs(ctx, lid, f"❌ <@{approver}>님이 반려했어요 · <@{l['user_id']}> {desc}"
                              + (f"\n> {note}" if note else ""))
        ctx.dm(l["user_id"], f"❌ 휴가 신청이 반려됐어요: {desc}" + (f"\n> {note}" if note else ""))
    ctx.refresh_home(l["user_id"])
    return True, "승인했어요." if approve else "반려했어요."


def cancel_leave(ctx: Ctx, lid: int, uid: str) -> tuple[bool, str]:
    l = ctx.db.one("SELECT * FROM leaves WHERE id=?", (lid,))
    if not l or l["user_id"] != uid:
        return False, "본인의 휴가만 취소할 수 있어요."
    if l["status"] not in ("pending", "approved"):
        return False, "이미 처리가 끝난 신청이에요."
    if l["status"] == "approved" and l["end_date"] < ctx.today().isoformat():
        return False, "이미 지난 휴가는 취소할 수 없어요. 관리자에게 문의해 주세요."
    ctx.db.run("UPDATE leaves SET status='cancelled' WHERE id=?", (lid,))
    _update_approver_msgs(ctx, lid, f"↩️ <@{uid}>님이 신청을 취소했어요 · {leave_text(l)}")
    if l["status"] == "approved":
        ctx.post(ctx.cfg.attendance_channel, f"↩️ <@{uid}> 휴가 취소 · {leave_text(l)}")
    ctx.refresh_home(uid)
    return True, f"↩️ 취소했어요: {leave_text(l)}"


def my_leaves_blocks(ctx: Ctx, uid: str) -> list:
    year = ctx.today().year
    rows = ctx.db.all("SELECT * FROM leaves WHERE user_id=? AND (substr(start_date,1,4)=? OR status='pending')"
                      " AND status<>'cancelled' ORDER BY start_date DESC LIMIT 30", (uid, str(year)))
    badge = {"pending": "⏳ 대기", "approved": "✅ 승인", "rejected": "❌ 반려"}
    blocks = [section(f"*🌴 {year}년 내 휴가* · 연차 사용 {used_days(ctx, uid, year):g}일")]
    today = ctx.today().isoformat()
    for l in rows:
        txt = f"{badge.get(l['status'], l['status'])} · {leave_text(l)}"
        if l["status"] == "pending" or (l["status"] == "approved" and l["end_date"] >= today):
            blocks.append(section(txt, button("취소", "leave_cancel", str(l["id"]),
                                              confirm=confirm_dialog("휴가 취소", "이 휴가 신청을 취소할까요?"))))
        else:
            blocks.append(context(txt))
    if not rows:
        blocks.append(context("올해 신청한 휴가가 없어요."))
    blocks.append(actions(button("🌴 휴가 신청", "leave_open")))
    return blocks


def team_leaves_blocks(ctx: Ctx, days: int = 14) -> list:
    s, e = ctx.today(), ctx.today() + timedelta(days=days)
    rows = ctx.db.all("SELECT * FROM leaves WHERE status='approved' AND end_date>=? AND start_date<=?"
                      " ORDER BY start_date", (s.isoformat(), e.isoformat()))
    lines = [f"• <@{l['user_id']}> {leave_text(l)}" for l in rows] or ["예정된 휴가가 없어요."]
    return [section(f"*🗓 앞으로 {days}일 팀 휴가*\n" + "\n".join(lines))]


# ── 현황/리포트 ─────────────────────────────────────────────
def board_blocks(ctx: Ctx, d: date | None = None) -> list:
    d = d or ctx.today()
    groups: dict[str, list[str]] = {k: [] for k in ("office", "remote", "out", "leave", "none")}
    for uid in ctx.members():
        st = status_of(ctx, uid, d)
        if st["state"] == "in":
            r = st["rec"]
            groups[r["mode"]].append(f"<@{uid}> {hm(ctx, st['first_in'])}" + (f" ({r['note']})" if r["note"] else ""))
        elif st["state"] == "out":
            groups["out"].append(f"<@{uid}> {hm(ctx, st['first_in'])}~{hm(ctx, st['rec']['out_at'])}")
        elif st["state"] == "leave":
            groups["leave"].append(f"<@{uid}> ({LEAVE_KINDS.get(st['leave']['kind'])})")
        else:
            groups["none"].append(f"<@{uid}>")
        if st["state"] in ("in", "out") and st.get("leave"):  # 반차 후 출근
            groups["leave"].append(f"<@{uid}> ({LEAVE_KINDS.get(st['leave']['kind'])})")
    title = f"📅 {fmt_day(d)} 근태 현황"
    hol = ctx.holiday_name(d)
    if not ctx.is_workday(d):
        title += f" · {hol or '휴일'}"
    labels = [("office", "🏢 출근"), ("remote", "🏠 재택"),
              ("out", "🏁 퇴근"), ("leave", "🌴 휴가"), ("none", "⏳ 기록 없음")]
    lines = [f"*{lab}* ({len(groups[k])}) {', '.join(groups[k])}" for k, lab in labels if groups[k]]
    if not ctx.cfg.attendance_channel:
        lines.append("_ATTENDANCE_CHANNEL이 설정되지 않아 멤버 목록을 알 수 없어요._")
    return [section(f"*{title}*\n" + ("\n".join(lines) or "기록이 없어요."))]


def period_range(ctx: Ctx, kind: str) -> tuple[date, date, str]:
    t = ctx.today()
    if kind == "month":
        return t.replace(day=1), t, f"{t.month}월"
    start = t - timedelta(days=t.weekday())
    return start, t, f"이번 주 ({fmt_day(start)}~)"


def summarize(ctx: Ctx, uid: str, start: date, end: date) -> dict:
    now = int(ctx.now().timestamp())
    rows = ctx.db.all("SELECT * FROM attendance WHERE user_id=? AND date>=? AND date<=? ORDER BY in_at",
                      (uid, start.isoformat(), end.isoformat()))
    days, secs, first_ins, remote = set(), 0, {}, set()
    missing = 0
    for r in rows:
        days.add(r["date"])
        first_ins.setdefault(r["date"], r["in_at"])
        if r["mode"] == "remote":
            remote.add(r["date"])
        if r["out_at"]:
            secs += r["out_at"] - r["in_at"]
        elif now - r["in_at"] < OPEN_WINDOW:
            secs += now - r["in_at"]
        else:
            missing += 1
    avg_in = None
    if first_ins:
        mins = [ctx.local(v).hour * 60 + ctx.local(v).minute for v in first_ins.values()]
        a = sum(mins) // len(mins)
        avg_in = f"{a // 60:02d}:{a % 60:02d}"
    lv = ctx.db.one("SELECT COALESCE(SUM(days),0) s FROM leaves WHERE user_id=? AND status='approved'"
                    " AND start_date>=? AND start_date<=?", (uid, start.isoformat(), end.isoformat()))
    return {"days": len(days), "secs": secs, "avg_in": avg_in, "remote": len(remote),
            "leave": float(lv["s"]), "missing": missing}


def report_blocks(ctx: Ctx, kind: str, target: str | None) -> list:
    start, end, label = period_range(ctx, kind)
    users = [target] if target else ctx.members()
    lines = []
    for uid in users:
        s = summarize(ctx, uid, start, end)
        line = (f"<@{uid}> · {s['days']}일 · {fmt_duration(s['secs'])}"
                + (f" · 평균 출근 {s['avg_in']}" if s["avg_in"] else "")
                + (f" · 재택 {s['remote']}일" if s["remote"] else "")
                + (f" · 휴가 {s['leave']:g}일" if s["leave"] else "")
                + (f" · ⚠️ 퇴근 누락 {s['missing']}건" if s["missing"] else ""))
        lines.append(line)
    blocks = [section(f"*📊 {label} 근무 요약*")]
    # section 하나당 3000자 제한 → 나눠 담기
    chunk = []
    for ln in lines or ["대상 멤버가 없어요."]:
        if sum(len(x) + 1 for x in chunk) + len(ln) > 2800:
            blocks.append(section("\n".join(chunk)))
            chunk = []
        chunk.append(ln)
    if chunk:
        blocks.append(section("\n".join(chunk)))
    return blocks


def export_csv(ctx: Ctx, year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    rows = ctx.db.all("SELECT * FROM attendance WHERE date>=? AND date<=? ORDER BY date, user_id, in_at",
                      (start.isoformat(), end.isoformat()))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["날짜", "이름", "Slack ID", "형태", "출근", "퇴근", "근무(시간)", "메모"])
    for r in rows:
        hours = round((r["out_at"] - r["in_at"]) / 3600, 2) if r["out_at"] else ""
        w.writerow([r["date"], ctx.name(r["user_id"]), r["user_id"], MODES[r["mode"]][1],
                    hm(ctx, r["in_at"]), hm(ctx, r["out_at"]) if r["out_at"] else "", hours, r["note"] or ""])
    att = "﻿" + buf.getvalue()  # 엑셀 한글 깨짐 방지 BOM

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["이름", "Slack ID", "종류", "시작", "종료", "일수", "상태", "승인자", "사유"])
    for l in ctx.db.all("SELECT * FROM leaves WHERE start_date<=? AND end_date>=? ORDER BY start_date",
                        (end.isoformat(), start.isoformat())):
        w.writerow([ctx.name(l["user_id"]), l["user_id"], LEAVE_KINDS.get(l["kind"], l["kind"]),
                    l["start_date"], l["end_date"], l["days"], l["status"],
                    ctx.name(l["approver_id"]) if l["approver_id"] else "", l["reason"] or ""])
    return att, "﻿" + buf.getvalue()


# ── 리마인더 ────────────────────────────────────────────────
def checkin_reminder_job(ctx: Ctx) -> None:
    d = ctx.today()
    if not ctx.is_workday(d):
        return
    for uid in ctx.members():
        st = status_of(ctx, uid, d)
        if st["state"] != "none":
            continue
        lv = leave_on(ctx, uid, d)
        if lv and lv["kind"] != "pm":
            continue
        ctx.dm(uid, "아직 오늘 출근 기록이 없어요.", [
            section("☀️ 아직 오늘 출근 기록이 없어요. 출근하셨다면 눌러 주세요."),
            actions(button("🏢 출근", "att_in_office", "office", style="primary"),
                    button("🏠 재택", "att_in_remote", "remote"),
                    button("🌴 휴가 신청", "leave_open")),
            context("늦게 기록한다면 `/in 09:10` 처럼 실제 출근 시각을 함께 적을 수 있어요."),
        ])


def checkout_reminder_job(ctx: Ctx) -> None:
    rows = ctx.db.all("SELECT * FROM attendance WHERE date=? AND out_at IS NULL", (ctx.today().isoformat(),))
    for r in rows:
        ctx.dm(r["user_id"], "퇴근 기록을 잊지 않으셨나요?", [
            section(f"🌙 {hm(ctx, r['in_at'])}에 출근한 기록이 아직 열려 있어요. 퇴근하셨나요?"),
            actions(button("🏁 지금 퇴근", "att_out", style="primary")),
            context("이미 퇴근했다면 `/out 18:30` 처럼 실제 퇴근 시각으로 기록해 주세요."),
        ])


# ── Slack 핸들러 ───────────────────────────────────────────
def _reply(ctx: Ctx, body, respond, uid: str, ok: bool, msg: str) -> None:
    """버튼 결과 표시: App Home이면 홈을 새로 그리고(실패 사유는 DM), 메시지면 그 메시지를 바꿉니다."""
    if body.get("container", {}).get("type") == "view":
        ctx.refresh_home(uid)
        if not ok:
            ctx.dm(uid, msg)
        return
    respond(replace_original=True, text=msg)


def register(app, ctx: Ctx) -> None:
    @app.command("/in")
    def cmd_in(ack, command, respond):
        ack()
        text = (command.get("text") or "").strip()
        if text in ("help", "도움말"):
            return respond(HELP)
        mode, note, at = parse_in_args(text)
        respond(clock_in(ctx, command["user_id"], mode, note, at)[1])

    @app.command("/out")
    def cmd_out(ack, command, respond):
        ack()
        text = (command.get("text") or "").strip()
        at, rest = None, []
        for tok in text.split():
            if parse_hhmm(tok) and at is None:
                at = parse_hhmm(tok)
            else:
                rest.append(tok)
        respond(clock_out(ctx, command["user_id"], " ".join(rest), at)[1])

    @app.command("/leave")
    def cmd_leave(ack, command, respond, client):
        ack()
        uid, sub = command["user_id"], (command.get("text") or "").strip().lower()
        if sub in ("list", "my", "mine", "내역", "목록"):
            return respond(text="내 휴가", blocks=my_leaves_blocks(ctx, uid))
        if sub in ("team", "all", "팀"):
            return respond(text="팀 휴가", blocks=team_leaves_blocks(ctx))
        if sub in ("help", "도움말"):
            return respond(HELP)
        if sub not in ("", "new", "request", "신청"):
            return respond(HELP)
        client.views_open(trigger_id=command["trigger_id"], view=leave_modal())

    @app.command("/attendance")
    def cmd_attendance(ack, command, respond, client):
        ack()
        uid, text = command["user_id"], (command.get("text") or "").strip()
        sub, _, rest = text.partition(" ")
        sub = sub.lower()
        m = re.search(r"<@([UW][A-Z0-9]+)", text)
        target = m.group(1) if m else None
        if sub in ("", "today", "오늘"):
            return respond(text="오늘 근태", blocks=board_blocks(ctx))
        if sub in ("week", "weekly", "month", "monthly", "주간", "월간"):
            kind = "month" if sub in ("month", "monthly", "월간") else "week"
            return respond(text="근무 요약", blocks=report_blocks(ctx, kind, target))
        if sub in ("export", "csv", "내보내기"):
            if not ctx.is_admin(uid):
                return respond("CSV 내보내기는 관리자(ADMIN_IDS)만 할 수 있어요.")
            mm = re.search(r"(\d{4})-(\d{1,2})", rest)
            y, mo = (int(mm.group(1)), int(mm.group(2))) if mm else (ctx.today().year, ctx.today().month)
            att, lv = export_csv(ctx, y, mo)
            try:
                dm = client.conversations_open(users=uid)["channel"]["id"]
                client.files_upload_v2(channel=dm, initial_comment=f"📎 {y}년 {mo}월 근태 기록",
                                       file_uploads=[
                                           {"content": att, "filename": f"attendance_{y}-{mo:02d}.csv"},
                                           {"content": lv, "filename": f"leaves_{y}-{mo:02d}.csv"}])
                return respond("DM으로 CSV 파일을 보냈어요.")
            except Exception as e:  # noqa: BLE001
                log.exception("CSV 업로드 실패")
                return respond(f"CSV 업로드에 실패했어요: {e}")
        return respond(HELP)

    @app.action(re.compile(r"^att_in_"))
    def act_in(ack, body, respond):
        ack()
        uid = body["user"]["id"]
        ok, msg = clock_in(ctx, uid, body["actions"][0]["value"])
        _reply(ctx, body, respond, uid, ok, msg)

    @app.action("att_out")
    def act_out(ack, body, respond):
        ack()
        uid = body["user"]["id"]
        ok, msg = clock_out(ctx, uid)
        _reply(ctx, body, respond, uid, ok, msg)

    @app.action("leave_open")
    def act_leave_open(ack, body, client):
        ack()
        if (body.get("view") or {}).get("type") == "modal":
            client.views_push(trigger_id=body["trigger_id"], view=leave_modal())
        else:
            client.views_open(trigger_id=body["trigger_id"], view=leave_modal())

    @app.view("leave_modal")
    def view_leave(ack, body, view):
        uid = body["user"]["id"]
        kind = modal_value(view, "kind") or "annual"
        s = modal_value(view, "start")
        e = modal_value(view, "end") or s
        start, end = date.fromisoformat(s), date.fromisoformat(e)
        if end < start:
            return ack(response_action="errors", errors={"end": "종료일이 시작일보다 빨라요."})
        if kind in ("am", "pm") and end != start:
            return ack(response_action="errors", errors={"end": "반차는 하루만 선택할 수 있어요."})
        if count_leave_days(ctx, kind, start, end) == 0:
            return ack(response_action="errors", errors={"start": "선택한 기간이 모두 주말·공휴일이에요."})
        ack()
        _ok, msg = request_leave(ctx, uid, kind, start, end, modal_value(view, "reason"))
        ctx.dm(uid, msg)

    @app.action("leave_approve")
    def act_approve(ack, body, respond):
        ack()
        ok, msg = decide_leave(ctx, int(body["actions"][0]["value"]), body["user"]["id"], approve=True)
        if not ok:
            respond(replace_original=False, response_type="ephemeral", text=msg)

    @app.action("leave_reject")
    def act_reject(ack, body, client):
        ack()
        client.views_open(trigger_id=body["trigger_id"], view={
            "type": "modal", "callback_id": "leave_reject_modal",
            "private_metadata": json.dumps({"id": int(body["actions"][0]["value"])}),
            "title": {"type": "plain_text", "text": "휴가 반려"},
            "submit": {"type": "plain_text", "text": "반려"},
            "close": {"type": "plain_text", "text": "취소"},
            "blocks": [{"type": "input", "block_id": "note", "optional": True,
                        "label": {"type": "plain_text", "text": "반려 사유 (신청자에게 전달돼요)"},
                        "element": {"type": "plain_text_input", "action_id": "v", "multiline": True}}],
        })

    @app.view("leave_reject_modal")
    def view_reject(ack, body, view):
        ack()
        lid = json.loads(view["private_metadata"])["id"]
        ok, msg = decide_leave(ctx, lid, body["user"]["id"], approve=False, note=modal_value(view, "note"))
        if not ok:
            ctx.dm(body["user"]["id"], msg)

    @app.action("leave_cancel")
    def act_cancel(ack, body, respond, client):
        ack()
        uid = body["user"]["id"]
        _ok, msg = cancel_leave(ctx, int(body["actions"][0]["value"]), uid)
        view = body.get("view") or {}
        if view.get("type") == "modal":  # App Home의 '내 휴가' 창
            client.views_update(view_id=view["id"], view={
                "type": "modal", "title": view["title"], "close": {"type": "plain_text", "text": "닫기"},
                "blocks": [context(msg)] + my_leaves_blocks(ctx, uid)})
        elif view.get("type") == "home":
            ctx.refresh_home(uid)
        else:
            respond(replace_original=True, text=msg, blocks=[context(msg)] + my_leaves_blocks(ctx, uid))
