"""날짜/시간 표현 파싱.
영어: 'today', 'tomorrow', 'fri', 'by friday', 'next tue', '10/5', '2026-10-05', '14:00', '3pm', 'at 3:30pm'
한국어: '오늘', '내일', '금요일까지', '다음주 화요일', '오후 3시', '3시 반'"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

WEEKDAYS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
WEEKDAY_NAMES = "월화수목금토일"

# 영어 요일. 'sat'/'sun'은 일반 단어와 겹치기 쉬워 문장 안에서는 전체 이름만 받습니다.
_EN_WD = r"(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|saturday|sunday)"
_EN_WD_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_EN_WD_TOKENS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
                 "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
                 "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}
_BY = r"(?:\b(?:by|due|until)\s+)?"


def weekday_index(token: str) -> int | None:
    """단독 토큰의 요일: '화', '화요일', 'tue', 'Tuesday' → 1 (sat/sun 약어 허용)"""
    token = token.strip()
    m = re.fullmatch(r"([월화수목금토일])(?:요일)?", token)
    if m:
        return WEEKDAYS[m.group(1)]
    return _EN_WD_TOKENS.get(token.lower())


_UNTIL = r"(?:까지)?"
_DATE_PATTERNS = [
    ("iso", re.compile(_BY + r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)" + _UNTIL)),
    ("md", re.compile(_BY + r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])" + _UNTIL)),
    ("next_wd", re.compile(r"다음\s?주\s?([월화수목금토일])요일" + _UNTIL)),
    ("wd", re.compile(r"(?:이번\s?주\s?)?([월화수목금토일])요일" + _UNTIL)),
    ("rel", re.compile(r"(오늘|내일|모레)" + _UNTIL)),
    ("next_wd_en", re.compile(_BY + r"\bnext\s+" + _EN_WD + r"\b", re.I)),
    ("wd_en", re.compile(_BY + r"\b(?:this\s+)?" + _EN_WD + r"\b", re.I)),
    ("rel_en", re.compile(_BY + r"\b(today|tonight|eod|tomorrow|tmrw|tmr)\b", re.I)),
]
_TIME_PATTERNS = [
    ("ampm", re.compile(r"(?:\b(?:at|by)\s+)?(?<![\d:])(1[0-2]|0?[1-9])(?::([0-5]\d))?\s?(am|pm)\b", re.I)),
    ("hm", re.compile(r"(?:\b(?:at|by)\s+)?(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)" + _UNTIL, re.I)),
    ("ko", re.compile(r"(오전|오후)?\s?(?<!\d)(\d{1,2})시(?!간)(?:\s?(\d{1,2})분|\s?반)?" + _UNTIL)),
]
_REL = {"오늘": 0, "내일": 1, "모레": 2, "today": 0, "tonight": 0, "eod": 0, "tomorrow": 1, "tmrw": 1, "tmr": 1}


def _strip(text: str, m: re.Match) -> str:
    return (text[: m.start()] + " " + text[m.end():]).strip()


def parse_due(text: str, now: datetime) -> tuple[datetime | None, bool, str]:
    """텍스트에서 마감 표현을 찾아 (마감 datetime, 시각 지정 여부, 남은 텍스트)를 돌려줍니다.

    now는 tz-aware 로컬 시각이어야 합니다. 시각이 없으면 해당 날짜 23:59로 둡니다.
    """
    today = now.date()
    d: date | None = None
    t: time | None = None

    for kind, pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            if kind == "iso":
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif kind == "md":
                d = date(today.year, int(m.group(1)), int(m.group(2)))
                if d < today:
                    d = date(today.year + 1, d.month, d.day)
            elif kind == "next_wd":
                next_monday = today + timedelta(days=7 - today.weekday())
                d = next_monday + timedelta(days=WEEKDAYS[m.group(1)])
            elif kind == "wd":
                d = today + timedelta(days=(WEEKDAYS[m.group(1)] - today.weekday()) % 7)
            elif kind in ("rel", "rel_en"):
                d = today + timedelta(days=_REL[m.group(1).lower()])
            elif kind == "next_wd_en":
                next_monday = today + timedelta(days=7 - today.weekday())
                d = next_monday + timedelta(days=_EN_WD_INDEX[m.group(1)[:3].lower()])
            elif kind == "wd_en":
                d = today + timedelta(days=(_EN_WD_INDEX[m.group(1)[:3].lower()] - today.weekday()) % 7)
        except ValueError:  # 2/30 같은 잘못된 날짜
            continue
        text = _strip(text, m)
        break

    for kind, pat in _TIME_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        if kind == "ampm":
            h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            t = time(h, int(m.group(2) or 0))
        elif kind == "hm":
            t = time(int(m.group(1)), int(m.group(2)))
        else:
            h = int(m.group(2))
            if h > 24:
                continue
            minute = int(m.group(3)) if m.group(3) else (30 if "반" in m.group(0) else 0)
            if m.group(1) == "오후" and h < 12:
                h += 12
            elif m.group(1) == "오전" and h == 12:
                h = 0
            elif m.group(1) is None and 1 <= h <= 7:
                h += 12  # '3시'는 보통 오후 3시
            if h == 24 or minute > 59:
                continue
            t = time(h, minute)
        text = _strip(text, m)
        break

    text = re.sub(r"\s+", " ", text).strip()
    if d is None and t is None:
        return None, False, text
    if d is None:  # 시각만 있으면 오늘(이미 지났으면 내일)
        d = today if datetime.combine(today, t, now.tzinfo) > now else today + timedelta(days=1)
    has_time = t is not None
    due = datetime.combine(d, t or time(23, 59), now.tzinfo)
    return due, has_time, text


def parse_hhmm(token: str) -> time | None:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", token.strip())
    return time(int(m.group(1)), int(m.group(2))) if m else None


def parse_duration(token: str) -> timedelta | None:
    """'3h', '90m', '2d', '1h30m', '3시간', '2일' → timedelta"""
    token = token.strip().lower()
    total = timedelta()
    found = False
    for num, unit in re.findall(r"(\d+)\s*(d|h|m|일|시간|분)", token):
        found = True
        n = int(num)
        total += {"d": timedelta(days=n), "일": timedelta(days=n),
                  "h": timedelta(hours=n), "시간": timedelta(hours=n),
                  "m": timedelta(minutes=n), "분": timedelta(minutes=n)}[unit]
    if not found or re.sub(r"(\d+)\s*(d|h|m|일|시간|분)", "", token).strip():
        return None
    return total if total > timedelta() else None


def fmt_due(due: datetime, has_time: bool, now: datetime) -> str:
    d = due.date()
    diff = (d - now.date()).days
    label = {0: "오늘", 1: "내일", 2: "모레", -1: "어제"}.get(diff)
    if label is None:
        label = f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})"
    if has_time:
        label += f" {due:%H:%M}"
    return label


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, m = divmod(seconds // 60, 60)
    if h and m:
        return f"{h}시간 {m}분"
    if h:
        return f"{h}시간"
    return f"{m}분"
