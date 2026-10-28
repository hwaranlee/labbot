"""모듈들이 공유하는 실행 컨텍스트: 설정, DB, Slack 클라이언트, 공통 헬퍼."""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import date, datetime
from typing import Callable

from slack_sdk import WebClient

from .config import Config
from .db import DB

log = logging.getLogger("labbot")

try:
    import holidays as _holidays
except ImportError:  # pragma: no cover
    _holidays = None


class Ctx:
    def __init__(self, cfg: Config, db: DB, client: WebClient):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.tz = cfg.tz
        self._user_cache: dict[str, tuple[float, dict]] = {}
        self._members_cache: tuple[float, list[str]] = (0.0, [])
        self._lock = threading.Lock()
        self._kr_holidays = _holidays.country_holidays("KR") if _holidays else {}
        # main.py에서 home.publish를 연결합니다 (순환 import 방지)
        self.refresh_home: Callable[[str], None] = lambda _uid: None
        self.llm = None  # main.py에서 LLM 인스턴스를 연결

    # ── 시간 ────────────────────────────────────────────────
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def local(self, epoch: int | float) -> datetime:
        return datetime.fromtimestamp(epoch, self.tz)

    def is_workday(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        if d in self._kr_holidays:
            return False
        return d.isoformat() not in self.cfg.extra_holidays

    def holiday_name(self, d: date) -> str | None:
        return self._kr_holidays.get(d) if self._kr_holidays else None

    # ── 사용자 ──────────────────────────────────────────────
    def user(self, uid: str) -> dict:
        with self._lock:
            hit = self._user_cache.get(uid)
            if hit and _time.time() - hit[0] < 3600:
                return hit[1]
        try:
            info = self.client.users_info(user=uid)["user"]
        except Exception:  # noqa: BLE001
            log.exception("users_info 실패: %s", uid)
            info = {"id": uid, "name": uid}
        with self._lock:
            self._user_cache[uid] = (_time.time(), info)
        return info

    def name(self, uid: str) -> str:
        u = self.user(uid)
        p = u.get("profile", {})
        return p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or uid

    def is_admin(self, uid: str) -> bool:
        return uid in self.cfg.admin_ids

    def members(self) -> list[str]:
        """근태 대상: ATTENDANCE_CHANNEL의 사람 멤버 (봇·비활성 제외). 10분 캐시."""
        if not self.cfg.attendance_channel:
            return []
        with self._lock:
            ts, cached = self._members_cache
            if cached and _time.time() - ts < 600:
                return cached
        ids: list[str] = []
        cursor = None
        try:
            while True:
                r = self.client.conversations_members(
                    channel=self.cfg.attendance_channel, cursor=cursor, limit=200)
                ids.extend(r["members"])
                cursor = r.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        except Exception:  # noqa: BLE001
            log.exception("채널 멤버 조회 실패")
            return cached
        people = [u for u in ids if not self.user(u).get("is_bot")
                  and not self.user(u).get("deleted") and u != "USLACKBOT"]
        with self._lock:
            self._members_cache = (_time.time(), people)
        return people

    # ── 메시지 ──────────────────────────────────────────────
    def dm(self, uid: str, text: str, blocks: list | None = None) -> dict | None:
        try:
            ch = self.client.conversations_open(users=uid)["channel"]["id"]
            return self.client.chat_postMessage(channel=ch, text=text, blocks=blocks)
        except Exception:  # noqa: BLE001
            log.exception("DM 실패: %s", uid)
            return None

    def post(self, channel: str, text: str, blocks: list | None = None) -> dict | None:
        if not channel:
            return None
        try:
            return self.client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        except Exception:  # noqa: BLE001
            log.exception("메시지 전송 실패: %s", channel)
            return None


# ── Block Kit 헬퍼 ──────────────────────────────────────────
def section(text: str, accessory: dict | None = None) -> dict:
    b = {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}
    if accessory:
        b["accessory"] = accessory
    return b


def button(text: str, action_id: str, value: str = "", style: str | None = None,
           confirm: dict | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text, "emoji": True},
         "action_id": action_id, "value": value or action_id}
    if style:
        b["style"] = style
    if confirm:
        b["confirm"] = confirm
    return b


def actions(*elements: dict, block_id: str | None = None) -> dict:
    b = {"type": "actions", "elements": list(elements)}
    if block_id:
        b["block_id"] = block_id
    return b


def context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:3000]}]}


def header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def divider() -> dict:
    return {"type": "divider"}


def confirm_dialog(title: str, text: str, ok: str = "확인", deny: str = "취소") -> dict:
    return {"title": {"type": "plain_text", "text": title},
            "text": {"type": "mrkdwn", "text": text},
            "confirm": {"type": "plain_text", "text": ok},
            "deny": {"type": "plain_text", "text": deny}}


def modal_value(view: dict, block_id: str, action_id: str = "v"):
    """뷰 state에서 입력값을 꺼냅니다 (타입별로 알맞은 필드 반환)."""
    v = view["state"]["values"].get(block_id, {}).get(action_id, {})
    t = v.get("type")
    if t == "plain_text_input":
        return (v.get("value") or "").strip()
    if t == "users_select":
        return v.get("selected_user")
    if t == "multi_users_select":
        return v.get("selected_users") or []
    if t == "datepicker":
        return v.get("selected_date")
    if t == "timepicker":
        return v.get("selected_time")
    if t in ("static_select", "radio_buttons"):
        opt = v.get("selected_option")
        return opt["value"] if opt else None
    return None
