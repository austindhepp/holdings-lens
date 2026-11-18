"""Build one personalized landing page per recipient.

Inputs:
  web/site.json        your name, email, GitHub, résumé link
  web/recipients.csv   slug, first_name, company, focus, note   (one row per person)
  web/demo_run.json    output of `python -m holdings_lens demo` (optional)
Output:
  web/dist/index.html                generic page
  web/dist/for/<slug>/index.html     one page per recipient

Host web/dist anywhere static (GitHub Pages, Netlify, Vercel) and send each
person their own /for/<slug>/ link.
"""
import csv
import html
import json
import os
import sys
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from holdings_lens import db as hdb  # noqa: E402
from holdings_lens.__main__ import MANIFEST, cmd_extract  # noqa: E402
from holdings_lens.evals import eval_manifest  # noqa: E402


def load_demo():
    p = os.path.join(HERE, "demo_run.json")
    if os.path.exists(p):
        return json.load(open(p))
    qs = json.load(open(os.path.join(ROOT, "evals", "qa.json")))
    return {"placeholder": True, "label": "Bundled sample filings (fictional funds)",
            "examples": [{"question": q["question"], "answer": None, "sources": [],
                          "expected": ", ".join(q["expected"]) if q["check"] == "contains" else ""} for q in qs]}


def count_tests():
    import re
    import subprocess
    out = subprocess.run([sys.executable, "-m", "pytest", "--co", "-q", os.path.join(ROOT, "tests")],
                         capture_output=True, text=True, cwd=ROOT).stdout
    m = re.search(r"(\d+) tests? collected", out)
    return int(m.group(1)) if m else 0


LIVE_DB = os.path.join(ROOT, "api", "holdings.db")
LOAN_TYPE = {"first_lien": "First lien", "unitranche": "Unitranche (one stop)", "second_lien": "Second lien",
             "subordinated": "Subordinated", "other": "Other debt", "equity": "Equity", "cash": "Cash equivalents"}
MARK_ORDER = ["100%+", "97–100%", "90–97%", "80–90%", "<80%"]


def nice_fund(name: str) -> str:
    """'GOLUB CAPITAL BDC, Inc.' -> 'Golub Capital BDC'; 'ARES CAPITAL CORP' -> 'Ares Capital'."""
    import re
    words = [w.capitalize() if w.isupper() and len(re.sub(r"[^A-Za-z]", "", w)) > 3 else w for w in name.split()]
    out = " ".join(words)
    return re.sub(r",?\s+(Inc\.?|Corp\.?|Corporation|Fund)$", "", out, flags=re.I).strip()


