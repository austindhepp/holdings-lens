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
MAX_TOKENS = int(os.environ.get("LIVE_MAX_TOKENS", "1200"))
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


def _base_post(payload: dict) -> dict:
    return agent._post(payload)


def answer(question: str, post=None) -> dict:
    base = post or _base_post

    def capped(payload: dict) -> dict:  # every model call is capped, whatever transport is used
        return base(dict(payload, max_tokens=min(payload.get("max_tokens", MAX_TOKENS), MAX_TOKENS)))

    old_turns = agent.MAX_TURNS
    agent.MAX_TURNS = MAX_TURNS
    try:
        r = agent.ask(question, db_path=DB_PATH, post=capped)
    finally:
        agent.MAX_TURNS = old_turns
    return {"answer": r["answer"], "citations": r["citations"], "grounded": r["citations"]["grounded"],
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
