"""One consistent sector for every borrower, so lenders' different industry labels can be compared.

Lenders label the same borrower differently (Golub files Huskies Parent under "Insurance", Ares under
"Software and Services" with the description "Insurance software provider"), and software loans are
often filed under other industries. This module asks the model to place each borrower in one shared
list, from the evidence in the filings: names as reported, each lender's label, and any business
description. Every call records its basis and confidence, results are cached in evals/sectors.json
(so only new borrowers cost anything), and they are loaded into a `sectors` table the agent can join.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from typing import Callable, Optional

SECTORS = [
    "Software & SaaS",
    "IT & tech-enabled services",
    "Healthcare providers & services",
    "Healthcare technology",
    "Pharma, biotech & life sciences",
    "Insurance & insurance services",
    "Financial services & fintech",
    "Business & professional services",
    "Consumer products & retail",
    "Consumer services",
    "Food & beverage",
    "Industrials & manufacturing",
    "Construction, building & real estate",
    "Transportation & logistics",
    "Energy, utilities & environmental",
    "Media, telecom & entertainment",
    "Education",
    "Aerospace, defense & government services",
    "Investment vehicles & asset managers",
    "Other / unclear",
]
BASES = ("description", "label", "name", "knowledge")
CONFIDENCE = ("high", "medium", "low")

SYSTEM = f"""You classify private-credit borrowers into ONE consistent sector list so that different lenders'
industry labels can be compared. For each borrower you get its name(s) as each lender reported them, each
lender's own industry label, and a business description when the filing has one.

Sectors (use exactly one of these strings):
{chr(10).join('- ' + s for s in SECTORS)}

Rules:
- "software" is true when the business mainly sells software or SaaS, including vertical software for one
  industry (e.g. restaurant, insurance or medical-records software). It is false for IT services and
  consulting, hardware, and businesses that merely use software. A software company can sit in another
  sector only if that is clearly its main business; otherwise use "Software & SaaS".
- Prefer the business description, then the lenders' labels, then what the name itself says. Use general
  knowledge only for well-known companies, and say so with basis "knowledge".
- Never guess beyond the evidence: if it is thin, use "Other / unclear" or confidence "low".
- "reason": at most 15 words, quoting or paraphrasing the evidence you relied on.