def explorer_data(con) -> dict:
    """Everything the interactive explorer needs, precomputed per filing so the page needs no server:
    headline numbers, breakdowns by five dimensions, the watchlist, quarter-over-quarter changes per
    lender, and the shared-borrower comparison for every pair of filings from different lenders."""
    from collections import defaultdict
    from holdings_lens.analytics import summary, changes, overlap, MARK_BUCKETS, DEBT
    filings = [dict(r) for r in con.execute("SELECT id, fund, period FROM filings ORDER BY fund, period")]
    out = {"filings": [], "by_filing": {}, "changes": [], "overlap": {}}
    for f in filings:
        fid = f["id"]
        s = summary(con, fid)
        rows = [dict(r) for r in con.execute("SELECT * FROM v_holdings WHERE filing_id = ? AND lien != 'cash'", (fid,))]
        dims = {d: defaultdict(lambda: {"fv": 0.0, "cost": 0.0, "principal": 0.0, "n": 0})
                for d in ("industry", "loan_type", "maturity_year", "debt_mark", "base_rate")}

        def add(dim, key, r):
            a = dims[dim][key]
            a["fv"] += r["fair_value_usd"] or 0
            a["cost"] += r["cost_usd"] or 0
            a["principal"] += r["principal_usd"] or 0
            a["n"] += 1

        for r in rows:
            add("industry", r["industry"] or "Unclassified", r)
            add("loan_type", LOAN_TYPE.get(r["lien"], r["lien"]), r)
            if r["lien"] in DEBT:
                add("maturity_year", (r["maturity"] or "")[:4] or "No date", r)
                add("base_rate", r["base_rate"] or "Fixed rate / none", r)
                if r["mark"] is not None:
                    label = next(lb for lb, lo, hi in MARK_BUCKETS if lo <= r["mark"] < hi)
                    add("debt_mark", label, r)
        debt_fv = sum(r["fair_value_usd"] or 0 for r in rows if r["lien"] in DEBT)
        out["filings"].append({"id": fid, "fund": nice_fund(f["fund"]), "period": f["period"],
                               "label": f"{nice_fund(f['fund'])} · {f['period']}"})
        out["by_filing"][fid] = {
            "kpis": {"fair_value": s["fair_value_usd"], "holdings": s["holdings"], "borrowers": s["companies"],
                     "fv_to_cost": s["fv_to_cost_pct"], "spread_bps": s["weighted_avg_spread_bps"],
                     "non_accrual_cost": s["non_accrual"]["pct_of_cost"], "non_accrual_fv": s["non_accrual"]["pct_of_fv"],
                     "non_accrual_n": s["non_accrual"]["count"], "pik_debt": s["pik"]["pct_of_debt_fv"],
                     "top10": s["top10_concentration_pct"], "debt_share": round(100 * debt_fv / s["fair_value_usd"], 1)},
            "dims": {d: [dict(k=k, **v) for k, v in vals.items()] for d, vals in dims.items()},
            "watch": [{k: w[k] for k in ("company", "instrument", "mark", "fair_value_usd", "non_accrual", "pik")}
                      for w in s["watchlist"][:10]],
            "largest": s["largest_positions"],
        }
    by_fund = defaultdict(list)
    for f in filings:
        by_fund[f["fund"]].append(f["id"])
    keep = lambda xs, n=12: xs[:n]
    for fund, ids in by_fund.items():
        if len(ids) >= 2:
            c = changes(con, ids[-2], ids[-1])
            out["changes"].append({"fund": nice_fund(fund), "from": c["from"]["period"], "to": c["to"]["period"],
                                   "fair_value": c["fair_value_usd"],
                                   "counts": {k: len(c[k]) for k in ("new_positions", "exited_positions", "remarked", "resized", "newly_non_accrual")},
                                   **{k: keep(c[k]) for k in ("remarked", "new_positions", "exited_positions", "resized", "newly_non_accrual")}})
    funds = list(by_fund)
    for i, fa in enumerate(funds):
        for fb in funds[i + 1:]:
            for ia in by_fund[fa]:
                for ib in by_fund[fb]:
                    o = overlap(con, [ia, ib])
                    out["overlap"][f"{ia}-{ib}"] = [
                        {"b": x["borrower"], "gap": x["mark_dispersion_pts"],
                         "f": [{"fund": nice_fund(p["fund"]), "mark": p["mark"], "fv": p["fair_value_usd"]} for p in x["funds"]]}
                        for x in o["shared_borrowers"]]
    out["funds"] = [nice_fund(f) for f in funds]
    out["by_fund"] = {nice_fund(f): ids for f, ids in by_fund.items()}
    return out


