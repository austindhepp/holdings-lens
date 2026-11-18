"""Read inline XBRL (iXBRL) facts embedded in an EDGAR filing.

Most BDC filings now tag their financial statements, often including the schedule
of investments, with machine-readable facts: <ix:nonFraction name="us-gaap:...">
wrapped around the number a person sees. Each fact points to a context, and a
context can carry a dimension member naming the specific investment.

That gives a second, independent source for every number the HTML parser pulls
out. Holdings Lens uses it to reconcile row by row: two methods that agree are
far more trustworthy than one parser grading its own work.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

FAIR_VALUE = "InvestmentOwnedAtFairValue"
COST = "InvestmentOwnedAtCost"
PRINCIPAL = "InvestmentOwnedBalancePrincipalAmount"
# Other concepts filers legitimately tag on fair-value cells (cash equivalents in the schedule)
FAIR_VALUE_LIKE = {FAIR_VALUE, "MoneyMarketFundsFairValueDisclosure", "CashEquivalentsAtCarryingValue"}


@dataclass
class Fact:
    concept: str          # local name, e.g. InvestmentOwnedAtFairValue
    context: str
    value: float          # in whole units (scale applied, sign applied)
    member: Optional[str]  # investment identifier from the context's dimension, if any
    period: Optional[str] = None  # instant date of the context, e.g. 2026-06-30


def _num(text: str) -> Optional[float]:
    t = re.sub(r"[^\d.]", "", text or "")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def contexts(soup) -> dict[str, Optional[str]]:
    """context id -> investment member text (None for undimensioned contexts)."""
    return {cid: m for cid, (m, _) in contexts_full(soup).items()}


def contexts_full(soup) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """context id -> (investment member text, instant/end date)."""
    out = {}
    for c in soup.find_all(["xbrli:context", "context"]):
        cid = c.get("id")
        if not cid:
            continue
        member = None
        # Only dimensions that identify an investment count (not geography, industry, etc.)
        for el in c.find_all(["xbrldi:typedmember", "typedmember", "xbrldi:explicitmember", "explicitmember"]):
            dim = (el.get("dimension") or "").lower()
            if "investment" in dim and "type" not in dim:
                member = re.sub(r"\s+", " ", el.get_text(" ")).strip() or None
                break
        inst = c.find(["xbrli:instant", "instant"]) or c.find(["xbrli:enddate", "enddate"])
        out[cid] = (member, inst.get_text(strip=True) if inst is not None else None)
    return out


def facts(html_or_soup, concepts=(FAIR_VALUE, COST, PRINCIPAL)) -> list[Fact]:
    soup = html_or_soup if not isinstance(html_or_soup, str) else BeautifulSoup(html_or_soup, "lxml")
    ctx = contexts_full(soup)
    out = []
    for el in soup.find_all(["ix:nonfraction", "nonfraction"]):
        name = (el.get("name") or "").split(":")[-1]
        if name not in concepts:
            continue
        v = _num(el.get_text())
        if v is None:
            continue
        scale = int(el.get("scale", "0") or 0)
        v = v * (10 ** scale)
        if el.get("sign") == "-":
            v = -v
        cid = el.get("contextref")
        member, period = ctx.get(cid, (None, None))
        out.append(Fact(name, cid, v, member, period))
    return out


def scale_in(cell) -> Optional[int]:
    el = cell.find(["ix:nonfraction", "nonfraction"]) if cell is not None else None
    return int(el.get("scale", "0") or 0) if el is not None else None


def fact_in(cell) -> Optional[tuple[str, float, str]]:
    """The (contextRef, value in whole units, concept) of the first iXBRL fact inside a table cell."""
    if cell is None:
        return None
    el = cell.find(["ix:nonfraction", "nonfraction"])
    if el is None:
        return None
    v = _num(el.get_text())
    if v is None:
        return None
    v *= 10 ** int(el.get("scale", "0") or 0)
    if el.get("sign") == "-":
        v = -v
    return el.get("contextref"), v, (el.get("name") or "").split(":")[-1]
