"""학습 작업 종료 알림: 서버의 `lb` CLI가 보내는 이벤트(start·progress·end·notify)를 받아 DM으로 알립니다.
작업이 도는 동안 에이전트 지표에서 그 작업의 프로세스(pid·부모 pid)를 찾아 GPU 사용량을 메모리에 집계합니다."""
from __future__ import annotations

import json
import logging
import re
import threading
import time as _time

from .ctx import Ctx, context, section
from .timeparse import fmt_duration

log = logging.getLogger("labbot.jobs")

HELP = """*🏃 /jobs 사용법*
• `/jobs` — 내 실행 중인 작업과 최근 10개 결과
• `/jobs all` — 연구실 전체 실행 중인 작업 (이름·서버·GPU·경과 시간)
서버에서는 학습 명령 앞에 `lb run --` 만 붙이면 끝나면 DM이 와요. 예: `lb run -- python train.py --lr 1e-4`
• `lb run --name exp1 --every 6h -- ...` 이름 지정·6시간마다 중간 보고 · `lb watch 12345 --log train.log` 이미 도는 작업 감시
• `lb run -- docker run --gpus all ...` 컨테이너도 그대로 (`-d`여도 끝날 때 알림) · `lb docker <컨테이너>` 이미 떠 있는 컨테이너 감시
• `lb notify "eval 끝남: BLEU 31.2"` 스크립트 안에서 한 줄 알림
_DM을 받으려면 먼저 `/gpu link <리눅스계정>` 으로 계정을 연결해 주세요._"""

EVENTS = ("start", "attach", "progress", "end", "notify")   # attach: 매칭할 pid 추가 (docker)
MAX_EXTRA_PIDS = 16
LOW_UTIL_MIN_SAMPLES = 3     # 지표 3번(약 3분) 이상 잡혀야 '거의 안 씀' 판단
TAIL_SHOWN = 20

