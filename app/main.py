"""LabBot 진입점: Slack Socket Mode + 지표 수신 서버 + 스케줄러."""
from __future__ import annotations

import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import attendance, deadlines, gpureport, home, jobs, meetings, papers, servers, todo
from .config import Config, load_config
from .ctx import Ctx
from .db import DB
from .llm import LLM

log = logging.getLogger("labbot")


def create_app(cfg: Config, verify_token: bool = True, client=None, sync: bool = False, llm=None):
    # sync=True: 리스너를 ack 전에 끝까지 실행 (테스트용)
    kwargs = {"token": cfg.bot_token, "token_verification_enabled": verify_token,
              "process_before_response": sync}
    if client is not None:
        kwargs["client"] = client
    app = App(**kwargs)
    ctx = Ctx(cfg, DB(cfg.db_path), app.client)
    ctx.llm = llm if llm is not None else LLM(cfg.anthropic_api_key, cfg.llm_model)
    mon = servers.Monitor(ctx)
    ctx.refresh_home = lambda uid: home.publish(ctx, mon, uid)
    todo.register(app, ctx)
    attendance.register(app, ctx)
    servers.register(app, ctx, mon)
    meetings.register(app, ctx)
    papers.register(app, ctx)
    deadlines.register(app, ctx)
    jobs.register(app, ctx, mon)
    home.register(app, ctx, mon)
    return app, ctx, mon


def start_scheduler(ctx: Ctx, mon: servers.Monitor) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=ctx.tz)

    def safe(fn):
        def run():
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("작업 실패: %s", getattr(fn, "__name__", fn))
        run.__name__ = getattr(fn, "__name__", "job")
        return run

    def at(t, days="mon-sun"):
        return CronTrigger(hour=t.hour, minute=t.minute, day_of_week=days, timezone=ctx.tz)

    c = ctx.cfg
    sched.add_job(safe(lambda: todo.reminder_job(ctx)), "interval", minutes=1, id="todo_reminders")
    sched.add_job(safe(lambda: todo.digest_job(ctx)), at(c.todo_digest, "mon-fri"), id="todo_digest")
    sched.add_job(safe(lambda: attendance.checkin_reminder_job(ctx)), at(c.checkin_reminder, "mon-fri"),
                  id="checkin_reminder")
    sched.add_job(safe(lambda: attendance.checkout_reminder_job(ctx)), at(c.checkout_reminder), id="checkout_reminder")
    sched.add_job(safe(mon.watch), "interval", minutes=1, id="server_watch")
    sched.add_job(safe(mon.daily_summary_job), at(c.server_summary), id="server_summary")
    sched.add_job(safe(lambda: attendance.sheet_job(ctx)), at(c.attendance_sheet_sync), id="attendance_sheet",
                  misfire_grace_time=3600)
    sched.add_job(safe(lambda: gpureport.weekly_job(ctx, mon)), at(c.gpu_weekly, c.gpu_weekly_day),
                  id="gpu_weekly", misfire_grace_time=3600)
    sched.add_job(safe(lambda: meetings.recurring_prompt_job(ctx)), "interval", minutes=5, id="meeting_prompt")
    sched.add_job(safe(lambda: meetings.weekly_review_job(ctx)), at(c.weekly_review, c.weekly_review_day),
                  id="weekly_review")
    sched.add_job(safe(lambda: papers.daily_job(ctx)), at(c.paper_daily, "mon-fri"), id="papers_daily",
                  misfire_grace_time=3600)
    sched.add_job(safe(lambda: papers.weekly_job(ctx)), at(c.paper_weekly, c.paper_weekly_day), id="papers_weekly",
                  misfire_grace_time=3600)
    sched.add_job(safe(lambda: deadlines.reminder_job(ctx)), at(c.deadline_remind), id="deadline_reminders",
                  misfire_grace_time=3600)
    sched.add_job(safe(lambda: deadlines.weekly_job(ctx)), at(c.deadline_weekly, c.deadline_weekly_day),
                  id="deadline_weekly", misfire_grace_time=3600)
    if mon.jobs:
        sched.add_job(safe(mon.jobs.watch_job), "interval", minutes=1, id="jobs_watch")
        sched.add_job(safe(mon.jobs.prune_job), CronTrigger(hour=4, minute=10, timezone=ctx.tz), id="jobs_prune")
    sched.add_job(safe(mon.prune_job), CronTrigger(hour=4, minute=0, timezone=ctx.tz), id="prune")
    sched.start()
    return sched


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    missing = [k for k, v in {"SLACK_BOT_TOKEN": cfg.bot_token, "SLACK_APP_TOKEN": cfg.app_token,
                              "METRICS_TOKEN": cfg.metrics_token}.items() if not v]
    if missing:
        sys.exit(f".env에 다음 값을 채워 주세요: {', '.join(missing)}")
    if not cfg.attendance_channel:
        log.warning("ATTENDANCE_CHANNEL이 비어 있어 미출근 리마인더와 팀 현황이 동작하지 않아요.")
    if not cfg.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY가 없어 미팅 정리·논문 요약이 규칙 기반으로 동작해요.")
    if not cfg.jobs_token:
        log.warning("JOBS_TOKEN이 비어 있어 lb 작업 알림(POST /jobs)이 꺼져 있어요.")
    elif cfg.jobs_token == cfg.metrics_token:
        sys.exit("JOBS_TOKEN은 METRICS_TOKEN과 다른 값이어야 해요 (lb 설정 파일은 모든 사용자가 읽을 수 있어요).")
    if not cfg.leave_approver_ids:
        log.warning("LEAVE_APPROVER_IDS/ADMIN_IDS가 비어 있어 휴가가 자동 승인돼요.")
    app, ctx, mon = create_app(cfg)
    servers.start_receiver(ctx, mon)
    start_scheduler(ctx, mon)
    log.info("LabBot 시작")
    SocketModeHandler(app, cfg.app_token).start()


if __name__ == "__main__":
    main()
