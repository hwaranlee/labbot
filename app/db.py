"""SQLite 저장소. 타임스탬프는 모두 UTC epoch 초(int), 날짜는 로컬 'YYYY-MM-DD' 문자열."""
from __future__ import annotations

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    creator      TEXT NOT NULL,
    assignee     TEXT NOT NULL,
    channel_id   TEXT,
    permalink    TEXT,
    due_at       INTEGER,
    due_has_time INTEGER DEFAULT 0,
    remind_at    INTEGER,
    reminded     INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'open',
    created_at   INTEGER NOT NULL,
    done_at      INTEGER,
    done_by      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_remind ON tasks(status, reminded, remind_at);

CREATE TABLE IF NOT EXISTS attendance (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,
    date     TEXT NOT NULL,
    in_at    INTEGER NOT NULL,
    out_at   INTEGER,
    mode     TEXT DEFAULT 'office',
    note     TEXT
);
CREATE INDEX IF NOT EXISTS idx_att_user_date ON attendance(user_id, date);
CREATE INDEX IF NOT EXISTS idx_att_date ON attendance(date);

CREATE TABLE IF NOT EXISTS attendance_breaks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    att_id   INTEGER NOT NULL,           -- attendance.id
    start_at INTEGER NOT NULL,
    end_at   INTEGER,                    -- NULL이면 휴식 중
    note     TEXT
);
CREATE INDEX IF NOT EXISTS idx_breaks_att ON attendance_breaks(att_id);

CREATE TABLE IF NOT EXISTS leaves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    days        REAL NOT NULL,
    reason      TEXT,
    status      TEXT DEFAULT 'pending',
    approver_id TEXT,
    decision_note TEXT,
    created_at  INTEGER NOT NULL,
    decided_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_leaves_user ON leaves(user_id, status);
CREATE INDEX IF NOT EXISTS idx_leaves_dates ON leaves(status, start_date, end_date);

