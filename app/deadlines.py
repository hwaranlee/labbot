"""학회 데드라인: 학회 목록(DB)·마감·목표 학회 등록, D-day 알림, 주간 요약.
학회 목록은 처음 실행할 때만 conferences.yaml에서 채우고, 그 뒤로는 DB가 기준입니다."""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ctx import Ctx, context, divider, modal_value, section
from .timeparse import WEEKDAY_NAMES

log = logging.getLogger("labbot.deadlines")

KINDS = {"abstract": "초록 마감", "paper": "논문 마감", "supplementary": "부록 마감",
         "arr_cycle": "투고 마감", "commitment": "commitment 마감"}
KIND_ALIASES = {"supp": "supplementary", "cycle": "arr_cycle", "arr": "arr_cycle", "commit": "commitment",
                "full": "paper"}
REVIEW_TYPES = {"direct": "직접 투고", "arr": "ARR → commit", "arr_cycle": "ARR 주기"}
STAGES = (30, 14, 7, 3, 1, 0)       # D-n 알림
UPCOMING_DAYS = 90                  # /deadline 기본 범위
WEEKLY_DAYS = 42                    # 월요일 채널 요약 범위 (6주)
CHECK_NOTE = "⚠️ 공식 사이트에서 날짜를 꼭 확인하세요"
SEED_KEY = "conferences_seeded"
SEED_FILE = os.path.join(os.path.dirname(__file__), "conferences.yaml")

HELP = """*🎯 /deadline 사용법*
• `/deadline` — 앞으로 90일 안의 마감 (D-day 순)
• `/deadline target ICLR [논문 제목]` — 내 목표 학회 등록 (D-30·14·7·3·1·당일 DM 알림)
   ARR 계열(ACL 등)은 가장 가까운 ARR 주기 마감과 commitment 마감을 함께 알려 드려요
• `/deadline untarget ICLR` · `/deadline mine` — 목표 해제 · 내 목표 학회
• `/deadline targets` — 누가 어느 학회를 준비 중인지
• `/deadline set ICLR 2027 paper 2026-10-01 23:59 AoE` — 마감 추가·수정 (시간대를 빼면 학회 기본값)
   종류: `abstract` `paper` `supplementary` `arr_cycle` `commitment` · 시간대: `AoE` `UTC-7` `KST` `America/Los_Angeles`
• `/deadline unset ICLR 2027 abstract` — 마감 삭제

*학회 목록*
• `/deadline conf list` — 그룹별 학회와 다음 마감
• `/deadline conf add` — 입력 창으로 추가 · 빠른 추가 `/deadline conf add KDD group:ML url:https://kdd.org tz:AoE`
   (`type:arr` ARR commit 학회 · `type:arr_cycle commit:ACL,EMNLP` ARR 주기)
• `/deadline conf edit KDD` — 학회 정보와 마감 목록 수정
• `/deadline conf remove KDD` — 삭제 (목표로 등록한 사람과 알림도 함께 정리)
• `/deadline conf watch KDD` · `/deadline conf unwatch KDD` — 월요일 채널 요약에 포함 여부
• `/deadline conf export` — 현재 목록과 마감을 yaml로 DM 받기 (백업·이전용)
_추가는 누구나, 수정·삭제는 추가한 사람이나 관리자만 할 수 있어요._"""


# ── 시간대 ─────────────────────────────────────────────────
_TZ_ALIASES = {"kst": "Asia/Seoul", "pt": "America/Los_Angeles", "pst": "America/Los_Angeles",
               "pdt": "America/Los_Angeles", "et": "America/New_York", "est": "America/New_York",
               "edt": "America/New_York", "cet": "Europe/Berlin", "cest": "Europe/Berlin"}


def parse_tz(label: str) -> tzinfo | None:
    """'AoE'(UTC-12), 'UTC', 'UTC-7', 'GMT+5:30', 'KST', 'PT', IANA 이름 → tzinfo"""
    s = (label or "").strip()
    low = s.lower()
    if low in ("aoe", "anywhere-on-earth"):
        return timezone(timedelta(hours=-12))
    if low in ("utc", "gmt", "z"):
        return timezone.utc
    m = re.fullmatch(r"(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", low)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if h > 14 or mi >= 60:
            return None
        sign = -1 if m.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=h, minutes=mi))
    try:
        return ZoneInfo(_TZ_ALIASES.get(low, s))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def norm_tz(label: str) -> str | None:
    """저장용 이름으로 정리: 'aoe' → 'AoE', 'utc-07:00' → 'UTC-7'. 모르는 시간대면 None"""
    s = (label or "").strip()
    if parse_tz(s) is None:
        return None
    low = s.lower()
    if low in ("aoe", "anywhere-on-earth"):
        return "AoE"
    if low in ("utc", "gmt", "z"):
        return "UTC"
    m = re.fullmatch(r"(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", low)
    if m:
        mi = int(m.group(3) or 0)
        return f"UTC{m.group(1)}{int(m.group(2))}" + (f":{mi:02d}" if mi else "")
    return s.upper() if low in _TZ_ALIASES else s


def to_epoch(local: datetime, tz_label: str) -> int:
    """tz_label 기준 현지 시각(naive) → UTC epoch"""
    return int(local.replace(tzinfo=parse_tz(tz_label)).timestamp())


def _local_label(ctx: Ctx) -> str:
    return "KST" if str(ctx.tz) == "Asia/Seoul" else str(ctx.tz)


def _md(d) -> str:
    return f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})"


def when_text(ctx: Ctx, due_at: int, tz_label: str) -> str:
    """'10/2(금) 20:59 KST (AoE 10/1 23:59)'"""
    kst = ctx.local(due_at)
    out = f"{_md(kst)} {kst:%H:%M} {_local_label(ctx)}"
    tz = parse_tz(tz_label)
    if tz is not None and tz_label not in ("KST", "Asia/Seoul"):
        orig = datetime.fromtimestamp(due_at, tz)
        if orig.utcoffset() != kst.utcoffset():
            out += f" ({tz_label} {orig.month}/{orig.day} {orig:%H:%M})"
    return out


def days_left(ctx: Ctx, due_at: int) -> int:
    return (ctx.local(due_at).date() - ctx.today()).days


def dday(n: int) -> str:
    return "D-day" if n == 0 else (f"D-{n}" if n > 0 else f"D+{-n}")


def _ro(s: str) -> str:
    """숫자로 끝나는 말 뒤의 '(으)로'"""
    return s + ("로" if s and s[-1] in "2459" else "으로")


# ── 학회 ───────────────────────────────────────────────────
def _key(name: str) -> str:
    return re.sub(r"[\s_\-]", "", (name or "").lower())


def all_confs(ctx: Ctx):
    return ctx.db.all("SELECT * FROM conferences ORDER BY grp, name COLLATE NOCASE")


def get_conf(ctx: Ctx, conf_id: int):
    return ctx.db.one("SELECT * FROM conferences WHERE id=?", (conf_id,))