SIGNALS = {1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE",
           9: "SIGKILL", 11: "SIGSEGV", 13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM", 24: "SIGXCPU", 25: "SIGXFSZ"}

# (원인, 패턴, 조언) — 위에서부터 먼저 맞는 것 하나
HINTS = [
    ("💥 GPU 메모리 부족 (CUDA out of memory)",
     re.compile(r"CUDA out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED|cudaErrorMemoryAllocation", re.I),
     "batch size를 줄이거나 gradient accumulation · mixed precision · gradient checkpointing을 써 보세요. "
     "`/gpu`로 같은 GPU를 다른 사람이 쓰고 있지 않은지도 확인해 보세요."),
    ("💾 디스크 공간 부족 (No space left on device)",
     re.compile(r"No space left on device|ENOSPC", re.I),
     "체크포인트나 캐시(~/.cache/huggingface 등)를 정리하거나 다른 디스크에 저장해 주세요. `/server`에서 남은 공간을 볼 수 있어요."),
    ("📦 모듈 없음 (ModuleNotFoundError{mod})",
     re.compile(r"ModuleNotFoundError: No module named '([^']+)'"),
     "가상환경(conda/venv)이 켜진 상태로 실행했는지, `pip install`이 필요한지 확인해 주세요."),
    ("📉 loss가 NaN/inf가 됐어요",
     re.compile(r"\b(?:loss|train[_ ]loss|val[_ ]loss)\b[^\n]{0,40}?(?<![\w.])-?(?:nan|inf)\b|non-finite loss",
                re.I),
     "learning rate를 낮추거나 gradient clipping을 쓰고, fp16 overflow나 데이터의 이상값을 확인해 보세요."),
]
OOM_KILL = ("💀 시스템 메모리 부족으로 강제 종료된 것 같아요 (exit 137 · Killed)",
            "OOM killer일 가능성이 커요. DataLoader `num_workers`, 데이터를 한꺼번에 RAM에 올리는지 확인해 보세요. "
            "직접 `kill -9`로 끈 거라면 무시하세요.")


def classify(tail: list[str], exit_code: int | None, signal: int | None) -> tuple[str, str] | None:
    """실패 원인 힌트 (제목, 조언). 모르면 None"""
    text = "\n".join(tail or [])
    for title, pat, advice in HINTS:
        m = pat.search(text)
        if m:
            mod = f": {m.group(1)}" if "{mod}" in title and m.groups() else ""
            return title.format(mod=mod), advice
    last = next((ln.strip() for ln in reversed(tail or []) if ln.strip()), "")
    if exit_code == 137 or signal == 9 or last in ("Killed", "killed"):
        return OOM_KILL
    return None


def exit_text(exit_code: int | None, signal: int | None) -> str:
    if exit_code is None:
        return "종료 코드 알 수 없음"
    sig = signal or (exit_code - 128 if 128 < exit_code < 160 else None)
    name = SIGNALS.get(sig, f"signal {sig}") if sig else ""
    return f"exit {exit_code}" + (f" ({name})" if name else "")


def dur(seconds: float) -> str:
    return f"{int(seconds)}초" if seconds < 60 else fmt_duration(seconds)


def gb(mb: float) -> str:
    return f"{mb / 1024:.1f}GB"


def _str(v, n: int) -> str:
    return str(v if v is not None else "")[:n]


def _int(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("숫자 필드 형식이 잘못됐어요")
    return int(v)


class Tracker:
    def __init__(self, ctx: Ctx, mon):
        self.ctx, self.mon = ctx, mon
        self.lock = threading.Lock()
        self.usage: dict[str, dict] = {}   # job id → GPU 사용량 집계 (작업이 도는 동안만)
        mon.listeners.append(self.on_metrics)
        mon.jobs = self

    # ── GPU 사용량 (에이전트 지표마다) ─────────────────────
    def on_metrics(self, host: str, data: dict) -> None:
        running = self.ctx.db.all("SELECT id, pid, extra_pids FROM jobs WHERE host=? AND status='running'", (host,))
        by_pid = {}
        for r in running:
            for pid in [r["pid"]] + json.loads(r["extra_pids"] or "[]"):
                if pid is not None:
                    by_pid[pid] = r["id"]
        if not by_pid:
            return
        per_job: dict[str, dict[int, float]] = {}
        utils: dict[int, float] = {}
        for g in data.get("gpus") or []:
            idx = int(g["index"])
            utils[idx] = float(g.get("util") or 0)
            for p in g.get("procs") or []:
                chain = [p.get("pid")] + list(p.get("ppids") or [])   # 자식 프로세스(torchrun worker 등)도 찾기
                job = next((by_pid[x] for x in chain if x in by_pid), None)
                if job:
                    mem = per_job.setdefault(job, {})
                    mem[idx] = mem.get(idx, 0.0) + float(p.get("mem_mb") or 0)
        now = int(_time.time())
        with self.lock:
            for job, mem in per_job.items():
                u = self.usage.setdefault(job, {"gpus": set(), "max_mem": 0.0, "max_gpu_mem": 0.0,
                                                "util_sum": 0.0, "n": 0})
                u["gpus"] |= set(mem)
                u["max_mem"] = max(u["max_mem"], sum(mem.values()))
                u["max_gpu_mem"] = max(u["max_gpu_mem"], max(mem.values()))
                u["util_sum"] += sum(utils.get(i, 0.0) for i in mem) / len(mem)
                u["n"] += 1
                u["current"], u["seen"] = sorted(mem), now

    def stats(self, job_id: str) -> dict | None:
        with self.lock:
            u = self.usage.get(job_id)
            if not u or not u["n"]:
                return None
            return {"gpus": sorted(u["gpus"]), "max_mem_mb": round(u["max_mem"]),
                    "max_gpu_mem_mb": round(u["max_gpu_mem"]), "avg_util": round(u["util_sum"] / u["n"], 1),
                    "samples": u["n"]}

    def current_gpus(self, job_id: str) -> list[int]:
        with self.lock:
            u = self.usage.get(job_id)
            return u["current"] if u and _time.time() - u.get("seen", 0) < 300 else []

    # ── 이벤트 ─────────────────────────────────────────────
    def handle(self, ev: dict) -> dict:
        """POST /jobs 본문 처리. 잘못된 형식이면 ValueError"""
        if not isinstance(ev, dict) or ev.get("event") not in EVENTS:
            raise ValueError("event는 " + ", ".join(EVENTS) + " 중 하나예요")
        host = _str(ev.get("host"), 64).strip()
        user = _str(ev.get("user"), 32).strip()
        if not host or not re.fullmatch(r"[\w.\-]{1,32}", user):
            raise ValueError("host, user 필드를 확인해 주세요")
        sid = self.mon.link_of(user)
        kind = ev["event"]
        if kind == "notify":
            text = _str(ev.get("text"), 2000).strip()
            if not text:
                raise ValueError("text가 비어 있어요")
            if sid:
                self.ctx.dm(sid, f"📣 {host}: {text}", [section(f"📣 *{host}* · `{user}`\n{text}")])
            return {"ok": True, "linked": bool(sid)}

        job_id = _str(ev.get("job_id"), 64)
        if not re.fullmatch(r"[0-9a-fA-F\-]{8,64}", job_id):
            raise ValueError("job_id 형식이 잘못됐어요")
        tail = ev.get("tail") or []
        if not isinstance(tail, list):
            raise ValueError("tail은 문자열 목록이에요")
        tail = [_str(x, 500) for x in tail[-50:]]
        now = int(_time.time())
        started = _int(ev.get("started_at")) or now
        row = self.ctx.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:   # start를 놓쳤어도 end만으로 기록
            self.ctx.db.run(
                "INSERT INTO jobs(id, host, linux_user, slack_id, name, cmd, pid, mode, started_at, status,"
                " last_event_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, host, user, sid, _str(ev.get("name"), 100) or "job", _str(ev.get("cmd"), 500),
                 _int(ev.get("pid")), "watch" if ev.get("mode") == "watch" else "run", min(started, now),
                 "running", now))
            row = self.ctx.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        elif sid and not row["slack_id"]:
            self.ctx.db.run("UPDATE jobs SET slack_id=? WHERE id=?", (sid, job_id))
        if row["status"] != "running":
            return {"ok": True, "linked": bool(sid), "duplicate": True}   # 재전송된 end
        self.ctx.db.run("UPDATE jobs SET last_event_at=? WHERE id=?", (now, job_id))
        if kind == "attach":
            pids = ev.get("pids")
            if not isinstance(pids, list) or not pids:
                raise ValueError("pids는 숫자 목록이에요")
            pids = [_int(x) for x in pids]
            old = json.loads(row["extra_pids"] or "[]")
            merged = (old + [x for x in pids if x not in old and x and x > 0])[-MAX_EXTRA_PIDS:]
            self.ctx.db.run("UPDATE jobs SET extra_pids=? WHERE id=?", (json.dumps(merged), job_id))
        elif kind == "progress" and sid:
            self._progress_dm(sid, row, _int(ev.get("elapsed")) or now - row["started_at"],
                              _str(ev.get("last_line"), 500))
        elif kind == "end":
            self._finish(row, sid, ev, tail, now)
        return {"ok": True, "linked": bool(sid)}

    def _progress_dm(self, sid: str, job, elapsed: int, last_line: str) -> None:
        st = self.stats(job["id"])
        txt = f"⏳ *{job['name']}* · {job['host']} · {dur(elapsed)}째 실행 중"
        if st:
            txt += f"\n🎛 GPU {','.join(map(str, st['gpus']))} · 평균 사용률 {st['avg_util']:.0f}%"
        if last_line:
            txt += f"\n```{_code(last_line)}```"
        self.ctx.dm(sid, f"⏳ {job['name']} {dur(elapsed)}째 실행 중", [section(txt)])

    def _finish(self, job, sid, ev: dict, tail: list[str], now: int) -> None:
        exit_code, sig = _int(ev.get("exit_code")), _int(ev.get("signal"))
        if ev.get("cancelled"):
            status = "cancelled"
        elif exit_code is None:
            status = "ended"
        else:
            status = "ok" if exit_code == 0 else "failed"
        ended = min(_int(ev.get("ended_at")) or now, now)
        st = self.stats(job["id"])
        with self.lock:
            self.usage.pop(job["id"], None)
        self.ctx.db.run("UPDATE jobs SET status=?, ended_at=?, exit_code=?, signal=?, tail=?, gpu_stats=? WHERE id=?",
                        (status, max(ended, job["started_at"]), exit_code, sig,
                         json.dumps(tail, ensure_ascii=False), json.dumps(st) if st else None, job["id"]))
        job = self.ctx.db.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
        if sid and ev.get("notify", True) and status != "cancelled":
            text, blocks = end_message(self.ctx, job)
            self.ctx.dm(sid, text, blocks)
            self.ctx.refresh_home(sid)

    # ── 서버 응답 없음 (1분마다) ───────────────────────────
    def watch_job(self) -> None:
        offline = {h for h, _d, online in self.mon.hosts() if not online}
        if not offline:
            return
        rows = self.ctx.db.all("SELECT * FROM jobs WHERE status='running' AND offline_alerted=0")
        for j in rows:
            if j["host"] not in offline:
                continue
            self.ctx.db.run("UPDATE jobs SET offline_alerted=1 WHERE id=?", (j["id"],))
            sid = j["slack_id"] or self.mon.link_of(j["linux_user"])
            if sid:
                self.ctx.dm(sid, f"{j['host']} 응답이 끊겼어요",
                            [section(f"🔴 *{j['host']}* 응답이 끊겼어요. 작업 *{j['name']}* "
                                     f"({dur(_time.time() - j['started_at'])}째 실행 중) 상태를 확인해 주세요."),
                             context("서버가 다시 연결되면 `/server`에 표시돼요. 작업이 끝나면 평소처럼 알려 드려요.")])

    def prune_job(self) -> None:
        cutoff = int(_time.time()) - self.ctx.cfg.retention_days * 86400
        self.ctx.db.run("UPDATE jobs SET tail=NULL WHERE ended_at<? AND tail IS NOT NULL", (cutoff,))


def _code(s: str) -> str:
    return s.replace("```", "ˋˋˋ")


def gpu_lines(ctx: Ctx, st: dict | None) -> list[str]:
    if not st:
        return ["🎛 GPU 사용 기록 없음 (CPU 작업이었거나, 에이전트가 없는 서버이거나, 컨테이너 안에서 lb를 실행)"]
    gpus = ",".join(map(str, st["gpus"]))
    line = f"🎛 GPU {gpus} · 최대 메모리 {gb(st['max_mem_mb'])}"
    if len(st["gpus"]) > 1:
        line += f" (GPU당 최대 {gb(st['max_gpu_mem_mb'])})"
    line += f" · 평균 사용률 {st['avg_util']:.0f}%"
    out = [line]
    if st["samples"] >= LOW_UTIL_MIN_SAMPLES and st["avg_util"] < ctx.cfg.jobs_low_util:
        out.append(f"🐢 GPU를 거의 쓰지 않았어요 (평균 {st['avg_util']:.0f}%). 데이터 로딩 병목일 수 있어요 "
                   f"(DataLoader `num_workers`, 전처리, 디스크 속도 확인).")
    return out


def end_message(ctx: Ctx, job) -> tuple[str, list]:
    tail = json.loads(job["tail"] or "[]")
    st = json.loads(job["gpu_stats"]) if job["gpu_stats"] else None
    took = dur((job["ended_at"] or job["started_at"]) - job["started_at"])
    hint = classify(tail, job["exit_code"], job["signal"]) if job["status"] in ("failed", "ended") else None
    if job["status"] == "ok":
        head = f"✅ *{job['name']}* 끝났어요"
    elif job["status"] == "failed":
        head = f"❌ *{job['name']}* 실패" + (f" — {hint[0]}" if hint else "")
    else:
        head = f"🏁 *{job['name']}* 끝났어요" + (f" — {hint[0]}" if hint else "")
    meta = f"🖥 {job['host']} · ⏱ {took} · {exit_text(job['exit_code'], job['signal'])}"
    if job["mode"] == "watch":
        meta += " (`lb watch`는 종료 코드를 알 수 없어요)"
    parts = [head, meta]
    if hint:
        parts.append(f"💡 {hint[1]}")
    parts += gpu_lines(ctx, st)
    blocks = [section("\n".join(parts))]
    shown = [_code(x)[-200:] for x in tail[-TAIL_SHOWN:]]
    while shown and len("\n".join(shown)) > 2800:
        shown.pop(0)
    if shown:
        blocks.append(section("```" + "\n".join(shown) + "```"))
    if job["cmd"]:
        blocks.append(context(f"`{_code(job['cmd'])[:300]}`"))
    return re.sub(r"[*`]", "", head), blocks


# ── 목록 ───────────────────────────────────────────────────
def _mine_sql(mon, uid: str) -> tuple[str, tuple]:
    users = mon.linux_users_of(uid)
    cond = "(slack_id=?" + (f" OR (slack_id IS NULL AND linux_user IN ({','.join('?' * len(users))}))" if users
                            else "") + ")"
    return cond, (uid, *users)


STATUS_ICON = {"ok": "✅", "failed": "❌", "ended": "🏁", "cancelled": "⏹"}


def my_running_count(ctx: Ctx, mon, uid: str) -> int:
    cond, params = _mine_sql(mon, uid)
    return ctx.db.one(f"SELECT COUNT(*) c FROM jobs WHERE status='running' AND {cond}", params)["c"]


def running_line(ctx: Ctx, tracker: Tracker | None, j, show_owner: bool) -> str:
    gpus = tracker.current_gpus(j["id"]) if tracker else []
    line = f"• *{j['name']}* · {j['host']}" + (f" GPU {','.join(map(str, gpus))}" if gpus else "")
    line += f" · {dur(_time.time() - j['started_at'])}째"
    if show_owner:
        line += " · " + (ctx.name(j["slack_id"]) if j["slack_id"] else f"`{j['linux_user']}`")
    return line


def mine_blocks(ctx: Ctx, mon, uid: str) -> list:
    cond, params = _mine_sql(mon, uid)
    tracker = getattr(mon, "jobs", None)
    running = ctx.db.all(f"SELECT * FROM jobs WHERE status='running' AND {cond} ORDER BY started_at", params)
    done = ctx.db.all(f"SELECT * FROM jobs WHERE status<>'running' AND {cond} ORDER BY ended_at DESC LIMIT 10",
                      params)
    b = [section(f"*🏃 내 실행 중인 작업* ({len(running)})\n"
                 + ("\n".join(running_line(ctx, tracker, j, False) for j in running) or "_없어요._"))]
    lines = []
    for j in done:
        when = ctx.local(j["ended_at"] or j["started_at"])
        line = (f"{STATUS_ICON.get(j['status'], '•')} *{j['name']}* · {j['host']} · {when:%m/%d %H:%M} · "
                f"{dur((j['ended_at'] or j['started_at']) - j['started_at'])}")
        if j["status"] == "failed":
            line += f" · {exit_text(j['exit_code'], j['signal'])}"
            h = classify(json.loads(j["tail"] or "[]"), j["exit_code"], j["signal"])
            if h:
                line += f" · {h[0]}"
        lines.append(line)
    b.append(section("*📜 최근 결과*\n" + ("\n".join(lines) or "_아직 없어요. 서버에서 `lb run -- python train.py` 처럼 실행해 보세요._")))
    if not mon.linux_users_of(uid):
        b.append(context("⚠️ 리눅스 계정이 연결되지 않았어요. `/gpu link <리눅스계정>` 을 해야 작업 알림이 와요."))
    return b


def all_blocks(ctx: Ctx, mon) -> list:
    tracker = getattr(mon, "jobs", None)
    rows = ctx.db.all("SELECT * FROM jobs WHERE status='running' ORDER BY host, started_at")
    lines = [running_line(ctx, tracker, j, True) for j in rows]
    head = f"*🏃 연구실 실행 중인 작업* ({len(rows)})"
    if not lines:
        return [section(head + "\n_지금 `lb`로 실행 중인 작업이 없어요._")]
    blocks, buf = [], head
    for ln in lines:
        if len(buf) + len(ln) > 2900:
            blocks.append(section(buf))
            buf = ln
        else:
            buf += "\n" + ln
    blocks.append(section(buf))
    return blocks[:45] + [context("`lb run`으로 실행한 작업만 보여요. 로그는 본인에게만 DM으로 가요.")]


# ── Slack 핸들러 ───────────────────────────────────────────
def register(app, ctx: Ctx, mon) -> Tracker:
    tracker = Tracker(ctx, mon)

    @app.command("/jobs")
    def cmd_jobs(ack, command, respond):
        ack()
        uid = command["user_id"]
        sub = (command.get("text") or "").strip().lower()
        if sub in ("", "mine", "list", "my"):
            return respond(text="내 작업", blocks=mine_blocks(ctx, mon, uid))
        if sub == "all":
            return respond(text="연구실 실행 중인 작업", blocks=all_blocks(ctx, mon))
        respond(HELP)

    return tracker
