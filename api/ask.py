"""Live "Ask the agent" endpoint, deployed as a Vercel serverless function.

POST /api/ask  {"question": "..."}  ->  {"answer", "citations", "sources", "grounded", "usage"}

The Anthropic API key lives only in the server environment (ANTHROPIC_API_KEY on Vercel),
never in the web page. The database is a read-only copy bundled with the function
(api/holdings.db, written by `python -m holdings_lens export-live-db`).

Guardrails, because the page is public:
  * question length limit and a per-visitor hourly limit (best effort: kept in memory per
    server instance, so set a monthly spend limit in the Anthropic console as the hard cap)
  * a per-instance daily cap on questions
  * the agent may take at most LIVE_MAX_TURNS tool steps and LIVE_MAX_TOKENS output tokens
  * tools are read-only; the database connection itself is opened read-only
"""
from __future__ import annotations

import json
import re
import os
import sys
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from holdings_lens import agent  # noqa: E402

DB_PATH = os.environ.get("LIVE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "holdings.db"))
MAX_QUESTION_CHARS = int(os.environ.get("LIVE_MAX_QUESTION_CHARS", "300"))
PER_VISITOR_PER_HOUR = int(os.environ.get("LIVE_PER_VISITOR_PER_HOUR", "5"))
DAILY_CAP = int(os.environ.get("LIVE_DAILY_CAP", "150"))
MAX_TURNS = int(os.environ.get("LIVE_MAX_TURNS", "6"))
MAX_TOKENS = int(os.environ.get("LIVE_MAX_TOKENS", "1600"))

# Visitors skim: short answers read better and cost less. Appended to the agent's system prompt.
LIVE_STYLE = """

This answer is shown on a public web page to someone skimming. Format:
  - Under 180 words. No headings.
  - First line: the answer in one bold sentence, with the key number.
  - Then at most 5 short bullets, with [H<id>] citations.
  - At most one short caveat line, only if it changes how to read the answer.
  - In prose, name filings by fund and date (e.g. "the June 30, 2026 10-Q"), not by F-number;
    keep [F<id>] only inside the brackets of a citation.
  - If the question needs more than this, answer the core of it and suggest one narrower follow-up."""
ALLOWED_ORIGIN = os.environ.get("LIVE_ALLOWED_ORIGIN", "")  # e.g. https://holdings-lens.vercel.app; empty = same-origin only

_visits: dict[str, deque] = defaultdict(deque)
_day = {"date": "", "count": 0}


def check_limits(visitor: str, now: float | None = None) -> str | None:
    """Return an error message if this request should be refused, else None (and count it)."""
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if _day["date"] != today:
        _day.update(date=today, count=0)
    if _day["count"] >= DAILY_CAP:
        return "The live demo has reached today's question limit. Please try again tomorrow."
    q = _visits[visitor]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= PER_VISITOR_PER_HOUR:
        return f"You've reached the limit of {PER_VISITOR_PER_HOUR} questions per hour. Please try again later."
    q.append(now)
    _day["count"] += 1
    return None


def validate(body: dict) -> tuple[str | None, str | None]:
    q = (body.get("question") or "").strip()
    if not q:
        return None, "Please type a question."
    if len(q) > MAX_QUESTION_CHARS:
        return None, f"Please keep questions under {MAX_QUESTION_CHARS} characters."
    return q, None


_scope_cache: dict = {}


def data_scope() -> str:
    """Tell the agent exactly which funds and quarters the live database holds, read from the database
    itself, so it never suggests a question the data can't answer (e.g. cross-fund comparisons with one fund)."""
    if DB_PATH in _scope_cache:
        return _scope_cache[DB_PATH]
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute("SELECT fund, GROUP_CONCAT(period, ', ') FROM "
                           "(SELECT fund, period FROM filings ORDER BY period) GROUP BY fund").fetchall()
        con.close()
    except Exception:
        return ""
    funds = "; ".join(f"{f} (filings dated {p})" for f, p in rows)
    note = (" There is only one fund, so do not offer or attempt cross-fund comparisons; quarter-over-quarter"
            " comparisons within the fund are fine." if len(rows) == 1 else "")
    _scope_cache[DB_PATH] = f"\n\nThe database holds exactly: {funds}.{note} Only suggest follow-ups this data can answer."
    return _scope_cache[DB_PATH]


def _base_post(payload: dict) -> dict:
    return agent._post(payload)


def answer(question: str, post=None) -> dict:
    base = post or _base_post

    def capped(payload: dict) -> dict:  # every model call is capped, whatever transport is used
        return base(dict(payload, max_tokens=min(payload.get("max_tokens", MAX_TOKENS), MAX_TOKENS)))

    old_turns = agent.MAX_TURNS
    agent.MAX_TURNS = MAX_TURNS
    try:
        r = agent.ask(question, db_path=DB_PATH, post=capped, style=LIVE_STYLE + data_scope())
    finally:
        agent.MAX_TURNS = old_turns
    c = r["citations"]
    # No holding cited (e.g. "the data can't answer that", or a filing-level total) is not a grounding
    # failure: report None so the page shows a neutral note instead of a warning.
    grounded = c["grounded"] if c["cited"] else None
    filing_level = bool(re.search(r"\[F\d+\]", r["answer"] or ""))
    return {"answer": r["answer"], "citations": c, "grounded": grounded, "filing_level": filing_level,
            "unsupported": r["citations"]["nonexistent"] + r["citations"]["not_retrieved"],
            "sources": r["sources"][:8], "usage": r["usage"], "model": r["model"]}


class handler(BaseHTTPRequestHandler):  # Vercel's Python runtime looks for a class named `handler`
    def _send(self, code: int, obj: dict):
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        self._send(200, {"ok": True, "database": os.path.exists(DB_PATH), "key_configured": bool(os.environ.get("ANTHROPIC_API_KEY"))})

    def do_POST(self):
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 10_000)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "Bad request."})
        question, err = validate(body)
        if err:
            return self._send(400, {"error": err})
        visitor = (self.headers.get("x-forwarded-for") or self.client_address[0] or "?").split(",")[0].strip()
        err = check_limits(visitor)
        if err:
            return self._send(429, {"error": err})
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return self._send(503, {"error": "The live demo isn't configured yet."})
        try:
            return self._send(200, answer(question))
        except Exception as e:  # never leak stack traces or keys to the page
            print("ask failed:", repr(e)[:500])
            return self._send(502, {"error": "The agent couldn't answer right now. Please try again in a minute."})