def find_conf(ctx: Ctx, name: str):
    """(학회, 비슷한 이름 제안). 대소문자·공백 무시 (iclr = ICLR, arr2027jan = ARR 2027 Jan)"""
    key = _key(name)
    rows = all_confs(ctx)
    for r in rows:
        if _key(r["name"]) == key:
            return r, None
    if not key or not rows:
        return None, None
    keys = {_key(r["name"]): r["name"] for r in rows}
    # 글자 순서만 바뀐 오타(icrl → ICLR)를 같은 점수의 다른 이름보다 먼저 제안
    best = max(keys, key=lambda k: (difflib.SequenceMatcher(None, key, k).ratio() >= 0.6 and sorted(k) == sorted(key),
                                    difflib.SequenceMatcher(None, key, k).ratio()))
    if difflib.SequenceMatcher(None, key, best).ratio() >= 0.6:
        return None, keys[best]
    pre = [n for k, n in keys.items() if k.startswith(key) or key.startswith(k)]
    return None, (pre[0] if pre else None)


def not_found(name: str, suggestion: str | None) -> str:
    return (f"'{name}' 학회를 찾을 수 없어요." + (f" 혹시 *{suggestion}*?" if suggestion else "")
            + " 목록은 `/deadline conf list`")


def can_edit(ctx: Ctx, uid: str, conf) -> bool:
    return ctx.is_admin(uid) or conf["created_by"] == uid


def no_perm(conf) -> str:
    who = f"추가한 사람(<@{conf['created_by']}>)이나 " if conf["created_by"].startswith(("U", "W")) else ""
    return f"*{conf['name']}* 은(는) {who}관리자(ADMIN_IDS)만 고칠 수 있어요."


def validate_conf(ctx: Ctx, name: str, tz: str, review_type: str, conf_id: int | None = None) -> str | None:
    if not name or len(name) > 40:
        return "학회 이름은 1~40자로 입력해 주세요."
    if re.fullmatch(r"\d+", name):
        return "학회 이름은 숫자만으로 쓸 수 없어요."
    for r in all_confs(ctx):
        if _key(r["name"]) == _key(name) and r["id"] != conf_id:
            return f"이미 *{r['name']}* 학회가 있어요."
    if norm_tz(tz) is None:
        return f"시간대 '{tz}'를 알 수 없어요. 예: AoE, UTC-7, KST, America/Los_Angeles"
    if review_type not in REVIEW_TYPES:
        return "review_type은 direct, arr, arr_cycle 중 하나예요."
    return None


def add_conf(ctx: Ctx, uid: str, name: str, group: str = "", url: str = "", review_type: str = "direct",
             tz: str = "AoE", watch: bool = True) -> int:
    return ctx.db.run(
        "INSERT INTO conferences(name, grp, official_url, review_type, default_tz, watch, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)", (name.strip(), group.strip(), url.strip(), review_type, norm_tz(tz),
                                      1 if watch else 0, uid, int(ctx.now().timestamp())))


def update_conf(ctx: Ctx, conf_id: int, name: str, group: str, url: str, review_type: str, tz: str) -> None:
    ctx.db.run("UPDATE conferences SET name=?, grp=?, official_url=?, review_type=?, default_tz=? WHERE id=?",
               (name.strip(), group.strip(), url.strip(), review_type, norm_tz(tz), conf_id))


def targeters(ctx: Ctx, conf_id: int) -> list:
    return ctx.db.all("SELECT * FROM conf_targets WHERE conf_id=? ORDER BY created_at", (conf_id,))


def delete_conf(ctx: Ctx, conf_id: int) -> list[str]:
    """학회와 마감·목표·보낸 알림 기록·ARR 연결을 지우고, 목표로 등록했던 사람 목록을 돌려줍니다."""
    users = [t["user_id"] for t in targeters(ctx, conf_id)]
    with ctx.db.lock:
        ctx.db.run("DELETE FROM deadline_alerts WHERE deadline_id IN (SELECT id FROM conf_deadlines WHERE conf_id=?)",
                   (conf_id,))
        ctx.db.run("DELETE FROM conf_deadlines WHERE conf_id=?", (conf_id,))
        ctx.db.run("DELETE FROM conf_targets WHERE conf_id=?", (conf_id,))
        ctx.db.run("DELETE FROM arr_links WHERE cycle_id=? OR conf_id=?", (conf_id, conf_id))
        ctx.db.run("DELETE FROM conferences WHERE id=?", (conf_id,))
    return users


def linked(ctx: Ctx, conf) -> list:
    """ARR 주기면 commit할 수 있는 학회들, ARR 학회면 연결된 주기들"""
    if conf["review_type"] == "arr_cycle":
        sql = ("SELECT c.* FROM arr_links l JOIN conferences c ON c.id=l.conf_id WHERE l.cycle_id=?"
               " ORDER BY c.name COLLATE NOCASE")
    else:
        sql = ("SELECT c.* FROM arr_links l JOIN conferences c ON c.id=l.cycle_id WHERE l.conf_id=?"
               " ORDER BY c.name COLLATE NOCASE")
    return ctx.db.all(sql, (conf["id"],))


def set_links(ctx: Ctx, cycle_id: int, conf_ids: list[int]) -> None:
    ctx.db.run("DELETE FROM arr_links WHERE cycle_id=?", (cycle_id,))
    for cid in dict.fromkeys(conf_ids):
        ctx.db.run("INSERT OR IGNORE INTO arr_links(cycle_id, conf_id) VALUES (?,?)", (cycle_id, cid))


# ── 마감 ───────────────────────────────────────────────────
def conf_deadlines(ctx: Ctx, conf_id: int, upcoming_only: bool = False):
    sql = "SELECT * FROM conf_deadlines WHERE conf_id=?"
    params: tuple = (conf_id,)
    if upcoming_only:
        sql += " AND due_at>?"
        params += (int(ctx.now().timestamp()),)
    return ctx.db.all(sql + " ORDER BY due_at", params)


def deadline_title(conf, dl) -> str:
    """'ICLR 2027 논문 마감', 'ARR 2027 Jan 투고 마감'"""
    name = conf["name"] if conf["review_type"] == "arr_cycle" else f"{conf['name']} {dl['year']}"
    return f"{name} {KINDS.get(dl['kind'], dl['kind'])}"


def dl_url(conf, dl=None) -> str:
    return (dl["official_url"] if dl is not None and dl["official_url"] else "") or conf["official_url"] or ""


def deadline_line(ctx: Ctx, conf, dl, with_dday: bool = True) -> str:
    """'ICLR 2027 논문 마감 · 10/2(금) 20:59 KST (AoE 10/1 23:59) · D-7' (초록 마감은 굵게)"""
    title = deadline_title(conf, dl)
    if dl["kind"] == "abstract":
        title = f"📝 *{title}*"
    out = f"{title} · {when_text(ctx, dl['due_at'], dl['tz'])}"
    if with_dday:
        n = days_left(ctx, dl["due_at"])
        out += f" · *{dday(n)}*" if 0 <= n <= 7 else f" · {dday(n)}"
    return out


def undated_line(conf, kind: str | None = None) -> str:
    what = conf["name"] + (f" {KINDS[kind]}" if kind else "")
    return f"{what} · _날짜 미정 · `/deadline set` 으로 추가_"