Reply with ONLY a JSON array, one object per borrower, in the order given:
[{{"key": "...", "sector": "...", "software": true, "basis": "description|label|name|knowledge",
   "confidence": "high|medium|low", "reason": "..."}}]"""


# Cells that look like words but aren't a business description: base rates with reset frequency
# ("Base Rate (Q)", "SOFR (M)"), instrument names, and footnote-style notes.
NOT_DESCRIPTION = re.compile(r"\((?:Q|M|S|A|SA)\)\s*$|^(?:base rate|sofr|libor|prime|euribor|sonia|corra)\b|"
                             r"\b(?:loan|notes?|units|shares|interests?|warrants?|revolv\w*|facility|preferred|common)\s*$", re.I)


def _nice(fund: str) -> str:
    """'ARES CAPITAL CORP' -> 'Ares Capital Corp' for evidence the model (and the page) shows."""
    return " ".join(w.capitalize() if w.isupper() and len(re.sub(r"[^A-Za-z]", "", w)) > 3 else w for w in fund.split())


def _description(raw: str, company: str, instrument: Optional[str], industry: Optional[str]) -> Optional[str]:
    """Business description from a schedule row, for filers that print one (e.g. Ares)."""
    skip = {(company or "").strip(), (instrument or "").strip(), (industry or "").strip()}
    cells = [c.strip() for c in (raw or "").split("|")]
    cands = [c for c in cells if len(c.split()) >= 3 and not re.search(r"\d", c) and c not in skip
             and not c.startswith((company or "")[:15]) and not NOT_DESCRIPTION.search(c)]
    return max(cands, key=len) if cands else None


def gather(con: sqlite3.Connection) -> list[dict]:
    """One evidence record per borrower (company_key) across every filing in the database."""
    con.row_factory = sqlite3.Row
    by_key: dict[str, dict] = defaultdict(lambda: {"names": set(), "labels": set(), "descriptions": set()})
    q = "SELECT fund, company, company_key, industry, instrument, raw FROM v_holdings WHERE lien != 'cash'"
    for r in con.execute(q):
        e = by_key[r["company_key"]]
        e["names"].add(r["company"])
        if r["industry"]:
            e["labels"].add(f"{_nice(r['fund'])}: {r['industry']}")
        d = _description(r["raw"], r["company"], r["instrument"], r["industry"])
        if d:
            e["descriptions"].add(d)
    return [{"key": k, "names": sorted(v["names"])[:3], "labels": sorted(v["labels"]),
             "descriptions": sorted(v["descriptions"])[:2]} for k, v in sorted(by_key.items())]


def parse_reply(text: str, keys: list[str]) -> dict[str, dict]:
    """The model's JSON array -> {key: record}, keeping only valid records for keys we asked about."""
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return {}
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    wanted, out = set(keys), {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or it.get("key") not in wanted:
            continue
        sector = it.get("sector") if it.get("sector") in SECTORS else "Other / unclear"
        out[it["key"]] = {
            "sector": sector,
            "software": bool(it.get("software")),
            "basis": it.get("basis") if it.get("basis") in BASES else "name",
            "confidence": it.get("confidence") if it.get("confidence") in CONFIDENCE else "low",
            "reason": str(it.get("reason") or "")[:160],
        }
    return out


def load_cache(path: str) -> dict:
    return json.load(open(path)) if os.path.exists(path) else {}


def save_cache(path: str, cache: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(dict(sorted(cache.items())), fh, indent=1)


def classify(con: sqlite3.Connection, post: Callable[[dict], dict], cache_path: str, model: str,
             batch: int = 40, limit: Optional[int] = None, log=print) -> dict:
    """Classify every borrower not already in the cache. Saves after each batch, so an interrupted run
    resumes where it stopped. Returns counts."""
    cache = load_cache(cache_path)
    todo = [e for e in gather(con) if e["key"] not in cache]
    if limit:
        todo = todo[:limit]
    usage = {"input_tokens": 0, "output_tokens": 0}
    missed = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        keys = [e["key"] for e in chunk]
        got: dict = {}
        for attempt in range(2):  # one retry for a malformed reply
            resp = post({"model": model, "max_tokens": 6000, "system": SYSTEM,
                         "messages": [{"role": "user", "content": json.dumps(chunk, indent=0)}]})
            for k in usage:
                usage[k] += resp.get("usage", {}).get(k, 0)
            text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            got.update(parse_reply(text, [k for k in keys if k not in got]))
            if len(got) == len(keys):
                break
        missed += len(keys) - len(got)
        cache.update(got)
        save_cache(cache_path, cache)
        log(f"  {min(i + batch, len(todo))}/{len(todo)} borrowers classified")
    return {"classified": len(todo) - missed, "missed": missed, "total_cached": len(cache), "usage": usage}


def load_into_db(con: sqlite3.Connection, cache: dict) -> int:
    """(Re)create the `sectors` table from the cache so the agent and analytics can join on company_key."""
    con.execute("DROP TABLE IF EXISTS sectors")
    con.execute("""CREATE TABLE sectors (company_key TEXT PRIMARY KEY, sector TEXT, is_software INTEGER,
                   basis TEXT, confidence TEXT, reason TEXT)""")
    con.executemany("INSERT INTO sectors VALUES (?,?,?,?,?,?)",
                    [(k, v["sector"], int(v["software"]), v["basis"], v["confidence"], v["reason"]) for k, v in cache.items()])
    con.commit()
    return len(cache)


def has_sectors(con: sqlite3.Connection) -> bool:
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sectors'").fetchone()) and \
        bool(con.execute("SELECT 1 FROM sectors LIMIT 1").fetchone())
