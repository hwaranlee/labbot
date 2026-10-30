"""논문: arXiv + Hugging Face Daily Papers에서 주제별로 새 논문을 모아,
연구실 관심사 기준으로 고르고 한국어로 요약해 매일/매주 공유합니다."""
from __future__ import annotations

import json
import logging
import math
import re
import time as _time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from . import todo
from .ctx import Ctx, actions, button, confirm_dialog, context, section
from .llm import LLMError
from .timeparse import WEEKDAY_NAMES

log = logging.getLogger("labbot.papers")

ARXIV_API = "https://export.arxiv.org/api/query"
HF_API = "https://huggingface.co/api/daily_papers"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_DELAY = 3.0  # arXiv API 이용 규칙: 요청 사이 3초
UA = "LabBot/1.0 (research lab Slack bot)"

HELP = """*📚 /papers 사용법*
• `/papers add diffusion model, video generation cat:cs.CV` — 연구실 주제 추가 (쉼표 = 또는, `cat:` 은 arXiv 분야 제한)
• `/papers add-mine neural radiance field` — 나만 받는 개인 주제 (DM으로 와요)
• `/papers list` 주제 보기·삭제 · `/papers delete 3`
• `/papers profile &lt;연구실 소개 몇 문장&gt;` 관련도 판단 기준 설정 (관리자)
• `/papers search test-time scaling` — 최근 2주 논문 바로 찾아 요약
• `/papers summary 2609.12345` — LabBot이 소개한 논문 자세히 요약
• `/papers now` — 다이제스트 바로 실행 (관리자)
• 매일 아침 새 논문 Top N, 매주 금요일 이번 주 Top N을 보내 드려요. 📌 버튼으로 읽을 목록(Todo)에 담을 수 있어요."""

SELECT_SYSTEM = """너는 대학 연구실의 논문 큐레이터다. 연구실 소개와 관심 주제를 보고, 후보 논문 중
이 연구실 구성원이 꼭 읽어야 할 논문을 고른다.
- relevance: 0~10. 연구실 주제와 직접 관련되고 방법론·결과가 의미 있으면 높게, 키워드만 겹치면 낮게.
- 커뮤니티 반응(upvotes)이 높으면 중요도 판단에 참고만 한다.
- summary: 무엇을 했고 핵심 아이디어·결과가 무엇인지 한국어 2~3문장. 전문 용어는 원어 유지.
- why: 이 연구실에 왜 중요한지 한국어 한 문장. 과장하지 않는다.
- 초록에 없는 내용은 지어내지 않는다."""

DETAIL_SYSTEM = """너는 연구실 동료에게 논문을 소개하는 대학원생이다. 제공된 제목과 초록만 근거로 한국어로 정리한다.
형식(슬랙 mrkdwn, 각 항목 1~3문장):
*한 줄 요약*
*문제*
*방법*
*결과*
*한계·읽을 때 볼 점*
초록에 없는 수치나 사실은 지어내지 말고, 알 수 없으면 '초록에 언급 없음'이라고 쓴다. 전문 용어는 원어를 유지한다."""


# ── 수집 ───────────────────────────────────────────────────
def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def esc(s: str) -> str:
    """Slack mrkdwn 이스케이프 (링크 안의 | 포함)"""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "∣")


def phrases(query: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;]", query) if p.strip()]


def split_topic(text: str) -> tuple[str, str]:
    """'diffusion, video cat:cs.CV,cs.LG' → ('diffusion, video', 'cs.CV,cs.LG')"""
    m = re.search(r"\bcat:([\w.\-,]+)", text)
    cats = m.group(1) if m else ""
    q = (text[:m.start()] + text[m.end():]).strip(" ,") if m else text.strip()
    return q, cats


def build_query(query: str, categories: str = "") -> str:
    terms = [f'(ti:"{p}" OR abs:"{p}")' for p in (x.replace('"', "") for x in phrases(query))]
    q = " OR ".join(terms)
    if len(terms) > 1:
        q = f"({q})"
    cats = [c.strip() for c in (categories or "").split(",") if c.strip()]
    if cats:
        q += " AND (" + " OR ".join(f"cat:{c}" for c in cats) + ")"
    return q