def set_deadline(ctx: Ctx, conf, year: int, kind: str, local: datetime, tz: str | None, uid: str,
                 url: str | None = None, note: str | None = None, notify: bool = True) -> tuple[int, int | None]:
    """마감 추가·수정. (deadline id, 바뀌기 전 due_at 또는 None)을 돌려주고, 날짜가 바뀌면 목표 등록자에게 DM.
    url·note가 None이면 기존 값을 유지합니다."""
    label = norm_tz(tz) if tz else conf["default_tz"]
    due = to_epoch(local, label)
    old = ctx.db.one("SELECT * FROM conf_deadlines WHERE conf_id=? AND year=? AND kind=?", (conf["id"], year, kind))
    now = int(ctx.now().timestamp())
    if old:
        ctx.db.run("UPDATE conf_deadlines SET due_at=?, tz=?, official_url=?, note=?, updated_by=?, updated_at=?"
                   " WHERE id=?", (due, label, old["official_url"] if url is None else url,
                                   old["note"] if note is None else note, uid, now,
                                   old["id"]))
        did = old["id"]
    else:
        did = ctx.db.run("INSERT INTO conf_deadlines(conf_id, year, kind, due_at, tz, official_url, note,"
                         " updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (conf["id"], year, kind, due, label, url or "", note or "", uid, now))
    dl = ctx.db.one("SELECT * FROM conf_deadlines WHERE id=?", (did,))
    if notify and (not old or old["due_at"] != due):
        notify_change(ctx, conf, dl, old["due_at"] if old else None)
    return did, (old["due_at"] if old else None)


def unset_deadline(ctx: Ctx, conf, dl, notify: bool = True) -> None:
    ctx.db.run("DELETE FROM deadline_alerts WHERE deadline_id=?", (dl["id"],))
    ctx.db.run("DELETE FROM conf_deadlines WHERE id=?", (dl["id"],))
    if notify and dl["due_at"] > ctx.now().timestamp():
        _notify(ctx, conf, dl, f"🗑 *{deadline_title(conf, dl)}* 이 목록에서 삭제됐어요.")


def watchers_of(ctx: Ctx, conf) -> list[str]:
    """이 학회(ARR 주기면 연결된 학회)를 목표로 등록한 사람"""
    ids = [conf["id"]] + ([c["id"] for c in linked(ctx, conf)] if conf["review_type"] == "arr_cycle" else [])
    rows = ctx.db.all(f"SELECT DISTINCT user_id FROM conf_targets WHERE conf_id IN ({','.join('?' * len(ids))})",
                      tuple(ids))
    return [r["user_id"] for r in rows]


def _notice(text: str, url: str) -> list:
    link = f"<{url}|🔗 공식 사이트> · " if url else ""
    return [section(text), context(link + CHECK_NOTE)]


def _notify(ctx: Ctx, conf, dl, text: str) -> None:
    for uid in watchers_of(ctx, conf):
        ctx.dm(uid, re.sub(r"[*_]", "", text), _notice(text, dl_url(conf, dl)))


def notify_change(ctx: Ctx, conf, dl, old_due: int | None) -> None:
    if dl["due_at"] <= ctx.now().timestamp():
        return
    new = ctx.local(dl["due_at"])
    if old_due is None:
        text = f"🆕 *{deadline_title(conf, dl)}* 날짜가 등록됐어요: {when_text(ctx, dl['due_at'], dl['tz'])}"
    else:
        old = ctx.local(old_due)
        a, b = f"{old.month}/{old.day}", f"{new.month}/{new.day}"
        if a == b:
            a, b = f"{a} {old:%H:%M}", f"{b} {new:%H:%M}"
        text = (f"🔔 *{deadline_title(conf, dl)}* 이 {a} → {_ro(b)} 바뀌었어요.\n"
                f"새 마감: {when_text(ctx, dl['due_at'], dl['tz'])} · {dday(days_left(ctx, dl['due_at']))}")
    _notify(ctx, conf, dl, text)


SET_RE = re.compile(
    r"^(?P<name>.+?)\s+(?:(?P<year>\d{4})\s+)?(?P<kind>[a-z_]+)\s+(?P<date>\d{4}-\d{1,2}-\d{1,2})"
    r"(?:\s+(?P<time>\d{1,2}:\d{2}))?(?:\s+(?P<tz>\S+))?\s*$", re.I)
UNSET_RE = re.compile(r"^(?P<name>.+?)(?:\s+(?P<year>\d{4}))?\s+(?P<kind>[a-z_]+)\s*$", re.I)


def norm_kind(s: str) -> str | None:
    k = (s or "").lower()
    k = KIND_ALIASES.get(k, k)
    return k if k in KINDS else None


def parse_set(text: str) -> tuple[dict | None, str | None]:
    """'ICLR 2027 paper 2026-10-01 23:59 AoE' → {name, year, kind, local, tz}"""
    usage = "형식: `/deadline set ICLR 2027 paper 2026-10-01 23:59 AoE` (시각·시간대는 생략 가능)"
    m = SET_RE.match(text.strip())
    if not m:
        return None, usage
    kind = norm_kind(m.group("kind"))
    if not kind:
        return None, f"마감 종류 '{m.group('kind')}'를 모르겠어요. ({', '.join(KINDS)})"
    try:
        d = date.fromisoformat("-".join(f"{int(x):02d}" for x in m.group("date").split("-")))
        hh, mm = (int(x) for x in (m.group("time") or "23:59").split(":"))
        local = datetime(d.year, d.month, d.day, hh, mm)
    except ValueError:
        return None, f"날짜·시각을 이해하지 못했어요. {usage}"
    tz = m.group("tz")
    if tz and norm_tz(tz) is None:
        return None, f"시간대 '{tz}'를 알 수 없어요. 예: AoE, UTC-7, KST, America/Los_Angeles"
    year = int(m.group("year")) if m.group("year") else None
    return {"name": m.group("name").strip(), "year": year, "kind": kind, "local": local, "tz": tz}, None


def cmd_set(ctx: Ctx, uid: str, text: str) -> str:
    p, err = parse_set(text)
    if err:
        return err
    conf, sug = find_conf(ctx, p["name"])
    if not conf:
        return not_found(p["name"], sug)
    if not can_edit(ctx, uid, conf):
        return no_perm(conf)
    year = p["year"] or (p["local"].year if p["kind"] == "arr_cycle" else None)
    if not year:
        return f"연도를 넣어 주세요. 예: `/deadline set {conf['name']} {p['local'].year + 1} {p['kind']} ...`"
    did, old = set_deadline(ctx, conf, year, p["kind"], p["local"], p["tz"], uid)
    dl = ctx.db.one("SELECT * FROM conf_deadlines WHERE id=?", (did,))
    head = "✏️ 마감을 바꿨어요" if old is not None else "✅ 마감을 추가했어요"
    if old is not None and old != dl["due_at"]:
        head += f" (이전: {when_text(ctx, old, dl['tz'])})"
    return f"{head}\n{deadline_line(ctx, conf, dl)}"


def cmd_unset(ctx: Ctx, uid: str, text: str) -> str:
    m = UNSET_RE.match(text.strip())
    kind = norm_kind(m.group("kind")) if m else None
    if not m or not kind:
        return "형식: `/deadline unset ICLR 2027 abstract`"
    conf, sug = find_conf(ctx, m.group("name"))
    if not conf:
        return not_found(m.group("name"), sug)
    if not can_edit(ctx, uid, conf):
        return no_perm(conf)
    rows = [d for d in conf_deadlines(ctx, conf["id"]) if d["kind"] == kind
            and (not m.group("year") or d["year"] == int(m.group("year")))]
    if not rows:
        return f"{conf['name']} {m.group('year') or ''} {KINDS[kind]}이 없어요.".replace("  ", " ")
    if len(rows) > 1:
        return "연도를 넣어 주세요: " + ", ".join(f"`{d['year']}`" for d in rows)
    unset_deadline(ctx, conf, rows[0])
    return f"🗑 {deadline_title(conf, rows[0])}을 지웠어요."


# ── 목표 학회 ──────────────────────────────────────────────
def alert_deadlines(ctx: Ctx, conf) -> list[tuple]:
    """목표 학회로 알림받을 마감들: [(학회, 마감 또는 None(날짜 미정), kind)]
    ARR 학회는 가장 가까운 ARR 주기 마감 + commitment 마감."""
    out: list[tuple] = []
    if conf["review_type"] == "arr":
        cycles = linked(ctx, conf)
        nxt = []
        for c in cycles:
            nxt += [(c, d) for d in conf_deadlines(ctx, c["id"], upcoming_only=True) if d["kind"] == "arr_cycle"]
        if nxt:
            out.append(min(nxt, key=lambda x: x[1]["due_at"]) + ("arr_cycle",))
        else:
            out += [(c, None, "arr_cycle") for c in cycles if not conf_deadlines(ctx, c["id"])][:1]
        mine = conf_deadlines(ctx, conf["id"], upcoming_only=True)
        out += [(conf, d, d["kind"]) for d in mine]
        if not any(d["kind"] == "commitment" for d in mine):
            out.append((conf, None, "commitment"))
        return out
    mine = conf_deadlines(ctx, conf["id"], upcoming_only=True)
    if mine:
        return [(conf, d, d["kind"]) for d in mine]
    return [(conf, None, "arr_cycle" if conf["review_type"] == "arr_cycle" else None)]


def _alert_lines(ctx: Ctx, items) -> list[str]:
    lines = []
    for c, d, kind in items:
        lines.append(deadline_line(ctx, c, d) if d is not None else undated_line(c, kind))
    return lines


def add_target(ctx: Ctx, uid: str, text: str) -> tuple[str, list | None]:
    name, title = text, ""
    conf, sug = find_conf(ctx, text)
    if not conf:  # 'ICLR 논문 제목'처럼 뒤에 제목이 붙은 경우: 가장 긴 앞부분부터 찾기
        words = text.split()
        for i in range(len(words) - 1, 0, -1):
            c, _ = find_conf(ctx, " ".join(words[:i]))
            if c:
                conf, name, title = c, " ".join(words[:i]), " ".join(words[i:])
                break
        else:
            name = words[0] if words else text
            if len(words) > 1:
                _c, sug = find_conf(ctx, name)
    if not conf:
        return not_found(name, sug), None
    title = title.strip().strip('"“”')[:200]
    ctx.db.run("INSERT INTO conf_targets(user_id, conf_id, title, created_at) VALUES (?,?,?,?)"
               " ON CONFLICT(user_id, conf_id) DO UPDATE SET title=excluded.title",
               (uid, conf["id"], title, int(ctx.now().timestamp())))
    ctx.refresh_home(uid)
    items = alert_deadlines(ctx, conf)
    head = f"🎯 *{conf['name']}* 을(를) 목표 학회로 등록했어요" + (f" · _{title}_" if title else "")
    if conf["review_type"] == "arr":
        head += ("\nARR을 거쳐 commit하는 학회라서 *ARR 주기 투고 마감*과 *commitment 마감*을 둘 다 알려 드릴게요.")
    body = "\n".join(f"• {x}" for x in _alert_lines(ctx, items))
    blocks = _notice(f"{head}\n{body}\nD-30 · 14 · 7 · 3 · 1 · 당일 오전에 DM을 보내요. "
                     f"날짜가 바뀌거나 새로 등록되면 바로 알려 드려요.", dl_url(conf))
    return head, blocks


def remove_target(ctx: Ctx, uid: str, name: str) -> str:
    conf, sug = find_conf(ctx, name)
    if not conf:
        return not_found(name, sug)
    n = ctx.db.run("DELETE FROM conf_targets WHERE user_id=? AND conf_id=?", (uid, conf["id"]))
    ctx.refresh_home(uid)
    return f"🎯 {conf['name']} 목표를 해제했어요." if n else f"{conf['name']}은(는) 목표 학회가 아니에요."


def my_targets(ctx: Ctx, uid: str) -> list[tuple]:
    """[(target, conf, alert items)] — 가장 가까운 마감 순"""
    out = []
    for t in ctx.db.all("SELECT * FROM conf_targets WHERE user_id=?", (uid,)):
        conf = get_conf(ctx, t["conf_id"])
        if conf:
            out.append((t, conf, alert_deadlines(ctx, conf)))
    return sorted(out, key=lambda x: _nearest(x[2]))


def _nearest(items) -> int:
    return min((d["due_at"] for _c, d, _k in items if d is not None), default=2 ** 40)


def mine_blocks(ctx: Ctx, uid: str) -> list:
    rows = my_targets(ctx, uid)
    if not rows:
        return [section("*🎯 내 목표 학회*\n아직 없어요. `/deadline target ICLR [논문 제목]` 으로 등록하세요.")]
    b = [section("*🎯 내 목표 학회*")]
    for t, conf, items in rows[:20]:
        head = f"*{conf['name']}*" + (f" · _{t['title']}_" if t["title"] else "")
        b.append(section(head + "\n" + "\n".join(f"• {x}" for x in _alert_lines(ctx, items)),
                         _link_button(dl_url(conf))))
    b.append(context(CHECK_NOTE + " · 해제: `/deadline untarget 학회`"))
    return b


def _link_button(url: str) -> dict | None:
    if not url:
        return None
    return {"type": "button", "text": {"type": "plain_text", "text": "공식 사이트"}, "url": url[:3000],
            "action_id": "deadline_link", "value": "link"}


def targets_blocks(ctx: Ctx) -> list:
    rows = ctx.db.all("SELECT * FROM conf_targets ORDER BY created_at")
    by_conf: dict[int, list] = {}
    for t in rows:
        by_conf.setdefault(t["conf_id"], []).append(t)
    if not by_conf:
        return [section("*🎯 연구실 목표 학회*\n아직 아무도 등록하지 않았어요. `/deadline target ICLR` 처럼 등록하세요.")]
    entries = []
    for cid, ts in by_conf.items():
        conf = get_conf(ctx, cid)
        if not conf:
            continue
        items = alert_deadlines(ctx, conf)
        dated = [(c, d) for c, d, _k in items if d is not None]
        nxt = min(dated, key=lambda x: x[1]["due_at"]) if dated else None
        when = (f"{KINDS[nxt[1]['kind']]} {dday(days_left(ctx, nxt[1]['due_at']))}" if nxt else "날짜 미정")
        people = ", ".join(ctx.name(t["user_id"]) + (f" (_{t['title']}_)" if t["title"] else "") for t in ts)
        entries.append((_nearest(items), f"• *{conf['name']}* · {when} — {people}"))
    lines = [x for _n, x in sorted(entries)]
    return _chunks(f"*🎯 연구실 목표 학회* ({len(rows)}건)", lines) + [context(CHECK_NOTE)]


def weekly_line(ctx: Ctx, uid: str) -> str:
    """금요일 주간 정리용 한 줄: '🎯 목표 학회까지 ICLR 논문 마감 D-12 · ACL commitment 마감 D-40'"""
    parts = []
    for _t, conf, items in my_targets(ctx, uid):
        dated = [(c, d) for c, d, _k in items if d is not None]
        if dated:
            c, d = min(dated, key=lambda x: x[1]["due_at"])
            parts.append(f"{deadline_title(c, d)} {dday(days_left(ctx, d['due_at']))}")
        else:
            parts.append(f"{conf['name']} 날짜 미정")
    return ("🎯 목표 학회까지 " + " · ".join(parts)) if parts else ""


def home_lines(ctx: Ctx, uid: str) -> list[str]:
    out = []
    for t, conf, items in my_targets(ctx, uid)[:5]:
        dated = [(c, d) for c, d, _k in items if d is not None]
        if dated:
            c, d = min(dated, key=lambda x: x[1]["due_at"])
            out.append(f"• *{conf['name']}* · {deadline_line(ctx, c, d)}")
        else:
            out.append(f"• *{conf['name']}* · 날짜 미정")
    return out


# ── 목록 화면 ──────────────────────────────────────────────
def _chunks(head: str, lines: list[str]) -> list:
    """긴 목록을 3000자 제한에 맞춰 여러 section으로"""
    blocks, buf = [], head
    for ln in lines:
        if len(buf) + len(ln) + 1 > 2900:
            blocks.append(section(buf))
            buf = ln
        else:
            buf += "\n" + ln
    blocks.append(section(buf))
    return blocks[:45]


def upcoming(ctx: Ctx, days: int, watched_only: bool = False) -> list[tuple]:
    now = int(ctx.now().timestamp())
    sql = ("SELECT d.*, c.id AS cid FROM conf_deadlines d JOIN conferences c ON c.id=d.conf_id"
           " WHERE d.due_at>? AND d.due_at<=?" + (" AND c.watch=1" if watched_only else "") + " ORDER BY d.due_at")
    return [(get_conf(ctx, d["cid"]), d) for d in ctx.db.all(sql, (now, now + days * 86400))]


def undated_confs(ctx: Ctx, watched_only: bool = True) -> list:
    """다음 마감이 없는 학회 (마감이 이미 지난 ARR 주기는 끝난 것이라 제외)"""
    now = int(ctx.now().timestamp())
    return ctx.db.all("SELECT * FROM conferences c WHERE NOT EXISTS (SELECT 1 FROM conf_deadlines d"
                      " WHERE d.conf_id=c.id AND (d.due_at>? OR c.review_type='arr_cycle'))"
                      + (" AND c.watch=1" if watched_only else "")
                      + " ORDER BY c.grp, c.name COLLATE NOCASE", (now,))


def upcoming_blocks(ctx: Ctx, days: int = UPCOMING_DAYS, watched_only: bool = False) -> list:
    items = upcoming(ctx, days, watched_only)
    head = f"*📅 앞으로 {days}일 안의 마감* ({len(items)})"
    lines = []
    for conf, d in items:
        url = dl_url(conf, d)
        lines.append(f"• {deadline_line(ctx, conf, d)}" + (f" · <{url}|사이트>" if url else ""))
    b = _chunks(head, lines or ["_이 기간에 등록된 마감이 없어요._"])
    nod = undated_confs(ctx, watched_only=True)
    if nod:
        b.append(context("날짜 미정: " + ", ".join(c["name"] for c in nod)[:2500] + " · `/deadline set` 으로 추가"))
    b.append(context(CHECK_NOTE + " · 내 목표 학회 알림: `/deadline target ICLR`"))
    return b


def conf_list_blocks(ctx: Ctx) -> list:
    groups: dict[str, list] = {}
    for c in all_confs(ctx):
        groups.setdefault(c["grp"] or "기타", []).append(c)
    if not groups:
        return [section("등록된 학회가 없어요. `/deadline conf add` 로 추가하세요.")]
    b = [section(f"*🏛 학회 목록* ({sum(len(v) for v in groups.values())}개)")]
    for g, confs in groups.items():
        lines = []
        for c in confs:
            nxt = conf_deadlines(ctx, c["id"], upcoming_only=True)
            tag = f"`{REVIEW_TYPES[c['review_type']]}`" + ("" if c["watch"] else " 🔕")
            name = f"<{c['official_url']}|{c['name']}>" if c["official_url"] else c["name"]
            if c["review_type"] == "arr_cycle":
                tag += " → " + (", ".join(x["name"] for x in linked(ctx, c)) or "연결된 학회 없음")
            past = conf_deadlines(ctx, c["id"])
            if nxt:
                lines.append(f"• *{name}* {tag}\n    " + "\n    ".join(deadline_line(ctx, c, d) for d in nxt[:3]))
            elif past and c["review_type"] == "arr_cycle":
                lines.append(f"• *{name}* {tag} · _종료 (투고 마감 {_md(ctx.local(past[-1]['due_at']))})_")
            elif past:
                lines.append(f"• *{name}* {tag} · _다음 일정 미정 (지난 마감 "
                             f"{_md(ctx.local(past[-1]['due_at']))}) · `/deadline set` 으로 추가_")
            else:
                lines.append(f"• *{name}* {tag} · _날짜 미정 · `/deadline set` 으로 추가_")
        b += _chunks(f"*{g}*", lines)
    b.append(context("🔕 = 월요일 채널 요약에서 제외 · 수정: `/deadline conf edit 이름` · "
                     "추가: `/deadline conf add`"))
    return b[:50]


# ── 모달 ───────────────────────────────────────────────────
def _opt(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text[:75]}, "value": value}


