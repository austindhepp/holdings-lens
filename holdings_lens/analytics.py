"""Portfolio analytics an analyst would actually ask for.

  summary(filing)          size, lien mix, concentration, industry mix, weighted spread,
                           non-accruals, PIK, mark distribution, watchlist, maturity wall
  changes(old, new)        quarter over quarter: new, exited and re-marked positions
  overlap(filings)         the same borrower held by several funds, and how differently
                           each fund values it (mark dispersion), a useful valuation signal

All functions read the database through plain SQL and return JSON-ready dicts.
"""
from __future__ import annotations

import re
from collections import defaultdict

DEBT = ("first_lien", "second_lien", "unitranche", "subordinated", "other")
MARK_BUCKETS = [("100%+", 1.0, 99), ("97–100%", 0.97, 1.0), ("90–97%", 0.90, 0.97), ("80–90%", 0.80, 0.90), ("<80%", -1, 0.80)]


def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args)]


def _pct(a, b):
    return round(100.0 * a / b, 2) if b else None


def filing_info(con, fid):
    r = con.execute("SELECT id, fund, period, form FROM filings WHERE id = ?", (fid,)).fetchone()
    return dict(r) if r else None


def summary(con, fid: int) -> dict:
    rows = _rows(con, "SELECT * FROM v_holdings WHERE filing_id = ? AND lien != 'cash'", (fid,))
    if not rows:
        return {"filing": filing_info(con, fid), "holdings": 0}
    tot = sum(r["fair_value_usd"] or 0 for r in rows)
    cost = sum(r["cost_usd"] or 0 for r in rows)
    debt = [r for r in rows if r["lien"] in DEBT]
    debt_fv = sum(r["fair_value_usd"] or 0 for r in debt)

    lien_mix = defaultdict(float)
    by_company = defaultdict(float)
    by_industry = defaultdict(float)
    for r in rows:
        lien_mix[r["lien"]] += r["fair_value_usd"] or 0
        by_company[r["company_key"]] += r["fair_value_usd"] or 0
        by_industry[r["industry"] or "Unknown"] += r["fair_value_usd"] or 0
    names = {}
    for r in rows:
        names.setdefault(r["company_key"], r["company"])
    top = sorted(by_company.items(), key=lambda kv: -kv[1])

    spread_w = [(r["spread_bps"], r["fair_value_usd"]) for r in debt if r["spread_bps"] and r["fair_value_usd"]]
    rate_w = [(r["rate_pct"], r["fair_value_usd"]) for r in debt if r["rate_pct"] and r["fair_value_usd"]]
    wa = lambda pairs: round(sum(a * w for a, w in pairs) / sum(w for _, w in pairs), 1) if pairs else None

    na = [r for r in debt if r["non_accrual"]]
    pik = [r for r in debt if r["pik"]]
    buckets = []
    for label, lo, hi in MARK_BUCKETS:
        fv = sum(r["fair_value_usd"] for r in debt if r["mark"] is not None and lo <= r["mark"] < hi)
        buckets.append({"bucket": label, "fair_value_usd": fv, "pct_of_debt": _pct(fv, debt_fv)})
    watch = sorted([r for r in debt if (r["mark"] is not None and r["mark"] < 0.9) or r["non_accrual"]],
                   key=lambda r: r["mark"] if r["mark"] is not None else 0)
    wall = defaultdict(float)
    for r in debt:
        if r["maturity"]:
            wall[r["maturity"][:4]] += r["fair_value_usd"] or 0

    return {
        "filing": filing_info(con, fid),
        "holdings": len(rows),
        "companies": len(by_company),
        "fair_value_usd": tot,
        "cost_usd": cost,
        "fv_to_cost_pct": _pct(tot, cost),
        "lien_mix_pct": {k: _pct(v, tot) for k, v in sorted(lien_mix.items(), key=lambda kv: -kv[1])},
        "top10_concentration_pct": _pct(sum(v for _, v in top[:10]), tot),
        "largest_positions": [{"company": names[k], "fair_value_usd": v, "pct": _pct(v, tot)} for k, v in top[:5]],
        "industry_mix": [{"industry": k, "fair_value_usd": v, "pct": _pct(v, tot)}
                         for k, v in sorted(by_industry.items(), key=lambda kv: -kv[1])],
        "weighted_avg_spread_bps": wa(spread_w),
        "weighted_avg_rate_pct": wa(rate_w),
        "non_accrual": {"count": len(na), "fair_value_usd": sum(r["fair_value_usd"] for r in na),
                        "pct_of_fv": _pct(sum(r["fair_value_usd"] for r in na), tot),
                        "pct_of_cost": _pct(sum(r["cost_usd"] for r in na), cost),
                        "ids": [r["id"] for r in na]},
        "pik": {"count": len(pik), "pct_of_debt_fv": _pct(sum(r["fair_value_usd"] for r in pik), debt_fv),
                "ids": [r["id"] for r in pik]},
        "mark_distribution": buckets,
        "watchlist": [{"id": r["id"], "company": r["company"], "instrument": r["instrument"], "mark": r["mark"],
                       "fair_value_usd": r["fair_value_usd"], "non_accrual": bool(r["non_accrual"]), "pik": bool(r["pik"])}
                      for r in watch],
        "maturity_wall": [{"year": y, "fair_value_usd": v} for y, v in sorted(wall.items())],
    }