def parse_arxiv(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for e in root.findall("a:entry", NS):
        raw_id = (e.findtext("a:id", "", NS) or "").rsplit("/abs/", 1)[-1]
        pid = re.sub(r"v\d+$", "", raw_id)
        if not pid:
            continue
        out.append({
            "id": pid,
            "title": _clean(e.findtext("a:title", "", NS)),
            "abstract": _clean(e.findtext("a:summary", "", NS)),
            "authors": [_clean(a.findtext("a:name", "", NS)) for a in e.findall("a:author", NS)],
            "published": (e.findtext("a:published", "", NS) or "")[:10],
            "url": f"https://arxiv.org/abs/{pid}",
            "upvotes": 0,
        })
    return out


def arxiv_search(query: str, categories: str = "", max_results: int = 50) -> list[dict]:
    params = {"search_query": build_query(query, categories), "sortBy": "submittedDate",
              "sortOrder": "descending", "start": 0, "max_results": max_results}
    return parse_arxiv(http_get(ARXIV_API + "?" + urllib.parse.urlencode(params)))


def hf_daily(day: date) -> list[dict]:
    try:
        data = json.loads(http_get(f"{HF_API}?date={day.isoformat()}"))
    except Exception as e:  # noqa: BLE001
        log.warning("HF Daily Papers 조회 실패: %s", e)
        return []
    out = []
    for item in data:
        p = item.get("paper", item)
        if not p.get("id"):
            continue
        out.append({"id": p["id"], "title": _clean(p.get("title", "")), "abstract": _clean(p.get("summary", "")),
                    "authors": [a.get("name", "") for a in p.get("authors", [])],
                    "published": (p.get("publishedAt") or "")[:10], "url": f"https://arxiv.org/abs/{p['id']}",
                    "upvotes": int(p.get("upvotes") or 0)})
    return out


def _match(p: dict, phrase: str) -> int:
    words = phrase.lower().split()
    title, abstract = p["title"].lower(), p["abstract"].lower()
    if phrase.lower() in title or all(w in title for w in words):
        return 3
    if phrase.lower() in abstract or all(w in abstract for w in words):
        return 1
    return 0


def heuristic(p: dict, topic_queries: list[str]) -> float:
    s = sum(_match(p, ph) for q in topic_queries for ph in phrases(q))
    return s + 2 * math.log1p(p.get("upvotes") or 0)


def collect(ctx: Ctx, topics: list[dict], days: int) -> list[dict]:
    """주제별 arXiv 검색 + 최근 HF Daily Papers를 합쳐 후보를 만듭니다."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    found: dict[str, dict] = {}
    for i, t in enumerate(topics):
        if i:
            _time.sleep(ARXIV_DELAY)
        try:
            for p in arxiv_search(t["query"], t.get("categories") or ""):
                if p["published"] >= cutoff:
                    found.setdefault(p["id"], p)
        except Exception as e:  # noqa: BLE001
            log.warning("arXiv 검색 실패 (%s): %s", t["query"], e)
    queries = [t["query"] for t in topics]
    today = ctx.today()
    for k in range(days + 1):
        for p in hf_daily(today - timedelta(days=k)):
            if p["id"] in found:
                found[p["id"]]["upvotes"] = max(found[p["id"]]["upvotes"], p["upvotes"])
            elif heuristic({**p, "upvotes": 0}, queries) > 0:
                found[p["id"]] = p
    now = int(_time.time())
    rows = []
    for p in found.values():
        p["h"] = heuristic(p, queries)
        rows.append((p["id"], p["title"], ", ".join(p["authors"]), p["abstract"], p["url"], p["published"],
                     p["upvotes"], now))
    if rows:
        ctx.db.many("INSERT INTO papers(id, title, authors, abstract, url, published, upvotes, first_seen)"
                    " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET upvotes=MAX(upvotes, excluded.upvotes)",
                    rows)
    return sorted(found.values(), key=lambda p: p["h"], reverse=True)


# ── 선별·요약 ──────────────────────────────────────────────
def lab_profile(ctx: Ctx) -> str:
    return ctx.db.get_setting("lab_profile", "")


def first_sentences(text: str, n: int = 2, limit: int = 350) -> str:
    s = " ".join(re.split(r"(?<=[.!?])\s+", text)[:n])
    return s[:limit] + ("…" if len(s) > limit else "")


def select(ctx: Ctx, candidates: list[dict], topic_queries: list[str], top_n: int) -> list[dict]:
    """후보 중 top_n개를 골라 summary/why/score를 붙입니다."""
    if not candidates:
        return []
    err = ""
    if ctx.llm and ctx.llm.available:
        try:
            return _llm_select(ctx, candidates[:40], topic_queries, top_n)
        except LLMError as e:
            log.warning("LLM 선별 실패, 키워드 점수로 대체: %s", e)
            err = str(e)
    picks = []
    for p in candidates[:top_n]:
        hit = [ph for q in topic_queries for ph in phrases(q) if _match(p, ph)]
        picks.append({**p, "score": round(p["h"], 1), "summary": first_sentences(p["abstract"]),
                      "why": ("주제: " + ", ".join(dict.fromkeys(hit))) if hit else "", "by": "rule",
                      "llm_error": err})
    return picks


def _llm_select(ctx: Ctx, cands: list[dict], topic_queries: list[str], top_n: int) -> list[dict]:
    by_id = {p["id"]: p for p in cands}
    listing = "\n\n".join(
        f"[{p['id']}] {p['title']}" + (f" (upvotes {p['upvotes']})" if p.get("upvotes") else "")
        + f"\n{p['abstract'][:1200]}" for p in cands)
    prompt = (f"연구실 소개: {lab_profile(ctx) or '(없음 — 관심 주제로 판단)'}\n"
              f"관심 주제: {'; '.join(topic_queries)}\n\n"
              f"후보 논문 {len(cands)}편 중 읽을 가치가 큰 순서로 최대 {min(len(cands), top_n * 2)}편을 평가해 주세요.\n\n{listing}")
    schema = {"type": "object", "properties": {"papers": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string", "enum": list(by_id)},
                       "relevance": {"type": "integer", "description": "0~10"},
                       "summary": {"type": "string"}, "why": {"type": "string"}},
        "required": ["id", "relevance", "summary", "why"]}}}, "required": ["papers"]}
    out = ctx.llm.structured(SELECT_SYSTEM, prompt, "pick_papers", "선별한 논문과 요약을 저장한다.", schema,
                             max_tokens=16000)
    picks, seen = [], set()
    for r in sorted(out.get("papers", []), key=lambda x: x.get("relevance", 0), reverse=True):
        p = by_id.get(r.get("id"))
        if not p or p["id"] in seen or r.get("relevance", 0) < ctx.cfg.paper_min_score:
            continue
        seen.add(p["id"])
        picks.append({**p, "score": r["relevance"], "summary": r.get("summary", ""), "why": r.get("why", ""), "by": "llm"})
    return picks[:top_n]


def detail(ctx: Ctx, pid: str) -> str:
    """봇이 소개한 논문(다이제스트·검색 결과)만 자세히 요약합니다. 아무 arXiv 논문이나 요약하지 않음."""
    row = ctx.db.one("SELECT p.* FROM papers p WHERE p.id=? AND EXISTS"
                     " (SELECT 1 FROM paper_posts pp WHERE pp.paper_id=p.id)", (pid,))
    if not row:
        return (f"`{pid}` 는 LabBot이 소개한 논문이 아니라서 요약할 수 없어요. "
                "다이제스트나 `/papers search` 결과에 나온 논문만 요약해요.")
    if row["detail"]:
        return row["detail"]
    head = f"*<{row['url']}|{esc(row['title'])}>*\n{esc(_authors(row['authors']))}\n\n"
    if ctx.llm and ctx.llm.available:
        try:
            body = ctx.llm.text(DETAIL_SYSTEM, f"제목: {row['title']}\n\n초록:\n{row['abstract']}", max_tokens=8000)
            text = head + body + "\n_초록 기반 요약이에요._"
            ctx.db.run("UPDATE papers SET detail=? WHERE id=?", (text, pid))
            return text
        except LLMError:
            pass
    return head + f"*Abstract*\n{row['abstract']}"


def _authors(a) -> str:
    names = [x.strip() for x in (a if isinstance(a, str) else ", ".join(a)).split(",") if x.strip()]
    return ", ".join(names[:3]) + (f" 외 {len(names) - 3}명" if len(names) > 3 else "")


def fallback_note(pick: dict) -> str:
    if pick.get("by") == "llm":
        return ""
    if pick.get("llm_error"):
        return f"⚠️ Claude API 호출에 실패해서 키워드 기준으로 골랐어요: `{pick['llm_error'][:300]}`"
    return "ANTHROPIC_API_KEY를 설정하면 Claude가 관련도를 판단하고 한국어로 요약해요."


def paper_blocks(picks: list[dict], heading: str, note: str = "") -> list:
    b = [section(f"*{heading}*")]
    for i, p in enumerate(picks, 1):
        meta = [esc(_authors(p.get("authors") or ""))]
        if p.get("published"):
            meta.append(p["published"])
        if p.get("upvotes"):
            meta.append(f"🔥 {p['upvotes']}")
        if p.get("by") == "llm":
            meta.append(f"관련도 {p['score']}/10")
        txt = f"*{i}. <{p['url']}|{esc(p['title'])}>*\n{' · '.join(m for m in meta if m)}\n{esc(p.get('summary', ''))}"
        if p.get("why"):
            txt += f"\n_💡 {esc(p['why'])}_"
        b.append(section(txt))
        b.append(actions(button("📌 읽을 목록", "paper_bookmark", p["id"]),
                         button("🔍 자세히", "paper_detail", p["id"])))
    if note:
        b.append(context(note))
    return b[:50]


# ── 다이제스트 ─────────────────────────────────────────────
def topics_for(ctx: Ctx, target: str) -> list[dict]:
    return [dict(r) for r in ctx.db.all("SELECT * FROM paper_topics WHERE owner=? ORDER BY id", (target,))]


def personal_targets(ctx: Ctx) -> list[str]:
    return [r["owner"] for r in ctx.db.all("SELECT DISTINCT owner FROM paper_topics WHERE owner<>'lab'")]


def run_digest(ctx: Ctx, target: str, days: int, heading: str) -> list[dict]:
    topics = topics_for(ctx, target)
    if not topics:
        return []
    cands = collect(ctx, topics, days)
    posted = {r["paper_id"] for r in ctx.db.all("SELECT paper_id FROM paper_posts WHERE target=?", (target,))}
    cands = [p for p in cands if p["id"] not in posted]
    picks = select(ctx, cands, [t["query"] for t in topics], ctx.cfg.paper_top_n)
    if not picks:
        return []
    now = int(_time.time())
    ctx.db.many("INSERT OR IGNORE INTO paper_posts(paper_id, target, posted_at, score, summary, why)"
                " VALUES (?,?,?,?,?,?)", [(p["id"], target, now, p["score"], p["summary"], p["why"]) for p in picks])
    note = fallback_note(picks[0])
    blocks = paper_blocks(picks, heading, note)
    if target == "lab":
        ctx.post(ctx.cfg.paper_channel, heading, blocks)
    else:
        ctx.dm(target, heading, blocks)
    return picks


def _day_label(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAY_NAMES[d.weekday()]})"


def daily_job(ctx: Ctx) -> None:
    if ctx.cfg.paper_schedule not in ("daily", "both") or not ctx.is_workday(ctx.today()):
        return
    heading = f"📚 오늘의 논문 · {_day_label(ctx.today())}"
    if ctx.cfg.paper_channel:
        run_digest(ctx, "lab", ctx.cfg.paper_window_days, heading)
    for uid in personal_targets(ctx):
        _time.sleep(ARXIV_DELAY)
        run_digest(ctx, uid, ctx.cfg.paper_window_days, heading + " (내 주제)")


def weekly_job(ctx: Ctx) -> None:
    if ctx.cfg.paper_schedule not in ("weekly", "both"):
        return
    heading = f"🏆 이번 주 중요 논문 Top {ctx.cfg.paper_top_n} · {_day_label(ctx.today())}"
    targets = (["lab"] if ctx.cfg.paper_channel else []) + personal_targets(ctx)
    for target in targets:
        if ctx.cfg.paper_schedule == "weekly":  # 매일 보내지 않았다면 이번 주 전체에서 새로 고름
            run_digest(ctx, target, 7, heading)
            continue
        since = int(_time.time()) - 7 * 86400
        rows = ctx.db.all("SELECT pp.*, p.title, p.url, p.authors, p.published, p.upvotes FROM paper_posts pp"
                          " JOIN papers p ON p.id=pp.paper_id WHERE pp.target=? AND pp.posted_at>=?"
                          " ORDER BY pp.score DESC, p.upvotes DESC LIMIT ?", (target, since, ctx.cfg.paper_top_n))
        if not rows:
            continue
        llm_used = bool(ctx.llm and ctx.llm.available)
        picks = [{"id": r["paper_id"], "title": r["title"], "url": r["url"], "authors": r["authors"],
                  "published": r["published"], "upvotes": r["upvotes"], "score": r["score"],
                  "summary": r["summary"], "why": r["why"], "by": "llm" if llm_used else "rule"} for r in rows]
        blocks = paper_blocks(picks, heading, "이번 주에 소개한 논문 중 관련도가 높은 순서예요.")
        if target == "lab":
            ctx.post(ctx.cfg.paper_channel, heading, blocks)
        else:
            ctx.dm(target, heading, blocks)


# ── Slack 핸들러 ───────────────────────────────────────────
def topics_blocks(ctx: Ctx, uid: str) -> list:
    b = [section("*📚 논문 구독 주제*")]
    for owner, label in (("lab", "연구실"), (uid, "내 개인")):
        rows = topics_for(ctx, owner)
        b.append(section(f"*{label} 주제* ({len(rows)})"))
        for t in rows:
            q = t["query"] if len(t["query"]) <= 200 else t["query"][:200] + "…"
            txt = f"`#{t['id']}` {q}" + (f" · `cat:{t['categories']}`" if t["categories"] else "")
            b.append(section(txt, button("삭제", "paper_topic_delete", str(t["id"]),
                                         confirm=confirm_dialog("주제 삭제", f"'{q[:200]}' 주제를 삭제할까요?"))))
        if not rows:
            b.append(context("없음 · `/papers " + ("add" if owner == "lab" else "add-mine") + " 키워드` 로 추가"))
    prof = lab_profile(ctx)
    b.append(context(f"연구실 소개: {prof[:300] if prof else '미설정 (`/papers profile ...`)'} · "
                     f"일정: {ctx.cfg.paper_schedule} · Claude 요약: {'켜짐' if ctx.llm and ctx.llm.available else '꺼짐'}"))
    return b[:50]


def register(app, ctx: Ctx) -> None:
    @app.command("/papers")
    def cmd_papers(ack, command, respond):
        ack()
        uid, text = command["user_id"], (command.get("text") or "").strip()
        sub, _, rest = text.partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if sub in ("", "help", "도움말"):
            return respond(text="논문", blocks=[section(HELP)] + topics_blocks(ctx, uid)[:40])
        if sub in ("add", "add-mine", "myadd", "추가", "내추가"):
            owner = uid if sub in ("add-mine", "myadd", "내추가") else "lab"
            q, cats = split_topic(rest)
            if not q:
                return respond("키워드를 적어 주세요. 예: `/papers add diffusion model cat:cs.CV`")
            if len(q) > 300 or len(phrases(q)) > 10:
                return respond("주제가 너무 길어요. 키워드는 쉼표로 10개, 300자 이내로 나눠서 여러 번 추가해 주세요.")
            tid = ctx.db.run("INSERT INTO paper_topics(owner, query, categories, created_by, created_at) VALUES (?,?,?,?,?)",
                             (owner, q, cats or None, uid, int(_time.time())))
            where = "연구실" if owner == "lab" else "내 개인"
            return respond(f"➕ {where} 주제 `#{tid}` 추가: *{q}*" + (f" (`cat:{cats}`)" if cats else ""))
        if sub in ("list", "topics", "목록"):
            return respond(text="주제", blocks=topics_blocks(ctx, uid))
        if sub in ("delete", "del", "remove", "삭제"):
            m = re.search(r"\d+", rest)
            return respond(_delete_topic(ctx, uid, int(m.group())) if m else "번호를 알려 주세요. 예: `/papers delete 3`")
        if sub in ("profile", "관심"):
            if not rest:
                return respond(f"연구실 소개: {lab_profile(ctx) or '미설정'}")
            if ctx.cfg.admin_ids and not ctx.is_admin(uid):
                return respond("연구실 소개는 관리자만 바꿀 수 있어요.")
            ctx.db.set_setting("lab_profile", rest)
            return respond("✅ 연구실 소개를 저장했어요. 논문 관련도 판단에 사용해요.")
        if sub in ("search", "검색"):
            if not rest:
                return respond("예: `/papers search test-time scaling`")
            respond(f"🔎 '{rest}' 최근 2주 논문을 찾는 중이에요…")
            q, cats = split_topic(rest)
            cands = collect(ctx, [{"query": q, "categories": cats}], 14)
            picks = select(ctx, cands, [q], ctx.cfg.paper_top_n)
            if not picks:
                return respond(f"최근 2주 동안 '{q}' 관련 논문을 찾지 못했어요.")
            # 검색 결과도 '소개한 논문'으로 남겨 🔍 자세히·/papers summary가 되도록 (target='search'는 다이제스트와 무관)
            ctx.db.many("INSERT OR IGNORE INTO paper_posts(paper_id, target, posted_at, score, summary, why)"
                        " VALUES (?,?,?,?,?,?)",
                        [(p["id"], "search", int(_time.time()), p["score"], p["summary"], p["why"]) for p in picks])
            return respond(text=f"'{q}' 검색 결과", blocks=paper_blocks(picks, f"🔎 '{q}' 최근 2주 Top {len(picks)}"))
        if sub in ("summary", "detail", "요약"):
            m = re.search(r"(\d{4}\.\d{4,5})", rest)
            if not m:
                return respond("arXiv 번호나 링크를 주세요. 예: `/papers summary 2609.12345`")
            respond(f"📖 {m.group(1)} 요약 중이에요…")
            return respond(detail(ctx, m.group(1)))
        if sub in ("now", "지금"):
            if not ctx.is_admin(uid):
                return respond("관리자만 실행할 수 있어요.")
            respond("📚 지금 다이제스트를 만드는 중이에요…")
            picks = run_digest(ctx, "lab", ctx.cfg.paper_window_days, f"📚 오늘의 논문 · {_day_label(ctx.today())}")
            return respond(f"{len(picks)}편을 <#{ctx.cfg.paper_channel}>에 올렸어요." if picks
                           else "새로 소개할 논문이 없거나 PAPER_CHANNEL/주제가 설정되지 않았어요.")
        respond(HELP)

    @app.action("paper_topic_delete")
    def act_topic_delete(ack, body, respond):
        ack()
        uid = body["user"]["id"]
        msg = _delete_topic(ctx, uid, int(body["actions"][0]["value"]))
        view = body.get("view") or {}
        if view.get("type") == "modal":
            ctx.client.views_update(view_id=view["id"], view={
                "type": "modal", "title": view["title"], "close": {"type": "plain_text", "text": "닫기"},
                "blocks": [context(msg)] + topics_blocks(ctx, uid)})
        else:
            respond(replace_original=True, text=msg, blocks=[context(msg)] + topics_blocks(ctx, uid))

    @app.action("paper_bookmark")
    def act_bookmark(ack, body, respond):
        ack()
        uid, pid = body["user"]["id"], body["actions"][0]["value"]
        p = ctx.db.one("SELECT * FROM papers WHERE id=?", (pid,))
        if not p:
            return respond(replace_original=False, response_type="ephemeral", text="논문 정보를 찾을 수 없어요.")
        dup = ctx.db.one("SELECT id FROM tasks WHERE assignee=? AND status='open' AND permalink=?", (uid, p["url"]))
        if dup:
            return respond(replace_original=False, response_type="ephemeral", text=f"이미 읽을 목록에 있어요 (`#{dup['id']}`).")
        tid = todo.create_task(ctx, f"📄 논문 읽기: {p['title'][:150]}", uid, uid, None, False, permalink=p["url"])
        ctx.refresh_home(uid)
        respond(replace_original=False, response_type="ephemeral",
                text=f"📌 읽을 목록(Todo `#{tid}`)에 추가했어요. 마감을 정하려면 `/todo` 에서 다시 추가해도 돼요.")

    @app.action("paper_detail")
    def act_detail(ack, body, respond, client):
        ack()
        pid = body["actions"][0]["value"]
        container = body.get("container", {})
        text = detail(ctx, pid)
        ch, ts = body.get("channel", {}).get("id"), (body.get("message") or {}).get("ts")
        if container.get("is_ephemeral") or not ch or not ts:
            return respond(replace_original=False, response_type="ephemeral", text=text)
        if ctx.db.one("SELECT 1 FROM paper_threads WHERE channel=? AND ts=? AND paper_id=?", (ch, ts, pid)):
            return respond(replace_original=False, response_type="ephemeral",
                           text="스레드에 이미 자세한 요약이 있어요. 👀\n\n" + text)
        try:
            client.chat_postMessage(channel=ch, thread_ts=ts, text=text)
            ctx.db.run("INSERT OR IGNORE INTO paper_threads(channel, ts, paper_id) VALUES (?,?,?)", (ch, ts, pid))
        except Exception:  # noqa: BLE001
            respond(replace_original=False, response_type="ephemeral", text=text)


def _delete_topic(ctx: Ctx, uid: str, tid: int) -> str:
    t = ctx.db.one("SELECT * FROM paper_topics WHERE id=?", (tid,))
    if not t:
        return f"`#{tid}` 주제가 없어요."
    if t["owner"] != uid and not (t["owner"] == "lab" and (t["created_by"] == uid or ctx.is_admin(uid))):
        return "본인이 추가한 주제나 개인 주제만 삭제할 수 있어요."
    ctx.db.run("DELETE FROM paper_topics WHERE id=?", (tid,))
    return f"🗑 주제 `#{tid}` {t['query']} 를 삭제했어요."
