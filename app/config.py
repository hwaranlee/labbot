"""환경변수(.env)에서 설정을 읽습니다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ImportError:  # 테스트 환경 등
    def load_dotenv(*_a, **_k):
        return False


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _time(name: str, default: str) -> time:
    raw = os.environ.get(name, default).strip()
    h, m = raw.split(":")
    return time(int(h), int(m))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Config:
    bot_token: str = ""
    app_token: str = ""
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Asia/Seoul"))
    db_path: str = "labbot.db"

    # 권한
    admin_ids: list[str] = field(default_factory=list)          # CSV 내보내기 등
    leave_approver_ids: list[str] = field(default_factory=list)  # 휴가 승인자

    # 채널
    attendance_channel: str = ""   # 출퇴근 알림 + 근태 대상 멤버 목록
    server_channel: str = ""       # 서버 알림/일일 요약

    # 근태
    checkin_reminder: time = time(10, 30)
    checkout_reminder: time = time(21, 0)
    extra_holidays: list[str] = field(default_factory=list)  # YYYY-MM-DD, 연구실 자체 휴무일
    attendance_gsheet_id: str = ""       # 출퇴근·휴가 기록 시트 (GPU 시트와 별도)
    attendance_sheet_sync: time = time(4, 30)

    # Todo
    todo_digest: time = time(9, 0)

    # 서버 모니터링
    metrics_host: str = "0.0.0.0"
    metrics_port: int = 8787
    metrics_token: str = ""
    server_summary: time = time(9, 0)
    disk_alert_pct: int = 90
    offline_minutes: int = 5
    idle_gpu_minutes: int = 60
    idle_gpu_util: int = 5
    idle_gpu_min_mem_mb: int = 1000
    free_gpu_mem_mb: int = 500
    free_gpu_util: int = 10
    retention_days: int = 60
    gpu_weekly_day: str = "mon"          # 주간 GPU 리포트 (지난주 월~일)
    gpu_weekly: time = time(9, 30)
    gsheet_id: str = ""                  # 비워 두면 시트 기록 안 함
    google_sa_file: str = ""             # 서비스 계정 JSON 키 경로

    # LLM (Claude API) — 비워 두면 규칙 기반으로 동작
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"

    # 미팅
    meeting_channel: str = ""            # 공유를 선택한 미팅 요약 + 금요일 랩 전체 현황
    weekly_review_day: str = "fri"
    weekly_review: time = time(16, 0)

    # 논문
    paper_channel: str = ""
    paper_schedule: str = "both"         # daily | weekly | both | off
    paper_daily: time = time(8, 30)
    paper_weekly_day: str = "fri"
    paper_weekly: time = time(17, 0)
    paper_top_n: int = 5
    paper_min_score: int = 6             # LLM 관련도(0~10) 이 값 미만은 제외
    paper_window_days: int = 3


def load_config() -> Config:
    load_dotenv()
    approvers = _list("LEAVE_APPROVER_IDS")
    admins = _list("ADMIN_IDS")
    return Config(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        llm_model=os.environ.get("LLM_MODEL", "claude-sonnet-5-5"),
        meeting_channel=os.environ.get("MEETING_CHANNEL", ""),
        weekly_review_day=os.environ.get("WEEKLY_REVIEW_DAY", "fri").lower(),
        weekly_review=_time("WEEKLY_REVIEW", "16:00"),
        paper_channel=os.environ.get("PAPER_CHANNEL", ""),
        paper_schedule=os.environ.get("PAPER_SCHEDULE", "both").lower(),
        paper_daily=_time("PAPER_DAILY", "08:30"),
        paper_weekly_day=os.environ.get("PAPER_WEEKLY_DAY", "fri").lower(),
        paper_weekly=_time("PAPER_WEEKLY", "17:00"),
        paper_top_n=_int("PAPER_TOP_N", 5),
        paper_min_score=_int("PAPER_MIN_SCORE", 6),
        paper_window_days=_int("PAPER_WINDOW_DAYS", 3),
        bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
        app_token=os.environ.get("SLACK_APP_TOKEN", ""),
        tz=ZoneInfo(os.environ.get("TZ_NAME", "Asia/Seoul")),
        db_path=os.environ.get("DB_PATH", "labbot.db"),
        admin_ids=admins,
        leave_approver_ids=approvers or admins,
        attendance_channel=os.environ.get("ATTENDANCE_CHANNEL", ""),
        server_channel=os.environ.get("SERVER_CHANNEL", ""),
        checkin_reminder=_time("CHECKIN_REMINDER", "10:30"),
        checkout_reminder=_time("CHECKOUT_REMINDER", "21:00"),
        extra_holidays=_list("EXTRA_HOLIDAYS"),
        attendance_gsheet_id=os.environ.get("ATTENDANCE_GSHEET_ID", ""),
        attendance_sheet_sync=_time("ATTENDANCE_SHEET_SYNC", "04:30"),
        todo_digest=_time("TODO_DIGEST", "09:00"),
        metrics_host=os.environ.get("METRICS_HOST", "0.0.0.0"),
        metrics_port=_int("METRICS_PORT", 8787),
        metrics_token=os.environ.get("METRICS_TOKEN", ""),
        server_summary=_time("SERVER_SUMMARY", "09:00"),
        disk_alert_pct=_int("DISK_ALERT_PCT", 90),
        offline_minutes=_int("OFFLINE_MINUTES", 5),
        idle_gpu_minutes=_int("IDLE_GPU_MINUTES", 60),
        idle_gpu_util=_int("IDLE_GPU_UTIL", 5),
        idle_gpu_min_mem_mb=_int("IDLE_GPU_MIN_MEM_MB", 1000),
        free_gpu_mem_mb=_int("FREE_GPU_MEM_MB", 500),
        free_gpu_util=_int("FREE_GPU_UTIL", 10),
        retention_days=_int("RETENTION_DAYS", 60),
        gpu_weekly_day=os.environ.get("GPU_WEEKLY_DAY", "mon").lower(),
        gpu_weekly=_time("GPU_WEEKLY", "09:30"),
        gsheet_id=os.environ.get("GSHEET_ID", ""),
        google_sa_file=os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
    )
