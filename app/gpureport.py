"""GPU 주간 리포트: 예약 내역과 실제 사용량(사용률·메모리) 비교, Slack 게시, Google Sheet 기록."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from . import gsheet
from .ctx import Ctx, context, section
from .timeparse import WEEKDAY_NAMES

log = logging.getLogger("labbot.gpureport")

LOW_USE_PCT = 30  # 예약 시간 중 실제로 쓴 비율이 이보다 낮으면 ⚠️

SHEET_TABS = {
    "GPU 주간": ["주 시작", "주 끝", "서버", "GPU", "평균 사용률(%)", "평균 메모리(GB)", "메모리 총량(GB)",
               "평균 메모리(%)", "사용 중 비율(%)", "기록 시간(h)"],
    "사용자 주간": ["주 시작", "주 끝", "리눅스 계정", "Slack 이름", "Slack ID", "GPU·시간", "평균 사용률(%)",
                "평균 메모리(GB)", "최대 메모리(GB)", "유휴 점유(h)"],
    "예약 내역": ["주 시작", "예약 ID", "예약자", "Slack ID", "서버", "GPU", "시작", "종료(실제)",
              "예약 GPU·시간(이번 주)", "평균 사용률(%)", "평균 메모리(GB)", "사용 중 비율(%)",
              "본인 사용 GPU·시간", "메모"],
}


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _fmt_day(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})"


def _r(x, n: int = 1):
    return round(x, n) if x is not None else ""


class Report:
    """[start, end) 기간의 GPU 사용 통계. end는 지난 시각이면 그대로, 미래면 지금까지만 집계됩니다."""

    def __init__(self, ctx: Ctx, mon, start: date, end: date):
        self.ctx, self.mon, self.start, self.end = ctx, mon, start, end
        self.s = int(datetime.combine(start, time(0), ctx.tz).timestamp())
        self.e = min(int(datetime.combine(end, time(0), ctx.tz).timestamp()), int(ctx.now().timestamp()))
        self.gpus = self._gpus()
        self.users = self._users()
        self.reservations = self._reservations()

    # ── 집계 ────────────────────────────────────────────────
    def _busy_case(self, util: str = "util", mem: str = "mem_used") -> str:
        c = self.ctx.cfg
        return (f"CASE WHEN COALESCE({util},0)>={int(c.free_gpu_util)} OR COALESCE({mem},0)>={int(c.free_gpu_mem_mb)}"
                f" THEN weight ELSE 0 END")

    def _gpus(self) -> list[dict]:
        rows = self.ctx.db.all(
            "SELECT host, gpu, SUM(weight) w, SUM(COALESCE(util,0)*weight) uw, SUM(COALESCE(mem_used,0)*weight) mw,"
            f" MAX(mem_total) mt, SUM({self._busy_case()}) busy"
            " FROM gpu_samples WHERE ts>=? AND ts<? GROUP BY host, gpu ORDER BY host, gpu", (self.s, self.e))
        out = []
        for r in rows:
            w = r["w"] or 1
            mem = r["mw"] / w
            out.append({"host": r["host"], "gpu": r["gpu"], "hours": r["w"] / 3600, "util": r["uw"] / w,
                        "mem_mb": mem, "mem_total_mb": r["mt"] or 0,
                        "mem_pct": mem / r["mt"] * 100 if r["mt"] else None, "busy_pct": r["busy"] / w * 100})
        return out

    def _users(self) -> list[dict]:
        c = self.ctx.cfg
        rows = self.ctx.db.all(
            "SELECT us.user, SUM(us.weight) w, SUM(COALESCE(gs.util,0)*us.weight) uw,"
            " SUM(COALESCE(us.mem_mb,0)*us.weight) mw, MAX(us.mem_mb) peak,"
            " SUM(CASE WHEN COALESCE(gs.util,0)<=? AND us.mem_mb>=? THEN us.weight ELSE 0 END) idle"
            " FROM gpu_user_samples us JOIN gpu_samples gs ON gs.ts=us.ts AND gs.host=us.host AND gs.gpu=us.gpu"
            " WHERE us.ts>=? AND us.ts<? GROUP BY us.user ORDER BY w DESC",
            (c.idle_gpu_util, c.idle_gpu_min_mem_mb, self.s, self.e))
        return [{"user": r["user"], "slack_id": self.mon.link_of(r["user"]), "gpu_h": r["w"] / 3600,
                 "util": r["uw"] / r["w"], "mem_mb": r["mw"] / r["w"], "peak_mb": r["peak"] or 0,
                 "idle_h": r["idle"] / 3600} for r in rows if r["w"]]

    def _reservations(self) -> list[dict]:
        db = self.ctx.db
        rows = db.all("SELECT * FROM gpu_reservations WHERE start_at<? AND"
                      " MIN(end_at, COALESCE(released_at, end_at))>? ORDER BY start_at", (self.e, self.s))
        out = []
        for r in rows:
            end = min(r["end_at"], r["released_at"] or r["end_at"])
            a, b = max(r["start_at"], self.s), min(end, self.e)
            if b <= a:
                continue
            mine = self.mon.linux_users_of(r["user_id"])
            secs = w = uw = mw = busy = own = 0.0
            for g in map(int, r["gpus"].split(",")):
                secs += b - a
                st = db.one("SELECT SUM(weight) w, SUM(COALESCE(util,0)*weight) uw,"
                            f" SUM(COALESCE(mem_used,0)*weight) mw, SUM({self._busy_case()}) busy"
                            " FROM gpu_samples WHERE host=? AND gpu=? AND ts>=? AND ts<?", (r["host"], g, a, b))
                w += st["w"] or 0
                uw += st["uw"] or 0
                mw += st["mw"] or 0
                busy += st["busy"] or 0
                if mine:
                    marks = ",".join("?" * len(mine))
                    o = db.one("SELECT SUM(weight) w FROM (SELECT DISTINCT ts, weight FROM gpu_user_samples"
                               f" WHERE host=? AND gpu=? AND ts>=? AND ts<? AND user IN ({marks}))",
                               (r["host"], g, a, b, *mine))
                    own += o["w"] or 0
            out.append({"id": r["id"], "user_id": r["user_id"], "host": r["host"], "gpus": r["gpus"],
                        "start": r["start_at"], "end": end, "note": r["note"] or "",
                        "reserved_h": secs / 3600, "util": uw / w if w else None,
                        "mem_mb": mw / w if w else None, "busy_pct": busy / secs * 100 if secs else 0,
                        "busy_h": busy / 3600, "own_h": own / 3600 if mine else None})
        return out

    # ── Slack ───────────────────────────────────────────────
    def title(self) -> str:
        last = self.end - timedelta(days=1)
        return f"📈 {_fmt_day(self.start)}~{_fmt_day(last)} 주간 GPU 리포트"

    def blocks(self, sheet_url: str = "") -> list:
        ctx, mon = self.ctx, self.mon
        if not self.gpus:
            return [section(f"*{self.title()}*\n이 기간의 GPU 기록이 없어요.")]
        tot_h = sum(g["hours"] for g in self.gpus) or 1
        util = sum(g["util"] * g["hours"] for g in self.gpus) / tot_h
        busy = sum(g["busy_pct"] * g["hours"] for g in self.gpus) / tot_h
        mem_tot = sum(g["mem_total_mb"] * g["hours"] for g in self.gpus)
        mem = sum(g["mem_mb"] * g["hours"] for g in self.gpus) / mem_tot * 100 if mem_tot else 0
        res_h = sum(r["reserved_h"] for r in self.reservations)
        head = (f"*{self.title()}*\n"
                f"*평균 사용률 {util:.0f}%* · 메모리 {mem:.0f}% · 사용 중이던 시간 {busy:.0f}%\n"
                f"사용자 GPU·시간 {sum(u['gpu_h'] for u in self.users):.0f}h · "
                f"예약 {len(self.reservations)}건 {res_h:.0f} GPU·시간")
        blocks = [section(head)]

        by_host: dict[str, list] = defaultdict(list)
        for g in self.gpus:
            by_host[g["host"]].append(g)
        lines = []
        for host, gs in by_host.items():
            h = sum(g["hours"] for g in gs) or 1
            per = " ".join(f"{g['gpu']}:{g['util']:.0f}%" for g in gs)
            lines.append(f"• *{host}* 사용률 {sum(g['util'] * g['hours'] for g in gs) / h:.0f}% · "
                         f"사용 중 {sum(g['busy_pct'] * g['hours'] for g in gs) / h:.0f}% `{per}`")
        blocks.append(section("*🖥 서버별 (GPU번호:평균 사용률)*\n" + "\n".join(lines)))

        if self.users:
            ul = []
            for u in self.users[:15]:
                ln = (f"• {mon.who(u['user'])} {u['gpu_h']:.1f} GPU·시간 · 사용률 {u['util']:.0f}% · "
                      f"메모리 평균 {u['mem_mb'] / 1024:.1f}GB (최대 {u['peak_mb'] / 1024:.1f}GB)")
                if u["idle_h"] >= 1:
                    ln += f" · 💤 {u['idle_h']:.1f}h"
                ul.append(ln)
            blocks.append(section("*👥 사용자별 실제 사용량*\n" + "\n".join(ul)))

        if self.reservations:
            agg: dict[str, dict] = {}
            for r in self.reservations:
                a = agg.setdefault(r["user_id"], {"n": 0, "h": 0.0, "busy_h": 0.0, "uw": 0.0, "uh": 0.0,
                                                  "own": None})
                a["n"] += 1
                a["h"] += r["reserved_h"]
                a["busy_h"] += r["busy_h"]
                if r["util"] is not None:
                    a["uw"] += r["util"] * r["reserved_h"]
                    a["uh"] += r["reserved_h"]
                if r["own_h"] is not None:
                    a["own"] = (a["own"] or 0) + r["own_h"]
            rl = []
            for uid, a in sorted(agg.items(), key=lambda kv: -kv[1]["h"])[:15]:
                pct = a["busy_h"] / a["h"] * 100 if a["h"] else 0
                ln = (f"• {ctx.name(uid)} {a['n']}건 {a['h']:.1f} GPU·시간 → 실제 사용 {pct:.0f}%"
                      + (f" · 사용률 {a['uw'] / a['uh']:.0f}%" if a["uh"] else ""))
                if a["own"] is not None:
                    ln += f" · 본인 프로세스 {a['own']:.1f}h"
                if pct < LOW_USE_PCT:
                    ln += " ⚠️"
                rl.append(ln)
            blocks.append(section("*🔒 예약 vs 실제 사용*\n" + "\n".join(rl)))

        note = (f"'사용 중'은 사용률 {ctx.cfg.free_gpu_util}% 이상이거나 메모리 {ctx.cfg.free_gpu_mem_mb}MB 이상인 시간이에요."
                f" ⚠️ 예약 시간의 {LOW_USE_PCT}% 미만만 사용")
        if sheet_url:
            note += f" · <{sheet_url}|📄 전체 내역 (Google Sheet)>"
        blocks.append(context(note))
        return blocks

    # ── Google Sheet ────────────────────────────────────────
    def sheet_rows(self) -> dict[str, list[list]]:
        ctx = self.ctx
        ws, we = self.start.isoformat(), (self.end - timedelta(days=1)).isoformat()

        def ts(epoch: int) -> str:
            return f"{ctx.local(epoch):%Y-%m-%d %H:%M}"
        return {
            "GPU 주간": [[ws, we, g["host"], g["gpu"], _r(g["util"]), _r(g["mem_mb"] / 1024),
                        _r(g["mem_total_mb"] / 1024), _r(g["mem_pct"]), _r(g["busy_pct"]), _r(g["hours"])]
                       for g in self.gpus],
            "사용자 주간": [[ws, we, u["user"], ctx.name(u["slack_id"]) if u["slack_id"] else "", u["slack_id"] or "",
                         _r(u["gpu_h"], 2), _r(u["util"]), _r(u["mem_mb"] / 1024, 2), _r(u["peak_mb"] / 1024, 2),
                         _r(u["idle_h"], 2)] for u in self.users],
            "예약 내역": [[ws, r["id"], ctx.name(r["user_id"]), r["user_id"], r["host"], r["gpus"], ts(r["start"]),
                       ts(r["end"]), _r(r["reserved_h"], 2), _r(r["util"]),
                       _r(r["mem_mb"] / 1024 if r["mem_mb"] is not None else None, 2), _r(r["busy_pct"]),
                       _r(r["own_h"], 2), r["note"]] for r in self.reservations],
        }


def sheet_url(ctx: Ctx) -> str:
    return gsheet.url(ctx.cfg.gsheet_id)


def _open_sheet(ctx: Ctx):
    """서비스 계정으로 스프레드시트를 엽니다. 테스트에서 가짜로 바꿔 끼웁니다."""
    return gsheet.open_sheet(ctx, ctx.cfg.gsheet_id)


def write_sheet(ctx: Ctx, rep: Report, force: bool = False) -> str:
    """주간 통계를 시트 탭마다 행으로 추가합니다. 같은 주를 두 번 쓰지 않습니다."""
    if not (ctx.cfg.gsheet_id and ctx.cfg.google_sa_file):
        return "Google Sheet가 설정되지 않았어요 (`GSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE`)."
    key = rep.start.isoformat()
    if not force and ctx.db.get_setting("gsheet_week") >= key:
        return f"{key} 주는 이미 시트에 기록했어요."
    sh = _open_sheet(ctx)
    titles = {ws.title: ws for ws in sh.worksheets()}
    n = 0
    for tab, rows in rep.sheet_rows().items():
        ws = titles.get(tab)
        if ws is None:
            ws = sh.add_worksheet(title=tab, rows=1000, cols=len(SHEET_TABS[tab]))
            ws.append_row(SHEET_TABS[tab])
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            n += len(rows)
    ctx.db.set_setting("gsheet_week", key)
    return f"📄 Google Sheet에 {n}행을 기록했어요."


def last_week(ctx: Ctx) -> tuple[date, date]:
    end = week_start(ctx.today())
    return end - timedelta(days=7), end


def weekly_job(ctx: Ctx, mon) -> None:
    start, end = last_week(ctx)
    rep = Report(ctx, mon, start, end)
    ctx.post(ctx.cfg.server_channel, "주간 GPU 리포트", rep.blocks(sheet_url(ctx)))
    if ctx.cfg.gsheet_id:
        try:
            log.info(write_sheet(ctx, rep))
        except Exception:  # noqa: BLE001
            log.exception("Google Sheet 기록 실패")
