"""서버 모니터링: 에이전트가 보내는 지표 수신, /gpu·/server 현황, GPU 예약, 알림, 일일 요약."""
from __future__ import annotations

import json
import logging
import re
import threading
import time as _time
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import gpureport
from .ctx import Ctx, context, divider, section
from .timeparse import WEEKDAY_NAMES, fmt_duration, parse_duration

log = logging.getLogger("labbot.servers")

HELP = """*🖥 서버 명령어*
• `/gpu` 전체 GPU 현황 · `/gpu free` 비어 있는 GPU만
• `/gpu reserve gpu1 0,1 3h paper experiment` — GPU 예약 (`0-3`, `all` 가능 / 기간 `90m` `3h` `2d`)
• `/gpu release` 내 예약 모두 해제 · `/gpu release 12` · `/gpu mine` 내 예약 보기
• `/gpu link kim` — 내 Slack 계정을 리눅스 계정 `kim`과 연결 (알림·통계에 사용)
• `/gpu forget old-name` — 이름을 바꿨거나 없앤 서버를 현황에서 지우기 (관리자)
• `/gpu summary` 어제 사용량 요약 · `/gpu today` 오늘 · `/server` CPU·메모리·디스크 현황
• `/gpu week` 지난주 리포트 (예약 vs 실제 사용) · `/gpu week now` 이번 주 · `/gpu week sheet` 지난주를 Google Sheet에 기록 (관리자)"""

MAX_RESERVE = timedelta(days=7)


def bar(pct: float, width: int = 10) -> str:
    n = max(0, min(width, round(pct / 100 * width)))
    return "▓" * n + "░" * (width - n)


def gb(mb: float) -> str:
    return f"{mb / 1024:.0f}" if mb >= 10240 else f"{mb / 1024:.1f}"


