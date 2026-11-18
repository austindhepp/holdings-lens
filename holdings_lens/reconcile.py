"""Reconcile extracted holdings against the filing's own inline XBRL facts.

Three independent checks that need no hand-made answer key:
  1. Row values: the fact tagged on the cell we read as fair value must BE a fair-value
                 fact, with the same value once its scale is applied (catches a wrong column
                 mapping or a units mistake).
  2. Coverage:   every investment the filing tags should appear in the extraction
                 (an XBRL member with no extracted row = a row the parser missed).
  3. Totals:     the sum of extracted fair values vs. the filing's tagged total.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from . import xbrl
from .extract import UNIT_MULT, Holding, company_key


import difflib
import re


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _member_name(m: str) -> str:
    """'arcc:A15484880CanadaInc.Member' -> '15484880canadainc' (XBRL names can't start with a digit)."""
    name = m.split("|")[0].split(":")[-1]
    name = re.sub(r"Member$", "", name)
    return _squash(re.sub(r"^A(?=\d)", "", name))


def _is_category(m: str) -> bool:
    """Standard grouping members (affiliated/unaffiliated issuer, etc.), not individual investments."""
    return m.split(":")[0].lower() in ("us-gaap", "ifrs-full", "srt", "dei") or m.endswith("IssuerMember")


def _member_found(name: str, squashed: set, raws: list) -> bool:
    if not name:
        return False
    if name in squashed:
        return True
    if len(name) >= 15 and any(name in r for r in raws):  # some filers tag by the business description
        return True
    # tolerate a filer's typo in the tag ("Ferrelgas" vs "Ferrellgas")
    return bool(difflib.get_close_matches(name, squashed, n=1, cutoff=0.93))


def reconcile(html: str, holdings: list[Holding], tol: float = 0.5) -> dict:
    import warnings
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(html, "lxml")
    facts = xbrl.facts(soup, concepts=(xbrl.FAIR_VALUE,))
    periods = [h.xbrl_period for h in holdings if h.xbrl_period]
    period = max(set(periods), key=periods.count) if periods else None
    if period:  # only compare with the schedule these holdings came from
        facts = [f for f in facts if f.period == period]
    if not facts:
        return {"available": False, "reason": "no inline XBRL fair-value facts in this filing"}

    mult = UNIT_MULT.get(holdings[0].units if holdings else "dollars", 1)
    linked = [h for h in holdings if h.xbrl_fair_value is not None]
    mismatches = []
    for h in linked:
        wrong_column = h.xbrl_concept not in (None, *xbrl.FAIR_VALUE_LIKE)
        if wrong_column or abs(h.fair_value * mult - h.xbrl_fair_value) > tol * mult:
            mismatches.append({"company": h.company, "instrument": h.instrument,
                               "extracted": h.fair_value * mult, "xbrl": h.xbrl_fair_value,
                               "problem": f"read the {h.xbrl_concept} column as fair value" if wrong_column
                               else "value or scale differs from the XBRL tag"})
    untagged = [{"company": h.company, "instrument": h.instrument, "fair_value": h.fair_value}
                for h in holdings if h.xbrl_fair_value is None and h.fair_value]

    tagged_members = {f.member for f in facts if f.member and not _is_category(f.member)}
    extracted_members = {h.xbrl_member for h in linked if h.xbrl_member}
    keys = {h.company_key for h in holdings}
    # A tagged member is covered if its exact row was extracted, or (for company-level tags in the
    # notes, e.g. affiliate rollforwards) if that company appears in the extraction under any spelling.
    # Explicit members are spelled the XBRL way ("arcc:ATIRestorationLLCMember"): compare them squashed.
    squashed = {_squash(h.company or "") for h in holdings}
    raws = [_squash(h.raw or "") for h in holdings]
    missed = sorted(m for m in tagged_members - extracted_members
                    if not _is_category(m) and company_key(m.split("|")[0]) not in keys
                    and not _member_found(_member_name(m), squashed, raws))

    totals = [f.value for f in facts if not f.member]
    filing_total = max(totals) if totals else None  # "Total investments" is the largest undimensioned FV
    cash_like = [h for h in holdings if h.xbrl_concept and h.xbrl_concept != xbrl.FAIR_VALUE]
    extracted_total = sum((h.fair_value or 0) for h in holdings if h not in cash_like) * mult
    total_diff = None if filing_total is None else extracted_total - filing_total

    n = len(holdings)
    return {
        "available": True,
        "period": period,
        "rows_extracted": n,
        "rows_linked": len(linked),
        "rows_matched": len(linked) - len(mismatches),
        "row_match_rate": (len(linked) - len(mismatches)) / len(linked) if linked else None,
        "tagged_investments": len(tagged_members),
        "tagged_but_not_extracted": missed,
        "coverage": (len(tagged_members) - len(missed)) / len(tagged_members) if tagged_members else None,
        "extracted_total": extracted_total,
        "filing_total": filing_total,
        "total_difference": total_diff,
        "totals_match": total_diff is not None and abs(total_diff) <= tol * mult * max(1, n),
        "total_difference_pct": (100.0 * total_diff / filing_total) if total_diff is not None and filing_total else None,
        "cash_equivalent_rows_excluded": len(cash_like),
        "mismatches": mismatches,
        "untagged_rows": untagged,
    }


def summary_line(r: dict) -> str:
    if not r.get("available"):
        return "XBRL cross-check: not available (" + r.get("reason", "") + ")"
    rate, cov = r["row_match_rate"], r["coverage"]
    if r["totals_match"]:
        totals = "totals match"
    else:
        pct = r.get("total_difference_pct")
        totals = f"totals differ by {r['total_difference'] or 0:,.0f}" + (f" ({pct:+.2f}%)" if pct is not None else "")
    return (f"XBRL cross-check: {r['rows_matched']}/{r['rows_linked']} tagged rows match"
            f"{f' ({rate:.0%})' if rate is not None else ''}; coverage {cov:.0%} of tagged investments; {totals}")
