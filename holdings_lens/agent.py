"""A credit-analyst agent that answers questions from the holdings database.

Design choices that matter:
  * The model never sees raw filings. It must query the database through a
    read-only SQL tool, so every answer is grounded in extracted rows.
  * Every claim must cite holding ids like [H12]. After the run we check that
    each cited id exists AND was actually returned by a tool call, so made-up
    citations are caught, not just missing ones.
  * Token usage is recorded per question for the scorecard.

Uses the Anthropic Messages API over plain HTTP (no SDK needed). Set
ANTHROPIC_API_KEY; optionally HOLDINGS_MODEL.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Callable, Optional

import requests

from .db import DEFAULT_DB, connect_readonly

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("HOLDINGS_MODEL", "claude-sonnet-5-5")
MAX_TURNS = 8
MAX_ROWS = 200
SHARED_LIMIT = 20  # the full overlap list can run to dozens of borrowers; the widest gaps are what matter

SYSTEM = """You are a careful private-credit analyst assistant. You answer questions about
BDC (business development company) portfolios using ONLY the holdings database and tools.

Database (SQLite):
  filings(id, fund, cik, form, period, accession, url, reconciliation)
  holdings(id, filing_id, company, company_key, industry, investment_type, lien, instrument,
           interest_rate, rate_pct, spread, base_rate, spread_bps, maturity, principal, cost,
           fair_value, mark, pct_net_assets, non_accrual, pik, non_income, units, footnotes, notes,
           xbrl_fair_value)
  v_holdings = holdings + fund, period, fair_value_usd, cost_usd, principal_usd (whole dollars)
  v_latest   = v_holdings limited to each fund's most recent filing
  - Prefer v_holdings / v_latest and the *_usd columns; say amounts in dollars.
  - lien is one of first_lien, second_lien, unitranche, subordinated, equity, other, cash.
    lien = 'cash' marks money-market funds the filer lists with the schedule but excludes from
    total investments: ALWAYS add lien != 'cash' when computing portfolio or debt totals and shares.
  - mark = fair_value / cost (0.85 means valued at 85% of cost). spread_bps is the margin over base_rate.
  - company_key is a normalized borrower name: use it to match the same company across funds/quarters.
  - maturity is ISO (YYYY-MM-DD) or YYYY-MM when the filing shows only month/year.
  - Flags (non_accrual, pik, non_income) come from the filing's own footnotes, whose full text is in
    the footnotes column. Before interpreting a flag, read that text instead of assuming: e.g. some
    filers put preferred equity on non-accrual for unpaid non-cash dividends.
  - A small negative fair_value on a zero-cost row is usually an unfunded commitment valued below par.

Tools: run_sql for anything; portfolio_summary, compare_filings and shared_borrowers return
precomputed analytics (with holding ids to cite); get_sources returns original filing rows.

Rules:
  1. Never guess numbers. Look them up.
  2. Cite every holding you rely on as [H<id>] and filings as [F<id>]. For aggregates over more than
     10 holdings, say "computed from N holdings in [F<id>]".
  3. If the data cannot answer the question, say so plainly.
  4. Be concise: lead with the answer, then 2-5 supporting bullets."""

TOOLS = [
    {
        "name": "run_sql",
        "description": "Run one read-only SQL SELECT against the holdings database. Returns up to 200 rows as JSON. Include holdings.id in results so you can cite rows.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "portfolio_summary",
        "description": "Precomputed analytics for one filing: size, lien mix, concentration, industry mix, weighted average spread and rate, non-accruals, PIK, mark distribution, watchlist, maturity wall.",
        "input_schema": {"type": "object", "properties": {"filing_id": {"type": "integer"}}, "required": ["filing_id"]},
    },
    {
        "name": "compare_filings",
        "description": "Quarter-over-quarter changes between two filings of the same fund: new, exited, re-marked and resized positions.",
        "input_schema": {"type": "object", "properties": {"old_filing_id": {"type": "integer"}, "new_filing_id": {"type": "integer"}},
                         "required": ["old_filing_id", "new_filing_id"]},
    },
    {
        "name": "shared_borrowers",
        "description": "Borrowers held by more than one fund (each fund's latest filing), with each fund's debt mark (fair value / cost over its loans to that borrower, equity excluded) and the gap between them, widest first.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sources",
        "description": "Return the original filing row text for holding ids, to double-check extracted values.",
        "input_schema": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]},
    },
]

CITE_RE = re.compile(r"\[H(\d+)\]")  # single citation, kept for callers that want the simple form
# A bracket of holding citations in any form the model writes: [H12], [H12, H13], [H12–H15], [H12-15], [H12 and H13]
CITE_GROUP_RE = re.compile(r"\[(H\d+(?:\s*(?:,|;|–|—|-|and|&)\s*H?\d+)*)\]")
MAX_RANGE = 50  # a range wider than this is treated as its two endpoints, so a typo can't "cite" thousands of rows


def cited_ids(text: str) -> set[int]:
    """Every holding id an answer cites, expanding grouped citations and ranges."""
    ids: set[int] = set()
    for group in CITE_GROUP_RE.findall(text or ""):
        tokens = re.findall(r"\d+|–|—|-", group)
        i = 0
        while i < len(tokens):
            a = int(tokens[i])
            if i + 2 < len(tokens) and tokens[i + 1] in "–—-" and tokens[i + 2].isdigit():
                b = int(tokens[i + 2])
                if 0 <= b - a <= MAX_RANGE:
                    ids.update(range(a, b + 1))
                else:
                    ids.update({a, b})
                i += 3
            else:
                if tokens[i].isdigit():
                    ids.add(a)
                i += 1
    return ids
ID_COL = re.compile(r"(^|_)ids?($|_)", re.I)  # id, ids, holding_id, holding_ids, old_id, prior_id ...
ID_LIST = re.compile(r"^\s*\d+(\s*[,;|\s]\s*\d+)*\s*$")  # "3006,3007,3008" from GROUP_CONCAT(id)


def _ids_in(value) -> list[int]:
    """Holding ids in one result cell: an integer, or a GROUP_CONCAT-style list of integers."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str) and ID_LIST.match(value):
        return [int(x) for x in re.findall(r"\d+", value)]
    return []


