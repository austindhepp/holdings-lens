"""Scorecard: how accurate is extraction, and how trustworthy are the agent's answers?

Extraction is measured two ways:
  * against hand-checked gold rows (cell by cell), and
  * against the filing's own inline XBRL tags (no answer key needed; see reconcile.py).
Agent answers are checked against values computed by SQL, plus citation grounding and cost.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time

from .agent import ask
from .db import DEFAULT_DB, connect_readonly
from .extract import extract_holdings
from .reconcile import reconcile

FIELDS = ["investment_type", "company", "industry", "instrument", "interest_rate", "spread", "maturity",
          "principal", "cost", "fair_value", "pct_net_assets", "non_accrual", "pik"]
NUM_FIELDS = {"principal", "cost", "fair_value", "pct_net_assets"}
BOOL_FIELDS = {"non_accrual", "pik"}


def _norm(v):
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip().lower()


def _num_eq(a, b, tol=0.005):
    if a in (None, "") and b in (None, ""):
        return True
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= tol * max(1.0, abs(b))


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes")


def eval_extraction(html_path: str, gold_csv: str) -> dict:
    html = open(html_path, encoding="utf-8").read()
    hs = extract_holdings(html)
    got = [h.to_dict() for h in hs]
    gold = list(csv.DictReader(open(gold_csv)))
    fields = [f for f in FIELDS if gold and f in gold[0]]
    key = lambda r: (_norm(r["company"]), _norm(r["instrument"]), _norm(r["investment_type"]))
    # Funds hold several tranches of the same loan (same company, instrument and section), so pair
    # each gold row with the most similar unused extracted row in its group, not just the first one.
    groups: dict = {}
    for r in got:
        groups.setdefault(key(r), []).append(r)

    def similarity(g, r):
        return sum(_num_eq(r[f], g[f]) if f in NUM_FIELDS else _norm(r[f]) == _norm(g[f]) for f in fields if f in r)

    matched, hits, total, mistakes = 0, {f: 0 for f in fields}, {f: 0 for f in fields}, []
    for g in gold:
        cands = groups.get(key(g), [])
        r = max(cands, key=lambda c: similarity(g, c)) if cands else None
        if r is not None:
            cands.remove(r)
        if not r:
            mistakes.append({"missing_row": f"{g['company']} / {g['instrument']}"})
            continue
        matched += 1
        for f in fields:
            total[f] += 1
            if f in NUM_FIELDS:
                ok = _num_eq(r[f], g[f])
            elif f in BOOL_FIELDS:
                ok = _bool(r[f]) == _bool(g[f])
            else:
                ok = _norm(r[f]) == _norm(g[f])
            hits[f] += ok
            if not ok:
                mistakes.append({"company": g["company"], "field": f, "expected": g[f], "got": r[f]})
    ok_cells, cells = sum(hits.values()), sum(total.values())
    return {
        "file": os.path.basename(html_path),
        "rows_expected": len(gold), "rows_extracted": len(got), "rows_matched": matched,
        "row_recall": matched / len(gold) if gold else 0.0,
        "row_precision": matched / len(got) if got else 0.0,
        "field_accuracy": {f: (hits[f] / total[f] if total[f] else None) for f in fields},
        "cells_correct": ok_cells, "cells": cells,
        "cell_accuracy": ok_cells / cells if cells else 0.0,
        "mistakes": mistakes[:50],
        "xbrl": reconcile(html, hs),
    }


def eval_manifest(manifest_path: str, root: str) -> dict:
    items = json.load(open(manifest_path))
    per = [eval_extraction(os.path.join(root, m["path"]), os.path.join(root, m["gold"])) for m in items]
    cells = sum(p["cells"] for p in per)
    ok = sum(p["cells_correct"] for p in per)
    exp = sum(p["rows_expected"] for p in per)
    got = sum(p["rows_extracted"] for p in per)
    matched = sum(p["rows_matched"] for p in per)
    linked = sum(p["xbrl"].get("rows_linked", 0) for p in per)
    xok = sum(p["xbrl"].get("rows_matched", 0) for p in per)
    return {
        "filings": per,
        "cell_accuracy": ok / cells if cells else 0.0,
        "cells": cells,
        "row_recall": matched / exp if exp else 0.0,
        "row_precision": matched / got if got else 0.0,
        "rows": exp,
        "xbrl_rows_linked": linked,
        "xbrl_match_rate": xok / linked if linked else None,
        "xbrl_totals_match": all(p["xbrl"].get("totals_match") for p in per if p["xbrl"].get("available")),
    }


# ------------------------------------------------------------------ agent answers
SCALE = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(billion|bn|million|mm|thousand|[bmk])?\b", re.I)


def numbers_in(text: str) -> list[float]:
    out = []
    for m in NUM_RE.finditer(text.replace(",", "")):
        v = float(m.group(1))
        if m.group(2):
            v *= SCALE[m.group(2).lower()]
        out.append(v)
    return out


def _truth(con, item):
    row = con.execute(item["sql"]).fetchone()
    return row[0] if row else None


def answer_ok(answer: str, item: dict, truth) -> bool:
    if item["check"] == "number":
        tol = item.get("tolerance", 0.01)
        t = float(truth)
        return any(abs(n - t) <= tol * max(1.0, abs(t)) for n in numbers_in(answer))
    if item["check"] == "contains":
        return all(s.lower() in answer.lower() for s in item["expected"])
    raise ValueError(item["check"])


def eval_qa(qa_json: str, db_path: str = DEFAULT_DB, dry_run: bool = False, post=None) -> dict:
    items = json.load(open(qa_json))
    con = connect_readonly(db_path)
    results = []
    for it in items:
        truth = _truth(con, it) if it["check"] == "number" else it.get("expected")
        if dry_run:
            results.append({"question": it["question"], "truth": truth})
            continue
        t0 = time.time()
        r = ask(it["question"], db_path=db_path, **({"post": post} if post else {}))
        results.append({
            "question": it["question"], "truth": truth, "answer": r["answer"],
            "correct": answer_ok(r["answer"], it, truth),
            "grounded": r["citations"]["grounded"],
            "bad_citations": r["citations"]["nonexistent"] + r["citations"]["not_retrieved"],
            "tokens": r["usage"]["input_tokens"] + r["usage"]["output_tokens"],
            "seconds": round(time.time() - t0, 1), "run": r,
        })
    if dry_run:
        return {"dry_run": True, "items": results}
    n = len(results) or 1
    return {
        "questions": len(results),
        "accuracy": sum(r["correct"] for r in results) / n,
        "grounded_rate": sum(r["grounded"] for r in results) / n,
        "unsupported_citation_count": sum(len(r["bad_citations"]) for r in results),
        "avg_tokens": sum(r["tokens"] for r in results) / n,
        "avg_seconds": sum(r["seconds"] for r in results) / n,
        "items": results,
    }


def write_scorecard(extraction: dict, qa: dict | None, out_dir: str = "evals") -> str:
    os.makedirs(out_dir, exist_ok=True)
    card = {"extraction": extraction, "qa": qa, "generated": time.strftime("%Y-%m-%d %H:%M")}
    with open(os.path.join(out_dir, "scorecard.json"), "w") as fh:
        json.dump(card, fh, indent=2, default=str)
    e = extraction
    L = ["# Holdings Lens scorecard", "", f"Generated {card['generated']}", "",
         "## Extraction vs hand-checked gold rows",
         f"- {e['rows']} holdings across {len(e['filings'])} filings; recall {e['row_recall']:.0%}, precision {e['row_precision']:.0%}",
         f"- Cell accuracy: {e['cell_accuracy']:.1%} of {e['cells']} cells", "",
         "| Filing | Rows | Cell accuracy | XBRL rows matched | XBRL coverage | Totals |",
         "|---|---|---|---|---|---|"]
    for p in e["filings"]:
        x = p["xbrl"]
        if x.get("available"):
            L.append(f"| {p['file']} | {p['rows_matched']}/{p['rows_expected']} | {p['cell_accuracy']:.1%} | "
                     f"{x['rows_matched']}/{x['rows_linked']} | {x['coverage']:.0%} | {'match' if x['totals_match'] else 'differ'} |")
        else:
            L.append(f"| {p['file']} | {p['rows_matched']}/{p['rows_expected']} | {p['cell_accuracy']:.1%} | n/a | n/a | n/a |")
    mistakes = [m for p in e["filings"] for m in p["mistakes"]]
    if mistakes:
        L += ["", "### Mistakes", ""] + [f"- {json.dumps(m)}" for m in mistakes[:20]]
    if qa and not qa.get("dry_run"):
        L += ["", "## Agent answers",
              f"- Correct: {qa['accuracy']:.0%} of {qa['questions']} questions",
              f"- Grounded (every citation retrieved from the database): {qa['grounded_rate']:.0%}",
              f"- Unsupported citations: {qa['unsupported_citation_count']}",
              f"- Avg tokens per question: {qa['avg_tokens']:.0f}",
              f"- Avg seconds per question: {qa['avg_seconds']:.1f}"]
    path = os.path.join(out_dir, "scorecard.md")
    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")
    return path