def _text_el(initial: str = "", multiline: bool = False, placeholder: str = "", max_length: int = 3000) -> dict:
    el = {"type": "plain_text_input", "action_id": "v", "multiline": multiline, "max_length": max_length}
    if initial:
        el["initial_value"] = initial[:max_length]
    if placeholder:
        el["placeholder"] = {"type": "plain_text", "text": placeholder[:150]}
    return el


def deadline_lines_text(ctx: Ctx, conf) -> str:
    lines = []
    for d in conf_deadlines(ctx, conf["id"]):
        local = datetime.fromtimestamp(d["due_at"], parse_tz(d["tz"]))
        ln = f"{d['year']} {d['kind']} {local:%Y-%m-%d %H:%M} {d['tz']}"
        if d["official_url"] or d["note"]:
            ln += f" | {d['official_url'] or ''}" + (f" | {d['note']}" if d["note"] else "")
        lines.append(ln)
    return "\n".join(lines)


def parse_deadline_lines(text: str, default_tz: str) -> tuple[list[dict], list[str]]:
    """'2027 paper 2026-10-01 23:59 AoE | url | 메모' 줄들 → [{year, kind, local, tz, url, note}]"""
    out, errors, seen = [], [], set()
    for n, raw in enumerate((text or "").splitlines(), 1):
        if not raw.strip():
            continue
        parts = [p.strip() for p in raw.split("|")]
        m = re.fullmatch(r"(\d{4})\s+([a-z_]+)\s+(\d{4}-\d{1,2}-\d{1,2})(?:\s+(\d{1,2}:\d{2}))?(?:\s+(\S+))?",
                         parts[0], re.I)
        kind = norm_kind(m.group(2)) if m else None
        if not m or not kind:
            errors.append(f"{n}번째 줄: `연도 종류 날짜 [시각] [시간대]` 형식이 아니에요")
            continue
        try:
            d = date.fromisoformat("-".join(f"{int(x):02d}" for x in m.group(3).split("-")))
            hh, mm = (int(x) for x in (m.group(4) or "23:59").split(":"))
            local = datetime(d.year, d.month, d.day, hh, mm)
        except ValueError:
            errors.append(f"{n}번째 줄: 날짜·시각이 올바르지 않아요")
            continue
        tz = norm_tz(m.group(5)) if m.group(5) else default_tz
        if not tz:
            errors.append(f"{n}번째 줄: 시간대 '{m.group(5)}'를 알 수 없어요")
            continue
        if (int(m.group(1)), kind) in seen:
            errors.append(f"{n}번째 줄: {m.group(1)} {kind}가 두 번 나와요")
            continue
        seen.add((int(m.group(1)), kind))
        url = _clean_url(parts[1]) if len(parts) > 1 else ""
        out.append({"year": int(m.group(1)), "kind": kind, "local": local, "tz": tz, "url": url,
                    "note": parts[2] if len(parts) > 2 else ""})
    return out, errors