def _norm_inst(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def changes(con, old_id: int, new_id: int, mark_threshold: float = 0.02) -> dict:
    """Quarter over quarter for one fund. A position = borrower + lien + instrument, with all of its
    tranches added together (funds often hold several tranches of the same loan)."""
    def positions(fid):
        out = {}
        for r in _rows(con, "SELECT * FROM v_holdings WHERE filing_id = ? AND lien != 'cash'", (fid,)):
            k = (r["company_key"], r["lien"], _norm_inst(r["instrument"]))
            p = out.setdefault(k, {"ids": [], "company": r["company"], "instrument": r["instrument"], "lien": r["lien"],
                                   "fv": 0.0, "cost": 0.0, "principal": 0.0, "non_accrual": False})
            p["ids"].append(r["id"])
            p["fv"] += r["fair_value_usd"] or 0
            p["cost"] += r["cost_usd"] or 0
            p["principal"] += r["principal_usd"] or 0
            p["non_accrual"] = p["non_accrual"] or bool(r["non_accrual"])
        for p in out.values():
            p["id"] = p["ids"][0]
            p["mark"] = round(p["fv"] / p["cost"], 4) if p["cost"] > 0 and p["fv"] >= 0 else None
        return out
    old, new = positions(old_id), positions(new_id)
    brief = lambda p: {"id": p["id"], "ids": p["ids"], "company": p["company"], "instrument": p["instrument"], "fair_value_usd": p["fv"]}
    new_keys, old_keys = set(new.keys() - old.keys()), set(old.keys() - new.keys())
    # Borrowers get renamed between quarters ("Acme Purchaser III & Jet Tools Ltd." -> "Acme Purchaser III").
    # Pair a new and an exited position with the same lien and instrument whose names share their first two words.
    renamed = []
    stem = lambda k: " ".join(k[0].split()[:2])
    for nk in sorted(new_keys):
        match = next((ok for ok in sorted(old_keys) if ok[1:] == nk[1:] and stem(ok) == stem(nk) and len(stem(nk)) > 3), None)
        if match:
            renamed.append({"id": new[nk]["id"], "prior_id": old[match]["id"], "from": old[match]["company"],
                            "to": new[nk]["company"], "instrument": new[nk]["instrument"]})
            new_keys.discard(nk)
            old_keys.discard(match)
            old[nk] = old.pop(match)  # compare marks and size as one continuing position
    added = sorted([brief(new[k]) for k in new_keys], key=lambda r: -r["fair_value_usd"])
    exited = sorted([brief(old[k]) for k in old_keys], key=lambda r: -r["fair_value_usd"])
    remarked, resized = [], []
    for k in new.keys() & old.keys():
        o, n = old[k], new[k]
        if n["lien"] in DEBT and o["mark"] is not None and n["mark"] is not None and abs(n["mark"] - o["mark"]) >= mark_threshold:
            remarked.append({"id": n["id"], "ids": n["ids"], "prior_id": o["id"], "company": n["company"], "instrument": n["instrument"],
                             "prior_mark": o["mark"], "mark": n["mark"], "change_pts": round(100 * (n["mark"] - o["mark"]), 1),
                             "fair_value_change_usd": n["fv"] - o["fv"], "now_non_accrual": n["non_accrual"] and not o["non_accrual"]})
        if o["principal"] and n["principal"] and abs(n["principal"] - o["principal"]) / o["principal"] >= 0.05:
            resized.append({"id": n["id"], "ids": n["ids"], "prior_id": o["id"], "company": n["company"], "instrument": n["instrument"],
                            "prior_principal_usd": o["principal"], "principal_usd": n["principal"]})
    newly_na = sorted([{"id": new[k]["id"], "ids": new[k]["ids"], "company": new[k]["company"], "instrument": new[k]["instrument"],
                        "fair_value_usd": new[k]["fv"]}
                       for k in new if new[k]["non_accrual"] and not (old.get(k) or {}).get("non_accrual")],
                      key=lambda r: -r["fair_value_usd"])
    tot = lambda fid: con.execute("SELECT COALESCE(SUM(fair_value_usd),0) FROM v_holdings WHERE filing_id = ? AND lien != 'cash'", (fid,)).fetchone()[0]
    return {
        "from": filing_info(con, old_id), "to": filing_info(con, new_id),
        "fair_value_usd": {"from": tot(old_id), "to": tot(new_id), "change": tot(new_id) - tot(old_id)},
        "new_positions": added, "exited_positions": exited,
        "remarked": sorted(remarked, key=lambda r: r["change_pts"]),
        "resized": sorted(resized, key=lambda r: -abs(r["principal_usd"] - r["prior_principal_usd"])),
        "newly_non_accrual": newly_na,
        "renamed": renamed,
    }


def overlap(con, filing_ids: list[int] | None = None) -> dict:
    """Borrowers held by more than one fund, with each fund's debt mark (fair value / cost)."""
    if filing_ids is None:
        filing_ids = [r["id"] for r in _rows(con, """SELECT f.id FROM filings f
            JOIN (SELECT fund, MAX(period) p FROM filings GROUP BY fund) l ON l.fund = f.fund AND l.p = f.period""")]
    if not filing_ids:
        return {"filings": [], "shared_borrowers": []}
    q = f"SELECT * FROM v_holdings WHERE filing_id IN ({','.join('?' * len(filing_ids))}) AND lien IN ({','.join('?' * len(DEBT))})"
    agg = defaultdict(lambda: defaultdict(lambda: {"fv": 0.0, "cost": 0.0, "ids": [], "name": None, "spreads": []}))
    for r in _rows(con, q, list(filing_ids) + list(DEBT)):
        a = agg[r["company_key"]][r["fund"]]
        a["fv"] += r["fair_value_usd"] or 0
        a["cost"] += r["cost_usd"] or 0
        a["ids"].append(r["id"])
        a["name"] = a["name"] or r["company"]
        if r["spread_bps"]:
            a["spreads"].append(r["spread_bps"])
    shared = []
    for key, funds in agg.items():
        if len(funds) < 2:
            continue
        per = [{"fund": f, "company_as_reported": v["name"], "mark": round(v["fv"] / v["cost"], 4) if v["cost"] else None,
                "fair_value_usd": v["fv"], "spread_bps": (sum(v["spreads"]) / len(v["spreads"])) if v["spreads"] else None,
                "ids": v["ids"]} for f, v in sorted(funds.items())]
        marks = [p["mark"] for p in per if p["mark"] is not None]
        shared.append({"borrower": per[0]["company_as_reported"], "company_key": key, "funds": per,
                       "mark_dispersion_pts": round(100 * (max(marks) - min(marks)), 1) if len(marks) > 1 else None})
    shared.sort(key=lambda s: -(s["mark_dispersion_pts"] or 0))
    return {"filings": [filing_info(con, i) for i in filing_ids], "shared_borrowers": shared}


def full_report(con) -> dict:
    """Everything, for the landing page and the report command."""
    filings = _rows(con, "SELECT id, fund, period FROM filings ORDER BY fund, period")
    by_fund = defaultdict(list)
    for f in filings:
        by_fund[f["fund"]].append(f["id"])
    return {
        "summaries": [summary(con, f["id"]) for f in filings],
        "changes": [changes(con, ids[-2], ids[-1]) for ids in by_fund.values() if len(ids) >= 2],
        "overlap": overlap(con),
    }


def fmt_usd(v):
    if v is None:
        return "–"
    return f"${v / 1e6:,.1f}M" if abs(v) >= 1e6 else f"${v:,.0f}"


def _p(v):
    return "n/a" if v is None else f"{v:.0%}"


def report_markdown(rep: dict, limit: int = 15) -> str:
    L = ["# Portfolio report", ""]
    for s in rep["summaries"]:
        f = s["filing"]
        L += [f"## {f['fund']}, {f['period']}", "",
              f"- {s['holdings']} holdings in {s['companies']} companies; fair value {fmt_usd(s['fair_value_usd'])} "
              f"({s['fv_to_cost_pct']}% of cost)",
              f"- Lien mix (% of fair value): " + ", ".join(f"{k} {v}%" for k, v in s["lien_mix_pct"].items()),
              f"- Top-10 concentration: {s['top10_concentration_pct']}%",
              f"- Weighted average spread (debt): {s['weighted_avg_spread_bps']} bps; weighted average rate {s['weighted_avg_rate_pct']}%",
              f"- Non-accrual: {s['non_accrual']['count']} positions, {s['non_accrual']['pct_of_fv']}% of fair value "
              f"({s['non_accrual']['pct_of_cost']}% of cost)",
              f"- PIK: {s['pik']['count']} positions, {s['pik']['pct_of_debt_fv']}% of debt fair value", ""]
        if s["watchlist"]:
            L += ["Watchlist (debt below 90% of cost or on non-accrual):", ""]
            L += [f"- {w['company']} {w['instrument'] or ''}: {_p(w['mark'])} of cost"
                  f"{' · non-accrual' if w['non_accrual'] else ''}{' · PIK' if w['pik'] else ''} [H{w['id']}]"
                  for w in s["watchlist"][:limit]]
            if len(s["watchlist"]) > limit:
                L.append(f"- …and {len(s['watchlist']) - limit} more")
            L.append("")
    for c in rep["changes"]:
        L += [f"## Changes: {c['to']['fund']}, {c['from']['period']} → {c['to']['period']}", "",
              f"- Fair value {fmt_usd(c['fair_value_usd']['from'])} → {fmt_usd(c['fair_value_usd']['to'])}",
              f"- New positions: {len(c['new_positions'])}; exited: {len(c['exited_positions'])}; "
              f"re-marked by 2+ pts: {len(c['remarked'])}; resized: {len(c['resized'])}; renamed: {len(c.get('renamed', []))}",
              "- Largest new: " + (", ".join(f"{p['company']} {p['instrument']} {fmt_usd(p['fair_value_usd'])} [H{p['id']}]" for p in c["new_positions"][:5]) or "none"),
              "- Largest exits: " + (", ".join(f"{p['company']} {p['instrument']} {fmt_usd(p['fair_value_usd'])} [H{p['id']}]" for p in c["exited_positions"][:5]) or "none")]
        if c.get("newly_non_accrual"):
            L.append("- Newly on non-accrual: " + ", ".join(f"{p['company']} {fmt_usd(p['fair_value_usd'])} [H{p['id']}]"
                                                          for p in c["newly_non_accrual"][:8]))
        L += [f"- Re-marked (debt): {r['company']} {r['instrument'] or ''} {_p(r['prior_mark'])} → {_p(r['mark'])} "
              f"({r['change_pts']:+.1f} pts) [H{r['id']}]" for r in c["remarked"][:limit]]
        L += [f"- Resized: {r['company']} {r['instrument']} principal {fmt_usd(r['prior_principal_usd'])} → "
              f"{fmt_usd(r['principal_usd'])} [H{r['id']}]" for r in c["resized"][:5]]
        L.append("")
    if rep["overlap"]["shared_borrowers"]:
        L += ["## Same borrower, different marks", ""]
        for s in rep["overlap"]["shared_borrowers"]:
            L.append(f"- {s['borrower']}: " + "; ".join(f"{p['fund']} {_p(p['mark'])}" for p in s["funds"])
                     + f" (spread of {s['mark_dispersion_pts']} pts)")
    return "\n".join(L) + "\n"