def parse_gpu_list(spec: str, available: list[int] | None) -> list[int] | None:
    spec = spec.strip().lower()
    if spec in ("all", "전체"):
        return sorted(available) if available else None
    out: set[int] = set()
    for part in spec.split(","):
        if re.fullmatch(r"\d+-\d+", part):
            a, b = map(int, part.split("-"))
            out.update(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            return None
    if available is not None and not out <= set(available):
        return None
    return sorted(out)


class Monitor:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.lock = threading.RLock()
        self.latest: dict[str, dict] = {}            # host -> {"recv": epoch, "data": payload}
        self.idle: dict[tuple, dict] = {}            # (host, gpu, user) -> {"since": ts, "alerted": bool}
        self.offline_alerted: set[str] = set()
        self.disk_alerted: dict[tuple, float] = {}   # (host, mount) -> 마지막 알림 시각
        self.intruder_alerted: set[tuple] = set()    # (reservation id, linux user)
        self.listeners: list = []                    # fn(host, payload): 지표를 받을 때마다 (작업 GPU 집계 등)
        self.jobs = None                             # jobs.Tracker (POST /jobs 처리)

    # ── 수신 ────────────────────────────────────────────────
    def ingest(self, data: dict) -> None:
        host = str(data.get("host", "")).strip()
        if not host or len(host) > 64:
            raise ValueError("host 필드가 없어요")
        now = int(_time.time())
        interval = max(10, min(600, int(data.get("interval", 60))))
        gpus = data.get("gpus") or []
        with self.lock:
            self.latest[host] = {"recv": now, "data": data}
        g_rows, u_rows = [], []
        cfg = self.ctx.cfg
        seen_idle = set()
        for g in gpus:
            idx = int(g["index"])
            g_rows.append((now, host, idx, g.get("util"), g.get("mem_used"), g.get("mem_total"), interval))
            per_user: dict[str, float] = defaultdict(float)
            for p in g.get("procs") or []:
                per_user[p.get("user") or "?"] += float(p.get("mem_mb") or 0)
            for user, mem in per_user.items():
                u_rows.append((now, host, idx, user, mem, interval))
                if (g.get("util") or 0) <= cfg.idle_gpu_util and mem >= cfg.idle_gpu_min_mem_mb:
                    key = (host, idx, user)
                    seen_idle.add(key)
                    with self.lock:
                        self.idle.setdefault(key, {"since": now, "alerted": False})
        with self.lock:
            for key in [k for k in self.idle if k[0] == host and k not in seen_idle]:
                del self.idle[key]
        if g_rows:
            self.ctx.db.many("INSERT INTO gpu_samples VALUES (?,?,?,?,?,?,?)", g_rows)
        if u_rows:
            self.ctx.db.many("INSERT INTO gpu_user_samples VALUES (?,?,?,?,?,?)", u_rows)
        for fn in self.listeners:
            try:
                fn(host, data)
            except Exception:  # noqa: BLE001
                log.exception("지표 처리 실패: %s", getattr(fn, "__name__", fn))

    # ── 조회 ────────────────────────────────────────────────
    def hosts(self) -> list[tuple[str, dict, bool]]:
        now = _time.time()
        with self.lock:
            items = sorted(self.latest.items())
        return [(h, v["data"], now - v["recv"] <= self.ctx.cfg.offline_minutes * 60) for h, v in items]

    def forget(self, host: str) -> bool:
        """현황 목록과 알림 상태에서 서버를 지웁니다. 지난 사용 기록(통계)은 남깁니다."""
        with self.lock:
            if self.latest.pop(host, None) is None:
                return False
            for key in [k for k in self.idle if k[0] == host]:
                del self.idle[key]
            self.offline_alerted.discard(host)
            for key in [k for k in self.disk_alerted if k[0] == host]:
                del self.disk_alerted[key]
        return True

    def link_of(self, linux_user: str) -> str | None:
        r = self.ctx.db.one("SELECT slack_id FROM user_links WHERE linux_user=?", (linux_user,))
        return r["slack_id"] if r else None

    def linux_users_of(self, slack_id: str) -> list[str]:
        return [r["linux_user"] for r in self.ctx.db.all("SELECT linux_user FROM user_links WHERE slack_id=?",
                                                         (slack_id,))]

    def who(self, linux_user: str) -> str:
        """채널에서 멘션 알림이 가지 않도록 이름만 표기"""
        sid = self.link_of(linux_user)
        return f"{linux_user}({self.ctx.name(sid)})" if sid else linux_user

    def active_reservations(self, host: str | None = None):
        now = int(_time.time())
        sql = "SELECT * FROM gpu_reservations WHERE released=0 AND end_at>? AND start_at<=?"
        params: tuple = (now, now)
        if host:
            sql += " AND host=?"
            params += (host,)
        return self.ctx.db.all(sql + " ORDER BY host, id", params)

    def reserved_map(self) -> dict[tuple[str, int], object]:
        out = {}
        for r in self.active_reservations():
            for g in map(int, r["gpus"].split(",")):
                out[(r["host"], g)] = r
        return out

    def is_free(self, g: dict, reserved: bool) -> bool:
        c = self.ctx.cfg
        return (not reserved and (g.get("util") or 0) < c.free_gpu_util
                and (g.get("mem_used") or 0) < c.free_gpu_mem_mb)

    def gpu_counts(self) -> tuple[int, int]:
        res = self.reserved_map()
        free = total = 0
        for host, data, online in self.hosts():
            if not online:
                continue
            for g in data.get("gpus") or []:
                total += 1
                free += self.is_free(g, (host, int(g["index"])) in res)
        return free, total

    # ── 표시 ────────────────────────────────────────────────
    def gpu_blocks(self, only_free: bool = False) -> list:
        res = self.reserved_map()
        blocks, free_total, gpu_total = [], 0, 0
        for host, data, online in self.hosts():
            gpus = data.get("gpus") or []
            if not gpus:
                continue
            if not online:
                blocks.append(section(f"*🖥 {host}* · 🔴 응답 없음 (마지막 {self._last_seen(host)})"))
                continue
            lines = []
            for g in gpus:
                idx = int(g["index"])
                gpu_total += 1
                r = res.get((host, idx))
                free = self.is_free(g, r is not None)
                free_total += free
                if only_free and not free:
                    continue
                util, used, tot = g.get("util") or 0, g.get("mem_used") or 0, g.get("mem_total") or 0
                users = sorted({p.get("user") or "?" for p in g.get("procs") or []})
                state = "🟢" if free else ("🔒" if r is not None and not users else "🟠")
                line = (f"{state} `GPU{idx}` {bar(util)} {util:.0f}% · {gb(used)}/{gb(tot)}GB"
                        + (f" · {g['temp']:.0f}°C" if g.get("temp") is not None else ""))
                if users:
                    line += " · " + ", ".join(self.who(u) for u in users)
                if r is not None:
                    line += f" · 🔒 {self.ctx.name(r['user_id'])} ~{self.ctx.local(r['end_at']):%m/%d %H:%M}"
                    if r["note"]:
                        line += f" ({r['note']})"
                lines.append(line)
            if lines:
                name = gpus[0].get("name", "")
                blocks.append(section(f"*🖥 {host}* · {name}\n" + "\n".join(lines)))
        if not blocks:
            msg = "비어 있는 GPU가 없어요. 😢" if only_free and gpu_total else "GPU 서버에서 받은 데이터가 아직 없어요."
            blocks.append(section(msg))
        blocks.insert(0, section(f"*🎛 GPU 현황* · 🟢 사용 가능 {free_total}/{gpu_total}"))
        blocks.append(context("🟢 비어 있음 · 🟠 사용 중 · 🔒 예약됨 | `/gpu reserve 서버 GPU번호 기간` 으로 예약"))
        return blocks

    def _last_seen(self, host: str) -> str:
        with self.lock:
            recv = self.latest.get(host, {}).get("recv")
        return f"{self.ctx.local(recv):%m/%d %H:%M}" if recv else "-"

    def server_blocks(self) -> list:
        blocks = [section("*🖥 서버 현황*")]
        hosts = self.hosts()
        if not hosts:
            return [section("아직 어떤 서버에서도 데이터를 받지 못했어요. 에이전트 설치를 확인해 주세요.")]
        for host, d, online in hosts:
            if not online:
                blocks.append(section(f"*{host}* · 🔴 응답 없음 (마지막 {self._last_seen(host)})"))
                continue
            cpu, mem = d.get("cpu") or {}, d.get("mem") or {}
            parts = []
            if cpu:
                parts.append(f"CPU {cpu.get('pct', 0):.0f}% ({cpu.get('cores', '?')}코어, load {cpu.get('load1', 0):.1f})")
            if mem.get("total_mb"):
                pct = mem["used_mb"] / mem["total_mb"] * 100
                parts.append(f"RAM {gb(mem['used_mb'])}/{gb(mem['total_mb'])}GB ({pct:.0f}%)")
            if d.get("gpus"):
                parts.append(f"GPU {len(d['gpus'])}장")
            disk_lines = []
            for dk in d.get("disks") or []:
                pct = dk["used_gb"] / dk["total_gb"] * 100 if dk.get("total_gb") else 0
                warn = " ⚠️" if pct >= self.ctx.cfg.disk_alert_pct else ""
                disk_lines.append(f"`{dk['mount']}` {bar(pct)} {pct:.0f}% · "
                                  f"{_size(dk['used_gb'])}/{_size(dk['total_gb'])}{warn}")
            txt = f"*{host}* · " + " · ".join(parts)
            if disk_lines:
                txt += "\n" + "\n".join(disk_lines)
            blocks.append(section(txt))
        return blocks

    # ── 요약 ────────────────────────────────────────────────
    def summary_blocks(self, day: date) -> list:
        ctx = self.ctx
        s = int(datetime.combine(day, time(0), ctx.tz).timestamp())
        e = s + 86400
        title = f"📊 {day.month}/{day.day}({WEEKDAY_NAMES[day.weekday()]}) 서버 사용 요약"
        g = ctx.db.all("SELECT host, gpu, SUM(util*weight)*1.0/SUM(weight) u FROM gpu_samples"
                       " WHERE ts>=? AND ts<? GROUP BY host, gpu ORDER BY host, gpu", (s, e))
        if not g:
            return [section(f"*{title}*\n이 날짜의 GPU 기록이 없어요.")] + self._disk_summary()
        by_host: dict[str, list] = defaultdict(list)
        for r in g:
            by_host[r["host"]].append(r)
        lines = []
        for host, rows in by_host.items():
            avg = sum(r["u"] for r in rows) / len(rows)
            per = " ".join(f"{r['gpu']}:{r['u']:.0f}%" for r in rows)
            lines.append(f"• *{host}* 평균 {avg:.0f}% `{per}`")
        fleet = sum(r["u"] for r in g) / len(g)

        users = ctx.db.all("SELECT user, SUM(weight)/3600.0 h FROM gpu_user_samples WHERE ts>=? AND ts<?"
                           " GROUP BY user ORDER BY h DESC", (s, e))
        idle = {r["user"]: r["h"] for r in ctx.db.all(
            "SELECT us.user, SUM(us.weight)/3600.0 h FROM gpu_user_samples us JOIN gpu_samples gs"
            " ON gs.ts=us.ts AND gs.host=us.host AND gs.gpu=us.gpu"
            " WHERE us.ts>=? AND us.ts<? AND gs.util<=? AND us.mem_mb>=? GROUP BY us.user",
            (s, e, ctx.cfg.idle_gpu_util, ctx.cfg.idle_gpu_min_mem_mb))}
        ulines = []
        for r in users[:15]:
            ln = f"• {self.who(r['user'])} {r['h']:.1f} GPU·시간"
            if idle.get(r["user"], 0) >= 0.5:
                ln += f" (그중 놀고 있던 시간 {idle[r['user']]:.1f}h 💤)"
            ulines.append(ln)
        blocks = [section(f"*{title}*\n*GPU 평균 사용률 {fleet:.0f}%*\n" + "\n".join(lines))]
        if ulines:
            blocks.append(section("*👥 사용자별 GPU 사용 시간*\n" + "\n".join(ulines)))
        return blocks + self._disk_summary()

    def _disk_summary(self) -> list:
        lines = []
        for host, d, online in self.hosts():
            for dk in d.get("disks") or []:
                if not dk.get("total_gb"):
                    continue
                pct = dk["used_gb"] / dk["total_gb"] * 100
                warn = " ⚠️" if pct >= self.ctx.cfg.disk_alert_pct else ""
                lines.append(f"• {host} `{dk['mount']}` {pct:.0f}% (남은 공간 {_size(dk['total_gb'] - dk['used_gb'])}){warn}")
        return [section("*💾 디스크*\n" + "\n".join(lines))] if lines else []

    # ── 알림 (1분마다) ──────────────────────────────────────
    def watch(self) -> None:
        ctx, cfg = self.ctx, self.ctx.cfg
        ch = cfg.server_channel
        now = _time.time()
        for host, d, online in self.hosts():
            if not online and host not in self.offline_alerted:
                self.offline_alerted.add(host)
                ctx.post(ch, f"🔴 *{host}* 에서 {cfg.offline_minutes}분 넘게 데이터가 오지 않아요 "
                             f"(마지막 {self._last_seen(host)}). 서버나 에이전트 상태를 확인해 주세요.")
            elif online and host in self.offline_alerted:
                self.offline_alerted.discard(host)
                ctx.post(ch, f"✅ *{host}* 다시 연결됐어요.")
            if not online:
                continue
            for dk in d.get("disks") or []:
                if not dk.get("total_gb"):
                    continue
                pct = dk["used_gb"] / dk["total_gb"] * 100
                key = (host, dk["mount"])
                if pct >= cfg.disk_alert_pct and now - self.disk_alerted.get(key, 0) > 6 * 3600:
                    self.disk_alerted[key] = now
                    ctx.post(ch, f"💾 *{host}* `{dk['mount']}` 디스크 {pct:.0f}% 사용 중 "
                                 f"(남은 공간 {_size(dk['total_gb'] - dk['used_gb'])}). 정리가 필요해요!")
                elif pct < cfg.disk_alert_pct - 5:
                    self.disk_alerted.pop(key, None)

        # GPU를 잡고 놀고 있는 프로세스
        with self.lock:
            due = [(k, v) for k, v in self.idle.items()
                   if not v["alerted"] and now - v["since"] >= cfg.idle_gpu_minutes * 60]
            for _k, v in due:
                v["alerted"] = True
        for (host, idx, user), v in due:
            mins = int((now - v["since"]) // 60)
            msg = (f"💤 *{host}* `GPU{idx}` 를 {self.who(user)} 님이 {fmt_duration(mins * 60)}째 "
                   f"메모리만 점유하고 거의 사용하지 않고 있어요 (사용률 ≤{cfg.idle_gpu_util}%). "
                   f"끝난 작업이면 종료해 주세요!")
            ctx.post(ch, msg)
            sid = self.link_of(user)
            if sid:
                ctx.dm(sid, msg + "\n주피터 커널이나 디버거가 남아 있지 않은지 확인해 보세요.")

        # 예약된 GPU를 다른 사람이 쓰는 경우
        for host, d, online in self.hosts():
            if not online:
                continue
            for g in d.get("gpus") or []:
                r = self.reserved_map().get((host, int(g["index"])))
                if r is None:
                    continue
                mine = set(self.linux_users_of(r["user_id"]))
                if not mine:
                    continue  # 예약자 계정이 연결되지 않으면 판단 불가
                for p in g.get("procs") or []:
                    u = p.get("user") or "?"
                    key = (r["id"], u)
                    if u not in mine and key not in self.intruder_alerted:
                        self.intruder_alerted.add(key)
                        ctx.post(ch, f"🔒 *{host}* `GPU{g['index']}` 는 {ctx.name(r['user_id'])} 님이 "
                                     f"~{ctx.local(r['end_at']):%H:%M}까지 예약한 GPU인데 {self.who(u)} 님의 "
                                     f"프로세스가 실행 중이에요.")

        # 끝난 예약 알림
        ended = ctx.db.all("SELECT * FROM gpu_reservations WHERE released=0 AND end_at<=?", (int(now),))
        for r in ended:
            ctx.db.run("UPDATE gpu_reservations SET released=1, released_at=end_at WHERE id=?", (r["id"],))
            ctx.dm(r["user_id"], f"⌛ {r['host']} GPU {r['gpus']} 예약이 끝났어요. "
                                 f"계속 써야 하면 `/gpu reserve` 로 다시 예약해 주세요.")

    def daily_summary_job(self) -> None:
        y = self.ctx.today() - timedelta(days=1)
        self.ctx.post(self.ctx.cfg.server_channel, "어제 서버 사용 요약", self.summary_blocks(y))

    def prune_job(self) -> None:
        cutoff = int(_time.time()) - self.ctx.cfg.retention_days * 86400
        self.ctx.db.run("DELETE FROM gpu_samples WHERE ts<?", (cutoff,))
        self.ctx.db.run("DELETE FROM gpu_user_samples WHERE ts<?", (cutoff,))

    # ── 예약 ────────────────────────────────────────────────
    def reserve(self, uid: str, args: list[str]) -> str:
        if len(args) < 3:
            return "형식: `/gpu reserve gpu1 0,1 3h 메모`"
        host, spec, dur_s, note = args[0], args[1], args[2], " ".join(args[3:])
        with self.lock:
            known = self.latest.get(host)
        if not known:
            names = ", ".join(h for h, d, _ in self.hosts() if d.get("gpus")) or "없음"
            return f"`{host}` 서버를 찾을 수 없어요. 등록된 GPU 서버: {names}"
        available = [int(g["index"]) for g in known["data"].get("gpus") or []]
        gpus = parse_gpu_list(spec, available)
        if not gpus:
            return f"GPU 번호를 확인해 주세요. {host}에는 {', '.join(map(str, available))}번이 있어요."
        dur = parse_duration(dur_s)
        if not dur:
            return "기간 형식: `90m`, `3h`, `2d`, `1h30m`"
        if dur > MAX_RESERVE:
            return "한 번에 최대 7일까지 예약할 수 있어요."
        now = int(_time.time())
        for r in self.active_reservations(host):
            clash = set(gpus) & set(map(int, r["gpus"].split(",")))
            if clash and r["user_id"] != uid:
                return (f"GPU {','.join(map(str, sorted(clash)))}번은 {self.ctx.name(r['user_id'])} 님이 "
                        f"{self.ctx.local(r['end_at']):%m/%d %H:%M}까지 예약했어요.")
        rid = self.ctx.db.run("INSERT INTO gpu_reservations(host, gpus, user_id, start_at, end_at, note)"
                              " VALUES (?,?,?,?,?,?)",
                              (host, ",".join(map(str, gpus)), uid, now, now + int(dur.total_seconds()), note or None))
        end = self.ctx.local(now + int(dur.total_seconds()))
        busy = [g for g in known["data"].get("gpus") or []
                if int(g["index"]) in gpus and not self.is_free(g, False)]
        warn = ""
        if busy:
            warn = "\n⚠️ 지금 사용 중인 GPU가 있어요: " + ", ".join(f"GPU{g['index']}" for g in busy)
        if not self.linux_users_of(uid):
            warn += "\n💡 `/gpu link &lt;linux-user&gt;` 으로 계정을 연결하면 다른 사람이 예약 GPU를 쓸 때 알려 드려요."
        self.ctx.post(self.ctx.cfg.server_channel,
                      f"🔒 {self.ctx.name(uid)} 님이 *{host}* GPU {','.join(map(str, gpus))} 예약 "
                      f"(~{end:%m/%d %H:%M})" + (f" · {note}" if note else ""))
        return f"🔒 예약했어요 `#{rid}` · {host} GPU {','.join(map(str, gpus))} · ~{end:%m/%d %H:%M}{warn}"

    def release(self, uid: str, rid: int | None) -> str:
        if rid:
            r = self.ctx.db.one("SELECT * FROM gpu_reservations WHERE id=? AND released=0", (rid,))
            if not r:
                return f"`#{rid}` 활성 예약이 없어요."
            if r["user_id"] != uid and not self.ctx.is_admin(uid):
                return "본인 예약만 해제할 수 있어요."
            self.ctx.db.run("UPDATE gpu_reservations SET released=1, released_at=? WHERE id=?",
                            (int(_time.time()), rid))
            return f"🔓 `#{rid}` 예약을 해제했어요."
        n = self.ctx.db.run("UPDATE gpu_reservations SET released=1, released_at=? WHERE user_id=? AND released=0",
                            (int(_time.time()), uid))
        return f"🔓 예약 {n}건을 해제했어요." if n else "해제할 예약이 없어요."

    def my_reservations_text(self, uid: str) -> str:
        rows = [r for r in self.active_reservations() if r["user_id"] == uid]
        return "\n".join(f"🔒 `#{r['id']}` {r['host']} GPU {r['gpus']} ~{self.ctx.local(r['end_at']):%m/%d %H:%M}"
                         for r in rows)


def _size(g: float) -> str:
    return f"{g / 1024:.1f}TB" if g >= 1024 else f"{g:.0f}GB"


# ── 지표 수신 HTTP 서버 ─────────────────────────────────────
def start_receiver(ctx: Ctx, mon: Monitor) -> ThreadingHTTPServer:
    # 토큰을 경로별로 나눔: METRICS_TOKEN은 /metrics만, JOBS_TOKEN(lb CLI, 일반 사용자에게 공개)은 /jobs만
    tokens = {"/metrics": ctx.cfg.metrics_token, "/jobs": ctx.cfg.jobs_token}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                return self._send(200, {"ok": True, "hosts": list(mon.latest)})
            self._send(404, {"ok": False})

        def do_POST(self):  # noqa: N802
            if self.path not in tokens:
                return self._send(404, {"ok": False})
            token = tokens[self.path]
            if not token or self.headers.get("Authorization") != f"Bearer {token}":
                return self._send(401, {"ok": False, "error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 2_000_000:
                return self._send(413, {"ok": False})
            try:
                body = json.loads(self.rfile.read(n))
                if self.path == "/jobs":
                    if mon.jobs is None:
                        return self._send(503, {"ok": False, "error": "jobs disabled"})
                    return self._send(200, mon.jobs.handle(body))
                mon.ingest(body)
            except Exception as e:  # noqa: BLE001
                log.warning("잘못된 요청 %s: %s", self.path, e)
                return self._send(400, {"ok": False, "error": str(e)})
            self._send(200, {"ok": True})

        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer((ctx.cfg.metrics_host, ctx.cfg.metrics_port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="metrics-http").start()
    log.info("지표 수신 대기: %s:%s", ctx.cfg.metrics_host, ctx.cfg.metrics_port)
    return srv


# ── Slack 핸들러 ───────────────────────────────────────────
def register(app, ctx: Ctx, mon: Monitor) -> None:
    @app.command("/gpu")
    def cmd_gpu(ack, command, respond):
        ack()
        uid = command["user_id"]
        args = (command.get("text") or "").split()
        sub = args[0].lower() if args else ""
        rest = args[1:]
        if sub in ("help", "도움말"):
            return respond(HELP)
        if sub in ("", "status", "현황"):
            return respond(text="GPU 현황", blocks=mon.gpu_blocks())
        if sub in ("free", "빈", "비어있는"):
            return respond(text="사용 가능한 GPU", blocks=mon.gpu_blocks(only_free=True))
        if sub in ("reserve", "예약"):
            return respond(mon.reserve(uid, rest))
        if sub in ("release", "unreserve", "해제", "반납"):
            rid = int(rest[0].lstrip("#")) if rest and rest[0].lstrip("#").isdigit() else None
            return respond(mon.release(uid, rid))
        if sub in ("mine", "my", "내예약"):
            return respond(mon.my_reservations_text(uid) or "활성 예약이 없어요.")
        if sub in ("link", "연결"):
            if not rest or not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,31}", rest[0]):
                return respond("형식: `/gpu link &lt;linux-user&gt;` (예: `/gpu link kim`)")
            target = uid
            m = re.search(r"<@([UW][A-Z0-9]+)", " ".join(rest[1:]))
            if m:
                if not ctx.is_admin(uid):
                    return respond("다른 사람의 계정 연결은 관리자만 할 수 있어요.")
                target = m.group(1)
            ctx.db.run("INSERT OR REPLACE INTO user_links(linux_user, slack_id) VALUES (?,?)", (rest[0], target))
            return respond(f"🔗 리눅스 계정 `{rest[0]}` ↔ <@{target}> 연결했어요.")
        if sub in ("forget", "remove", "삭제"):
            if not ctx.is_admin(uid):
                return respond("서버 삭제는 관리자만 할 수 있어요.")
            if not rest:
                return respond("형식: `/gpu forget 서버이름`")
            if not mon.forget(rest[0]):
                names = ", ".join(h for h, _, _ in mon.hosts()) or "없음"
                return respond(f"`{rest[0]}` 서버가 목록에 없어요. 현재 목록: {names}")
            return respond(f"🗑 `{rest[0]}` 를 서버 목록에서 지웠어요. 에이전트가 다시 보내면 다시 나타나요.")
        if sub in ("summary", "요약", "report"):
            return respond(text="사용 요약", blocks=mon.summary_blocks(ctx.today() - timedelta(days=1)))
        if sub in ("today", "오늘"):
            return respond(text="오늘 사용 요약", blocks=mon.summary_blocks(ctx.today()))
        if sub in ("week", "weekly", "주간"):
            opt = rest[0].lower() if rest else ""
            if opt in ("now", "this", "이번주"):
                start = gpureport.week_start(ctx.today())
                rep = gpureport.Report(ctx, mon, start, start + timedelta(days=7))
                return respond(text="이번 주 GPU 리포트", blocks=rep.blocks(gpureport.sheet_url(ctx)))
            rep = gpureport.Report(ctx, mon, *gpureport.last_week(ctx))
            if opt in ("sheet", "시트"):
                if not ctx.is_admin(uid):
                    return respond("시트 기록은 관리자만 할 수 있어요.")
                try:
                    return respond(gpureport.write_sheet(ctx, rep, force="force" in rest))
                except Exception as e:  # noqa: BLE001
                    log.exception("Google Sheet 기록 실패")
                    return respond(f"Google Sheet 기록에 실패했어요: {e}")
            return respond(text="주간 GPU 리포트", blocks=rep.blocks(gpureport.sheet_url(ctx)))
        respond(HELP)

    @app.command("/server")
    def cmd_server(ack, command, respond):
        ack()
        if (command.get("text") or "").strip() in ("help", "도움말"):
            return respond(HELP)
        respond(text="서버 현황", blocks=mon.server_blocks())