def conf_modal(ctx: Ctx, conf=None) -> dict:
    rt_opts = [_opt(f"{k} · {v}", k) for k, v in REVIEW_TYPES.items()]
    rt = conf["review_type"] if conf else "direct"
    blocks = [
        {"type": "input", "block_id": "name", "label": {"type": "plain_text", "text": "학회 이름"},
         "hint": {"type": "plain_text", "text": "예: KDD, ARR 2027 Feb (연도 없이 학회 이름만, ARR 주기는 연도·월 포함)"},
         "element": _text_el(conf["name"] if conf else "", max_length=40)},
        {"type": "input", "block_id": "group", "optional": True, "label": {"type": "plain_text", "text": "그룹"},
         "element": _text_el(conf["grp"] if conf else "", placeholder="NLP / ML / Speech ...", max_length=30)},
        {"type": "input", "block_id": "url", "optional": True, "label": {"type": "plain_text", "text": "공식 사이트"},
         "element": _text_el(conf["official_url"] if conf else "", placeholder="https://...", max_length=300)},
        {"type": "input", "block_id": "review_type", "label": {"type": "plain_text", "text": "투고 방식"},
         "element": {"type": "static_select", "action_id": "v", "options": rt_opts,
                     "initial_option": next(o for o in rt_opts if o["value"] == rt)}},
        {"type": "input", "block_id": "tz", "label": {"type": "plain_text", "text": "기본 시간대"},
         "hint": {"type": "plain_text", "text": "AoE(=UTC-12), UTC-7, KST, America/Los_Angeles 등"},
         "element": _text_el(conf["default_tz"] if conf else "AoE", max_length=40)},
    ]
    arr_confs = [c for c in all_confs(ctx) if c["review_type"] == "arr"][:100]
    if arr_confs:
        el = {"type": "multi_static_select", "action_id": "v", "options": [_opt(c["name"], str(c["id"]))
                                                                            for c in arr_confs]}
        if conf and conf["review_type"] == "arr_cycle":
            cur = {c["id"] for c in linked(ctx, conf)}
            init = [o for o in el["options"] if int(o["value"]) in cur]
            if init:
                el["initial_options"] = init
        blocks.append({"type": "input", "block_id": "commit_to", "optional": True,
                       "label": {"type": "plain_text", "text": "ARR 주기일 때: commit할 수 있는 학회"},
                       "element": el})
    blocks.append({"type": "input", "block_id": "deadlines", "optional": True,
                   "label": {"type": "plain_text", "text": "마감 (한 줄에 하나)"},
                   "hint": {"type": "plain_text", "text": ("연도 종류 날짜 [시각] [시간대] | 링크 | 메모 · 예: 2027 paper "
                                                           "2026-10-01 23:59 AoE · 종류: abstract paper "
                                                           "supplementary arr_cycle commitment · 줄을 지우면 마감 삭제")},
                   "element": _text_el(deadline_lines_text(ctx, conf) if conf else "", multiline=True)})
    return {"type": "modal", "callback_id": "deadline_conf_modal",
            "private_metadata": json.dumps({"id": conf["id"] if conf else None}),
            "title": {"type": "plain_text", "text": "학회 수정" if conf else "학회 추가"},
            "submit": {"type": "plain_text", "text": "저장"},
            "close": {"type": "plain_text", "text": "취소"}, "blocks": blocks}