CREATE TABLE IF NOT EXISTS leave_msgs (
    leave_id INTEGER NOT NULL,
    channel  TEXT NOT NULL,
    ts       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gpu_samples (
    ts        INTEGER NOT NULL,
    host      TEXT NOT NULL,
    gpu       INTEGER NOT NULL,
    util      REAL,
    mem_used  REAL,
    mem_total REAL,
    weight    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gpu_samples_ts ON gpu_samples(ts);

CREATE TABLE IF NOT EXISTS gpu_user_samples (
    ts      INTEGER NOT NULL,
    host    TEXT NOT NULL,
    gpu     INTEGER NOT NULL,
    user    TEXT NOT NULL,
    mem_mb  REAL,
    weight  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gpu_user_samples_ts ON gpu_user_samples(ts);

CREATE TABLE IF NOT EXISTS gpu_reservations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    host      TEXT NOT NULL,
    gpus      TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    start_at  INTEGER NOT NULL,
    end_at    INTEGER NOT NULL,
    note      TEXT,
    released  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_links (
    linux_user TEXT PRIMARY KEY,
    slack_id   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meetings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    host         TEXT NOT NULL,
    participants TEXT NOT NULL,          -- 쉼표로 구분한 Slack ID
    meeting_date TEXT NOT NULL,
    notes        TEXT,
    summary      TEXT,
    decisions    TEXT,                   -- JSON 배열
    items        TEXT,                   -- JSON 배열 (확정 전 초안)
    share        TEXT DEFAULT 'dm',      -- dm | channel
    status       TEXT DEFAULT 'draft',   -- draft | confirmed | discarded
    recurring_id INTEGER,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(meeting_date);

CREATE TABLE IF NOT EXISTS recurring_meetings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner        TEXT NOT NULL,
    participants TEXT NOT NULL,
    weekday      INTEGER NOT NULL,       -- 0=월
    start        TEXT NOT NULL,          -- HH:MM
    duration_min INTEGER NOT NULL,
    title        TEXT NOT NULL,
    active       INTEGER DEFAULT 1,
    last_prompted TEXT
);

CREATE TABLE IF NOT EXISTS paper_topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner      TEXT NOT NULL,            -- 'lab' 또는 Slack ID(개인 구독)
    query      TEXT NOT NULL,
    categories TEXT,
    created_by TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS papers (
    id         TEXT PRIMARY KEY,         -- arXiv ID (버전 제외)
    title      TEXT NOT NULL,
    authors    TEXT,
    abstract   TEXT,
    url        TEXT,
    published  TEXT,
    upvotes    INTEGER DEFAULT 0,
    detail     TEXT,
    first_seen INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_posts (
    paper_id  TEXT NOT NULL,
    target    TEXT NOT NULL,             -- 'lab' 또는 Slack ID
    posted_at INTEGER NOT NULL,
    score     REAL,
    summary   TEXT,
    why       TEXT,
    PRIMARY KEY (paper_id, target)
);

CREATE TABLE IF NOT EXISTS paper_threads (
    channel TEXT NOT NULL,
    ts      TEXT NOT NULL,
    paper_id TEXT NOT NULL,
    PRIMARY KEY (channel, ts, paper_id)
);

CREATE TABLE IF NOT EXISTS conferences (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    grp          TEXT DEFAULT '',        -- NLP / ML / Speech ... (자유 입력)
    official_url TEXT DEFAULT '',
    review_type  TEXT DEFAULT 'direct',  -- direct | arr | arr_cycle
    default_tz   TEXT DEFAULT 'AoE',
    watch        INTEGER DEFAULT 1,      -- 월요일 채널 요약에 포함
    created_by   TEXT NOT NULL,          -- Slack ID 또는 'yaml'(초기 목록)
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conf_deadlines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    conf_id      INTEGER NOT NULL,
    year         INTEGER NOT NULL,
    kind         TEXT NOT NULL,          -- abstract | paper | supplementary | arr_cycle | commitment
    due_at       INTEGER NOT NULL,
    tz           TEXT NOT NULL,          -- 입력한 시간대 (비우면 학회 기본값으로 채움)
    official_url TEXT DEFAULT '',
    note         TEXT DEFAULT '',
    updated_by   TEXT,
    updated_at   INTEGER,
    UNIQUE (conf_id, year, kind)
);
CREATE INDEX IF NOT EXISTS idx_conf_deadlines_due ON conf_deadlines(due_at);

CREATE TABLE IF NOT EXISTS arr_links (
    cycle_id INTEGER NOT NULL,           -- review_type='arr_cycle' 학회
    conf_id  INTEGER NOT NULL,           -- 그 주기 결과로 commit할 수 있는 학회
    PRIMARY KEY (cycle_id, conf_id)
);

CREATE TABLE IF NOT EXISTS conf_targets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    conf_id    INTEGER NOT NULL,
    title      TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, conf_id)
);

CREATE TABLE IF NOT EXISTS deadline_alerts (
    user_id     TEXT NOT NULL,
    deadline_id INTEGER NOT NULL,
    stage       INTEGER NOT NULL,        -- D-n
    due_at      INTEGER NOT NULL,        -- 보낼 때의 마감 (날짜가 바뀌면 다시 보냄)
    sent_on     TEXT NOT NULL,
    PRIMARY KEY (user_id, deadline_id, stage, due_at)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 기존 DB에 컬럼을 추가하는 마이그레이션: (테이블, 컬럼, 정의)
MIGRATIONS = [
    ("tasks", "meeting_id", "INTEGER"),
    ("gpu_reservations", "released_at", "INTEGER"),   # 실제 해제 시각 (주간 리포트용)
]

# 기존 데이터를 고치는 마이그레이션 (여러 번 실행해도 결과가 같아야 함)
DATA_MIGRATIONS = [
    # 근태 형태를 출근/재택 두 가지로 정리: 외근 기록은 출근으로 바꾸고 메모에 남김
    "UPDATE attendance SET mode='office', note=TRIM('외근 ' || COALESCE(note, '')) WHERE mode='field'",
]


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            for table, col, decl in MIGRATIONS:
                cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            for sql in DATA_MIGRATIONS:
                self.conn.execute(sql)

    def get_setting(self, key: str, default: str = "") -> str:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r and r["value"] is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self.run("INSERT INTO settings(key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def run(self, sql: str, params=()) -> int:
        """INSERT/UPDATE 실행. INSERT면 새 id, 아니면 변경된 행 수를 돌려줍니다."""
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    def many(self, sql: str, rows) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