def _collect_ids(obj, seen: set):
    """Every holding id that appears in an analytics result counts as retrieved."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("filing", "from", "to", "filings"):  # these hold filing ids, not holding ids
                continue
            if k in ("id", "prior_id") and isinstance(v, int):
                seen.add(v)
            elif k == "ids" and isinstance(v, list):
                seen.update(i for i in v if isinstance(i, int))
            else:
                _collect_ids(v, seen)
    elif isinstance(obj, list):
        for v in obj:
            _collect_ids(v, seen)


class Tools:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.con = connect_readonly(db_path)
        self.seen_ids: set[int] = set()

    def run_sql(self, query: str) -> str:
        q = query.strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", q) or ";" in q:
            return json.dumps({"error": "Only a single SELECT/WITH statement is allowed."})
        try:
            cur = self.con.execute(q)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchmany(MAX_ROWS + 1)]
        except sqlite3.Error as e:
            return json.dumps({"error": str(e)})
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        reads_holdings = re.search(r"\b(holdings|v_holdings|v_latest)\b", q, re.I)
        for r in rows if reads_holdings else []:  # only holding ids count as retrieved rows
            for k, v in r.items():
                # id, holding_id, old_id, new_id, prior_id ... but never filing ids
                if ID_COL.search(k) and "filing" not in k.lower():
                    self.seen_ids.update(_ids_in(v))
        return json.dumps({"columns": cols, "rows": rows, "truncated": truncated}, default=str)

    def get_sources(self, ids: list[int]) -> str:
        ids = [int(i) for i in ids][:50]
        if not ids:
            return json.dumps({"rows": []})
        q = (f"SELECT h.id, h.company, h.raw, h.table_idx, h.row_idx, f.fund, f.period, f.url "
             f"FROM holdings h JOIN filings f ON f.id = h.filing_id WHERE h.id IN ({','.join('?' * len(ids))})")
        rows = [dict(r) for r in self.con.execute(q, ids)]
        self.seen_ids.update(r["id"] for r in rows)
        return json.dumps({"rows": rows}, default=str)

    def _analytics(self, payload) -> str:
        _collect_ids(payload, self.seen_ids)
        return json.dumps(payload, default=str)

    def call(self, name: str, args: dict) -> str:
        from . import analytics
        try:
            if name == "portfolio_summary":
                return self._analytics(analytics.summary(self.con, int(args["filing_id"])))
            if name == "compare_filings":
                return self._analytics(analytics.changes(self.con, int(args["old_filing_id"]), int(args["new_filing_id"])))
            if name == "shared_borrowers":
                o = analytics.overlap(self.con)
                shared = o["shared_borrowers"]  # already sorted by how far apart the lenders' marks are
                o = dict(o, total_shared=len(shared), shared_borrowers=shared[:SHARED_LIMIT],
                         basis="Debt positions only (loans and notes); equity stakes are excluded from these marks.",
                         note=f"Showing the {min(SHARED_LIMIT, len(shared))} borrowers with the widest mark gaps of "
                              f"{len(shared)} shared. Use run_sql for others.")
                return self._analytics(o)
        except (KeyError, ValueError, TypeError) as e:
            return json.dumps({"error": f"bad arguments: {e}"})
        if name == "run_sql":
            return self.run_sql(args.get("query", ""))
        if name == "get_sources":
            return self.get_sources(args.get("ids", []))
        return json.dumps({"error": f"unknown tool {name}"})


def _post(payload: dict) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("Set ANTHROPIC_API_KEY to run the agent.")
    r = requests.post(API_URL, json=payload, timeout=120, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"API error {r.status_code}: {r.text[:500]}")
    return r.json()


def check_citations(answer: str, tools: Tools) -> dict:
    cited = cited_ids(answer)
    existing = set()
    if cited:
        q = f"SELECT id FROM holdings WHERE id IN ({','.join('?' * len(cited))})"
        existing = {r[0] for r in tools.con.execute(q, list(cited))}
    return {
        "cited": sorted(cited),
        "nonexistent": sorted(cited - existing),
        "not_retrieved": sorted((cited & existing) - tools.seen_ids),  # cited but never seen by the agent
        "grounded": bool(cited) and cited <= tools.seen_ids,
    }


FINALIZE = ("You ran out of room before giving an answer. Using only what you have already retrieved, "
            "give the final answer now: under 150 words, key number first, with citations. No more tool calls.")
EMPTY_NOTE = "The answer ran past the length limit before it could be written. Please try a narrower question."


def _add_user_text(messages: list, text: str) -> None:
    """Append a user instruction while keeping user/assistant turns alternating."""
    last = messages[-1] if messages else None
    if last and last["role"] == "user":
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"].append({"type": "text", "text": text})
    else:
        messages.append({"role": "user", "content": [{"type": "text", "text": text}]})


TRIMMED_NOTE = "\n\n_(Answer trimmed at the length limit. Ask a narrower question for more detail.)_"


def trim_to_complete(text: str) -> str:
    """Cut a length-limited answer back to its last complete line or sentence."""
    text = text.rstrip()
    if not text:
        return ""
    cut = text.rfind("\n")
    if cut < len(text) * 0.5:  # no late line break: fall back to the last sentence end
        cut = max(text.rfind(". "), text.rfind(".\n"), text.rfind(".)"))
        cut = cut + 1 if cut > 0 else len(text)
    return text[:cut].rstrip() + TRIMMED_NOTE


def ask(question: str, db_path: str = DEFAULT_DB, post: Callable[[dict], dict] = _post,
        model: Optional[str] = None, style: str = "") -> dict:
    tools = Tools(db_path)
    messages = [{"role": "user", "content": question}]
    trace, usage = [], {"input_tokens": 0, "output_tokens": 0}
    answer = ""
    for _ in range(MAX_TURNS):
        resp = post({"model": model or MODEL, "max_tokens": 1500, "system": SYSTEM + style,
                     "tools": TOOLS, "messages": messages})
        for k in usage:
            usage[k] += resp.get("usage", {}).get(k, 0)
        content = resp.get("content", [])
        messages.append({"role": "assistant", "content": content})
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if resp.get("stop_reason") == "max_tokens":  # out of room: never show a sentence cut mid-word
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            if tool_uses or len(text.strip()) < 200:
                # It ran out while still working (often mid tool call), so there's no real answer yet.
                # Ask once more, tools off, to answer from what it has already retrieved.
                messages.pop()
                if text.strip():
                    messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
                _add_user_text(messages, FINALIZE)
                resp = post({"model": model or MODEL, "max_tokens": 1500, "system": SYSTEM + style,
                             "tools": TOOLS, "tool_choice": {"type": "none"}, "messages": messages})
                for k in usage:
                    usage[k] += resp.get("usage", {}).get(k, 0)
                text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
                answer = trim_to_complete(text) if resp.get("stop_reason") == "max_tokens" else text.strip()
            else:
                answer = trim_to_complete(text)
            if not answer.strip():
                answer = EMPTY_NOTE
            break
        if not tool_uses:
            answer = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            break
        results = []
        for tu in tool_uses:
            out = tools.call(tu["name"], tu.get("input", {}))
            trace.append({"tool": tu["name"], "input": tu.get("input", {}), "output_preview": out[:300]})
            results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out})
        messages.append({"role": "user", "content": results})
    else:
        answer = "(stopped: too many tool calls)"
    citations = check_citations(answer, tools)  # must run before get_sources marks ids as seen
    sources = json.loads(tools.get_sources(citations["cited"]))["rows"] if citations["cited"] else []
    return {"question": question, "answer": answer, "citations": citations,
            "sources": sources, "trace": trace, "usage": usage, "model": model or MODEL}