def remove_modal(ctx: Ctx, conf) -> dict:
    ts = targeters(ctx, conf["id"])
    n_dl = len(conf_deadlines(ctx, conf["id"]))
    txt = f"*{conf['name']}* 학회와 마감 {n_dl}개를 삭제할까요?"
    if ts:
        txt += (f"\n\n⚠️ 이 학회를 목표로 등록한 사람 {len(ts)}명:\n"
                + "\n".join(f"• <@{t['user_id']}>" + (f" · _{t['title']}_" if t["title"] else "") for t in ts[:30])
                + "\n삭제하면 이분들의 목표와 예약된 알림도 함께 정리되고, DM으로 알려 드려요.")
    return {"type": "modal", "callback_id": "deadline_conf_remove_modal",
            "private_metadata": json.dumps({"id": conf["id"]}),
            "title": {"type": "plain_text", "text": "학회 삭제"},
            "submit": {"type": "plain_text", "text": "삭제"},
            "close": {"type": "plain_text", "text": "취소"}, "blocks": [section(txt)]}


def _multi_values(view: dict, block_id: str) -> list[str]:
    v = view["state"]["values"].get(block_id, {}).get("v", {})
    return [o["value"] for o in v.get("selected_options") or []]


def _clean_url(s: str) -> str:
    """Slack이 감싼 '<https://x.org|x.org>' → 'https://x.org'"""
    s = (s or "").strip()
    m = re.fullmatch(r"<([^|>]+)(?:\|[^>]*)?>", s)
    return m.group(1) if m else s