def page_data(demo):
    import tempfile
    items = json.load(open(MANIFEST))
    # Always build analytics from a fresh database. (ignore_cleanup_errors: Windows can't delete a file
    # that another process, e.g. antivirus, still has open; a leftover temp file is harmless.)
    real = _has_filings(LIVE_DB)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        if real:  # the real SEC filings exported for the live ask box
            path = LIVE_DB
        else:     # fresh checkout without real data: fall back to the bundled fictional filings
            path = os.path.join(tmp, "h.db")
            cmd_extract([os.path.join(ROOT, m["path"]) for m in items], db_path=path, metas=items, quiet=True)
        con = hdb.connect_readonly(path)
        try:
            explorer = explorer_data(con)
        finally:
            con.close()
    ex = eval_manifest(MANIFEST, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    import test_layouts  # noqa: E402
    sc = dict(demo.get("scorecard") or {})
    sc.update(cell_accuracy=ex["cell_accuracy"], cells=ex["cells"], row_recall=ex["row_recall"],
              xbrl_match_rate=ex["xbrl_match_rate"], layouts=len(test_layouts.VARIANTS) + 1 + len({m["layout"] for m in items}),
              tests=count_tests())
    trust = []
    for m, p in zip(items, ex["filings"]):
        x = p["xbrl"]
        trust.append({"fund": m["fund"], "period": m["period"], "layout": m["layout"], "rows_matched": p["rows_matched"],
                      "rows_expected": p["rows_expected"], "cell_accuracy": p["cell_accuracy"],
                      "xbrl_matched": x.get("rows_matched", 0), "xbrl_linked": x.get("rows_linked", 0),
                      "coverage": x.get("coverage"), "totals_match": bool(x.get("totals_match"))})
    real_path = os.path.join(ROOT, "evals", "real_results.json")
    real_results = json.load(open(real_path)) if os.path.exists(real_path) else None
    return {
        "demo": demo, "scorecard": sc, "explorer": explorer, "explorer_is_real": real, "trust": trust, "real": real_results,
        "ask_url": None,  # set per site in main()
        "trust_note": ("Real filings are checked against their own XBRL tags (every row, coverage and totals); field-level accuracy "
                       "was hand-checked on a sample. The synthetic filings were written to stress unusual table layouts and come "
                       "with exact answer keys; they run in the test suite on every change."),
    }


def _has_filings(path: str) -> bool:
    if not os.path.exists(path):
        return False
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        con.close()
        return n > 0
    except sqlite3.Error:
        return False


def render(tpl, site, data, r=None):
    first = (r or {}).get("first_name", "").strip()
    company = (r or {}).get("company", "").strip()
    focus = (r or {}).get("focus", "").strip()
    note = (r or {}).get("note", "").strip()
    c, fo = html.escape(company), html.escape(focus)
    target = f"the {fo} at {c}" if ("'" in company or company.endswith("s")) else f"{c}'s {fo}"
    if r and first:
        headline = f"Hi {html.escape(first)}, I built this with {target} in mind."
    elif r:
        headline = f"Built with {target} in mind."
    else:
        headline = "Clean, cited data from messy fund filings."
    subject = quote(f"Coffee? (Holdings Lens{' / ' + company if company else ''})")
    rep = {
        "{{TITLE_SUFFIX}}": f" · for {html.escape(company)}" if company else "",
        "{{NAME}}": html.escape(site["name"]),
        "{{HEADLINE}}": headline,
        "{{NOTE_BLOCK}}": f'<div class="note">{html.escape(note)}</div>' if note else "",
        "{{CTA_TEXT}}": html.escape(site.get("cta_text", "")),
        "{{MAILTO}}": f"mailto:{site['email']}?subject={subject}",
        "{{GITHUB}}": html.escape(site["github"]),
        "{{RESUME_LINK}}": (f'<a class="btn ghost" href="{html.escape(site["resume_url"])}">Résumé</a>'
                            if site.get("resume_url") else ""),
        "{{DATA_LABEL}}": html.escape(data["demo"].get("label", "")),
        "{{DATA_JSON}}": json.dumps(data, default=str).replace("</", "<\\/"),
    }
    out = tpl
    for k, v in rep.items():
        out = out.replace(k, v)
    return out


def main():
    site = json.load(open(os.path.join(HERE, "site.json")))
    tpl = open(os.path.join(HERE, "templates", "page.html")).read()
    data = page_data(load_demo())
    data["ask_url"] = site.get("ask_url", "/api/ask") or None  # "" in site.json turns the live box off
    dist = os.path.join(HERE, "dist")
    os.makedirs(dist, exist_ok=True)
    open(os.path.join(dist, "index.html"), "w").write(render(tpl, site, data))
    n = 0
    for r in csv.DictReader(open(os.path.join(HERE, "recipients.csv"))):
        slug = r["slug"].strip().lower()
        if not slug or not slug.replace("-", "").isalnum():
            print(f"skipping bad slug: {slug!r}")
            continue
        d = os.path.join(dist, "for", slug)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "index.html"), "w").write(render(tpl, site, data, r))
        n += 1
        print(f"  /for/{slug}/")
    print(f"Built generic page + {n} personalized pages in {dist}")


if __name__ == "__main__":
    main()