def save_conf_modal(ctx: Ctx, uid: str, view: dict) -> tuple[dict | None, str]:
    """모달 저장. (입력 오류 {block_id: 메시지} 또는 None, 결과 메시지)"""
    meta = json.loads(view.get("private_metadata") or "{}")
    conf = get_conf(ctx, meta["id"]) if meta.get("id") else None
    if meta.get("id") and not conf:
        return {"name": "이미 삭제된 학회예요."}, ""
    if conf and not can_edit(ctx, uid, conf):
        return {"name": no_perm(conf)[:150]}, ""
    name = modal_value(view, "name")
    tz = modal_value(view, "tz") or "AoE"
    rt = modal_value(view, "review_type") or "direct"
    err = validate_conf(ctx, name, tz, rt, conf["id"] if conf else None)
    if err:
        return {"tz" if "시간대" in err else "name": err[:150]}, ""
    lines, errors = parse_deadline_lines(modal_value(view, "deadlines"), norm_tz(tz))
    if errors:
        return {"deadlines": " / ".join(errors)[:150]}, ""
    group, url = modal_value(view, "group"), _clean_url(modal_value(view, "url"))
    if conf:
        update_conf(ctx, conf["id"], name, group, url, rt, tz)
        cid = conf["id"]
    else:
        cid = add_conf(ctx, uid, name, group, url, rt, tz)
    if rt == "arr_cycle":
        set_links(ctx, cid, [int(x) for x in _multi_values(view, "commit_to")])
    else:
        ctx.db.run("DELETE FROM arr_links WHERE cycle_id=?", (cid,))
    conf = get_conf(ctx, cid)
    keep = set()
    for ln in lines:
        did, _old = set_deadline(ctx, conf, ln["year"], ln["kind"], ln["local"], ln["tz"], uid,
                                 url=ln["url"], note=ln["note"])
        keep.add(did)
    for d in conf_deadlines(ctx, cid):
        if d["id"] not in keep:
            unset_deadline(ctx, conf, d)
    verb = "수정했어요" if meta.get("id") else "추가했어요"
    return None, f"🏛 *{conf['name']}* 학회를 {verb}. (마감 {len(lines)}개)"


def quick_add(ctx: Ctx, uid: str, text: str) -> str:
    """'KDD group:ML url:https://kdd.org tz:AoE type:direct commit:ACL,EMNLP'"""
    opts, words = {}, []
    for tok in text.split():
        m = re.fullmatch(r"(group|grp|url|tz|type|commit|watch):(.*)", tok, re.I)
        if m:
            opts[m.group(1).lower()] = m.group(2)
        else:
            words.append(tok)
    name = " ".join(words).strip()
    rt = (opts.get("type") or "direct").lower()
    rt = {"arr-cycle": "arr_cycle", "cycle": "arr_cycle"}.get(rt, rt)
    tz = opts.get("tz") or "AoE"
    err = validate_conf(ctx, name, tz, rt)
    if err:
        return err
    commit, missing = [], []
    for n in filter(None, (opts.get("commit") or "").split(",")):
        c, _ = find_conf(ctx, n)
        if c:
            commit.append(c["id"])
        else:
            missing.append(n)
    if missing:
        return f"commit 학회를 찾을 수 없어요: {', '.join(missing)}"
    if commit and rt != "arr_cycle":
        return "`commit:`은 ARR 주기(`type:arr_cycle`)에만 쓸 수 있어요."
    watch = (opts.get("watch") or "on").lower() not in ("off", "0", "no", "false")
    cid = add_conf(ctx, uid, name, opts.get("group") or opts.get("grp") or "", _clean_url(opts.get("url", "")),
                   rt, tz, watch)
    if commit:
        set_links(ctx, cid, commit)
    c = get_conf(ctx, cid)
    return (f"🏛 *{c['name']}* 학회를 추가했어요 · {c['grp'] or '기타'} · {REVIEW_TYPES[rt]} · {c['default_tz']}\n"
            f"마감 추가: `/deadline set {c['name']} {ctx.today().year + 1} paper YYYY-MM-DD 23:59`")


# ── yaml 가져오기·내보내기 ────────────────────────────────
def import_data(ctx: Ctx, data: dict, by: str = "yaml") -> int:
    confs = (data or {}).get("conferences") or []
    for c in confs:
        if find_conf(ctx, c["name"])[0]:
            continue
        add_conf(ctx, str(c.get("created_by") or by), str(c["name"]), str(c.get("group") or ""), str(c.get("url") or ""),
                 c.get("review_type") or "direct", str(c.get("timezone") or "AoE"), bool(c.get("watch", True)))
    for c in confs:
        conf, _ = find_conf(ctx, c["name"])
        if c.get("commit_to"):
            ids = [x["id"] for x in (find_conf(ctx, n)[0] for n in c["commit_to"]) if x]
            set_links(ctx, conf["id"], ids)
        for d in c.get("deadlines") or []:
            at = d["at"]
            local = at.replace(tzinfo=None) if isinstance(at, datetime) else datetime.strptime(str(at), "%Y-%m-%d %H:%M")
            set_deadline(ctx, conf, int(d["year"]), norm_kind(d["kind"]), local, d.get("timezone"), by,
                         url=d.get("url") or "", note=d.get("note") or "", notify=False)
    return len(confs)


def export_data(ctx: Ctx) -> dict:
    out = []
    for c in all_confs(ctx):
        item = {"name": c["name"], "group": c["grp"], "url": c["official_url"], "review_type": c["review_type"],
                "timezone": c["default_tz"], "watch": bool(c["watch"]), "created_by": c["created_by"]}
        if c["review_type"] == "arr_cycle":
            item["commit_to"] = [x["name"] for x in linked(ctx, c)]
        dls = []
        for d in conf_deadlines(ctx, c["id"]):
            local = datetime.fromtimestamp(d["due_at"], parse_tz(d["tz"]))
            row = {"year": d["year"], "kind": d["kind"], "at": f"{local:%Y-%m-%d %H:%M}"}
            if d["tz"] != c["default_tz"]:
                row["timezone"] = d["tz"]
            if d["official_url"]:
                row["url"] = d["official_url"]
            if d["note"]:
                row["note"] = d["note"]
            dls.append(row)
        item["deadlines"] = dls
        out.append(item)
    return {"conferences": out}


def export_yaml(ctx: Ctx) -> str:
    import yaml
    head = (f"# LabBot 학회 목록 내보내기 ({ctx.now():%Y-%m-%d %H:%M} {_local_label(ctx)})\n"
            "# 새 봇의 app/conferences.yaml로 쓰면 첫 실행 때 이 내용으로 채워집니다.\n")
    return head + yaml.safe_dump(export_data(ctx), allow_unicode=True, sort_keys=False, width=120)


def seed(ctx: Ctx, path: str | None = None) -> bool:
    """처음 실행할 때만 yaml에서 학회 목록을 채웁니다. 그 뒤로는 DB가 기준."""
    if ctx.db.get_setting(SEED_KEY):
        return False
    if ctx.db.one("SELECT 1 FROM conferences LIMIT 1"):
        ctx.db.set_setting(SEED_KEY, "existing")
        return False
    path = path or ctx.cfg.deadline_seed_file or SEED_FILE
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            n = import_data(ctx, yaml.safe_load(f))
    except FileNotFoundError:
        log.warning("학회 초기 목록 파일이 없어요: %s", path)
        return False
    ctx.db.set_setting(SEED_KEY, ctx.now().isoformat(timespec="seconds"))
    log.info("학회 초기 목록 %d개를 %s에서 불러왔어요", n, path)
    return True


# ── 알림 ───────────────────────────────────────────────────
def reminder_job(ctx: Ctx) -> int:
    """목표 학회 마감 D-30·14·7·3·1·당일 DM. 같은 마감은 하루에 한 번, 단계별로 한 번만 보냅니다."""
    today = ctx.today().isoformat()
    now = int(ctx.now().timestamp())
    sent = 0
    for t in ctx.db.all("SELECT * FROM conf_targets ORDER BY id"):
        conf = get_conf(ctx, t["conf_id"])
        if not conf:
            continue
        for c, d, _k in alert_deadlines(ctx, conf):
            if d is None or d["due_at"] <= now:
                continue
            n = days_left(ctx, d["due_at"])
            if n not in STAGES:
                continue
            uid = t["user_id"]
            if ctx.db.one("SELECT 1 FROM deadline_alerts WHERE user_id=? AND deadline_id=? AND"
                          " (sent_on=? OR (stage=? AND due_at=?))", (uid, d["id"], today, n, d["due_at"])):
                continue
            ctx.db.run("INSERT INTO deadline_alerts(user_id, deadline_id, stage, due_at, sent_on) VALUES (?,?,?,?,?)",
                       (uid, d["id"], n, d["due_at"], today))
            head = "🚨 오늘 마감이에요!" if n == 0 else f"⏰ 마감 {dday(n)}"
            if d["kind"] == "abstract":
                head += " (초록 마감 — 본 마감보다 먼저예요)"
            title = f" · _{t['title']}_" if t["title"] and c["id"] == conf["id"] else ""
            text = f"{head}\n{deadline_line(ctx, c, d)}{title}" + (f"\n_{d['note']}_" if d["note"] else "")
            ctx.dm(uid, f"{head} {deadline_title(c, d)}", _notice(text, dl_url(c, d)))
            sent += 1
    return sent


def weekly_job(ctx: Ctx) -> bool:
    """매주 월요일: 앞으로 6주 안의 마감 요약을 채널에 (watch on인 학회만)"""
    ch = ctx.cfg.deadline_channel
    if not ch:
        return False
    items = upcoming(ctx, WEEKLY_DAYS, watched_only=True)
    if not items:
        return False
    lines = [f"• {deadline_line(ctx, c, d)}" + (f" · <{dl_url(c, d)}|사이트>" if dl_url(c, d) else "")
             for c, d in items]
    blocks = _chunks(f"*📅 앞으로 6주 안의 학회 마감* ({len(items)})", lines)
    blocks.append(context(CHECK_NOTE + " · 내 목표 학회 등록: `/deadline target ICLR`"))
    ctx.post(ch, "앞으로 6주 안의 학회 마감", blocks)
    return True


# ── Slack 핸들러 ───────────────────────────────────────────
def register(app, ctx: Ctx) -> None:
    seed(ctx)

    def conf_cmd(uid: str, text: str, respond, client, trigger_id: str):
        sub, _, rest = text.partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if sub in ("", "list", "ls"):
            return respond(text="학회 목록", blocks=conf_list_blocks(ctx))
        if sub == "add":
            if rest:
                return respond(quick_add(ctx, uid, rest))
            return client.views_open(trigger_id=trigger_id, view=conf_modal(ctx))
        if sub == "export":
            body = export_yaml(ctx)
            try:
                dm = client.conversations_open(users=uid)["channel"]["id"]
                client.files_upload_v2(channel=dm, initial_comment="📎 학회 목록과 마감 (yaml)",
                                       file_uploads=[{"content": body,
                                                      "filename": f"conferences_{ctx.today():%Y%m%d}.yaml"}])
                return respond("DM으로 yaml 파일을 보냈어요.")
            except Exception:  # noqa: BLE001
                log.exception("yaml 업로드 실패")
                ctx.dm(uid, "학회 목록 (yaml)", [section(f"```{body[:2900]}```")])
                return respond("파일 업로드에 실패해 DM에 내용을 붙여 보냈어요.")
        if sub in ("edit", "remove", "delete", "rm", "watch", "unwatch"):
            if not rest:
                return respond(f"형식: `/deadline conf {sub} 학회이름`")
            conf, sug = find_conf(ctx, rest)
            if not conf:
                return respond(not_found(rest, sug))
            if not can_edit(ctx, uid, conf):
                return respond(no_perm(conf))
            if sub == "edit":
                return client.views_open(trigger_id=trigger_id, view=conf_modal(ctx, conf))
            if sub in ("watch", "unwatch"):
                ctx.db.run("UPDATE conferences SET watch=? WHERE id=?", (1 if sub == "watch" else 0, conf["id"]))
                return respond(f"👁 {conf['name']}을(를) 월요일 채널 요약에 " +
                               ("포함해요." if sub == "watch" else "넣지 않아요."))
            return client.views_open(trigger_id=trigger_id, view=remove_modal(ctx, conf))
        return respond(HELP)

    @app.command("/deadline")
    def cmd_deadline(ack, command, respond, client):
        ack()
        uid, text = command["user_id"], (command.get("text") or "").strip()
        sub, _, rest = text.partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if sub in ("", "list", "upcoming"):
            return respond(text="다가오는 학회 마감", blocks=upcoming_blocks(ctx))
        if sub == "help":
            return respond(HELP)
        if sub == "conf":
            return conf_cmd(uid, rest, respond, client, command.get("trigger_id"))
        if sub == "target" and rest:
            text_, blocks = add_target(ctx, uid, rest)
            return respond(text=text_, blocks=blocks) if blocks else respond(text_)
        if sub == "untarget" and rest:
            return respond(remove_target(ctx, uid, rest))
        if sub == "mine":
            return respond(text="내 목표 학회", blocks=mine_blocks(ctx, uid))
        if sub == "targets":
            return respond(text="연구실 목표 학회", blocks=targets_blocks(ctx))
        if sub == "set" and rest:
            return respond(cmd_set(ctx, uid, rest))
        if sub == "unset" and rest:
            return respond(cmd_unset(ctx, uid, rest))
        return respond(HELP)

    @app.view("deadline_conf_modal")
    def view_conf(ack, body, view):
        uid = body["user"]["id"]
        errors, msg = save_conf_modal(ctx, uid, view)
        if errors:
            return ack(response_action="errors", errors=errors)
        ack()
        ctx.dm(uid, re.sub(r"[*_]", "", msg), [section(msg)])

    @app.view("deadline_conf_remove_modal")
    def view_remove(ack, body, view):
        uid = body["user"]["id"]
        conf = get_conf(ctx, json.loads(view["private_metadata"])["id"])
        ack()
        if not conf:
            return
        if not can_edit(ctx, uid, conf):
            return ctx.dm(uid, "삭제 권한이 없어요", [section(no_perm(conf))])
        users = delete_conf(ctx, conf["id"])
        for u in users:
            ctx.dm(u, f"{conf['name']} 학회가 목록에서 삭제됐어요",
                   [section(f"🗑 *{conf['name']}* 학회가 목록에서 삭제돼 내 목표 학회에서도 빠졌어요. "
                            f"(삭제: <@{uid}>)")])
            ctx.refresh_home(u)
        ctx.dm(uid, f"{conf['name']} 학회를 삭제했어요",
               [section(f"🗑 *{conf['name']}* 학회를 삭제했어요." + (f" 목표로 등록했던 {len(users)}명에게 알렸어요."
                                                                if users else ""))])

    @app.action("deadline_link")
    def act_link(ack):
        ack()

    @app.action("deadline_upcoming_open")
    def act_upcoming(ack, body, client):
        ack()
        client.views_open(trigger_id=body["trigger_id"], view={
            "type": "modal", "title": {"type": "plain_text", "text": "학회 마감"},
            "close": {"type": "plain_text", "text": "닫기"},
            "blocks": (mine_blocks(ctx, body["user"]["id"]) + [divider()] + upcoming_blocks(ctx))[:100]})
