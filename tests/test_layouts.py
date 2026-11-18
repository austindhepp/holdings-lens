"""Stress tests: the same three holdings rendered in many different table layouts.

Each variant changes something real filers do differently. The extractor must
recover the same values every time, without per-layout code.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from holdings_lens.extract import extract_holdings

ROWS = [  # company, industry, instrument, spread, maturity, principal, cost, fair value
    ("Alpha Software, Inc.", "Software", "First Lien Term Loan", "S + 5.25%", "2029-06-30", 10000, 9850, 9900),
    ("Alpha Software, Inc.", "Software", "Revolver", "S + 5.25%", "2029-06-30", 500, 490, 495),
    ("Beta Health LLC", "Health Care", "First Lien Term Loan", "S + 6.00%", "2028-03-31", 8000, 7920, 6800),
]


def table(header, body_rows, th=False, title="(in thousands)"):
    cell = "th" if th else "td"
    h = "".join(f"<{cell}>{x}</{cell}>" for x in header)
    body = "".join("<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>" for r in body_rows)
    return f"<html><body><p>Schedule of Investments {title}</p><table><tr>{h}</tr>{body}</table></body></html>"


def base(r, fmt=lambda v: f"{v:,}", company_first_only=False, prev=None):
    co = r[0] if not (company_first_only and prev == r[0]) else ""
    return [co, r[1], r[2], r[3], r[4], fmt(r[5]), fmt(r[6]), fmt(r[7])]


def standard(**kw):
    rows, prev = [], None
    for r in ROWS:
        rows.append(base(r, prev=prev, **kw))
        prev = r[0]
    return rows


HEAD = ["Portfolio Company", "Industry", "Investment", "Spread", "Maturity", "Principal", "Amortized Cost", "Fair Value"]

VARIANTS = {
    "plain": table(HEAD, standard()),
    "th_headers": table(HEAD, standard(), th=True),
    "company_on_first_row_only": table(HEAD, standard(company_first_only=True)),
    "reordered_columns": table(
        ["Fair Value", "Issuer", "Amortized Cost", "Sector", "Facility", "Par Amount", "Index / Spread", "Maturity Date"],
        [[f"{r[7]:,}", r[0], f"{r[6]:,}", r[1], r[2], f"{r[5]:,}", r[3], r[4]] for r in ROWS]),
    "two_row_header": (
        "<html><body><p>(in thousands)</p><table>"
        "<tr><td>Portfolio</td><td></td><td>Type of</td><td>Reference Rate</td><td>Maturity</td><td>Principal</td><td>Amortized</td><td>Fair</td></tr>"
        "<tr><td>Company</td><td>Industry</td><td>Investment</td><td>and Spread</td><td>Date</td><td>Amount</td><td>Cost</td><td>Value</td></tr>"
        + "".join("<tr>" + "".join(f"<td>{x}</td>" for x in base(r)) + "</tr>" for r in ROWS) + "</table></body></html>"),
    "dollar_cells_and_colspans": (
        "<html><body><p>(dollars in thousands)</p><table><tr><td>Company</td><td>Industry</td><td>Investment</td><td>Spread</td>"
        "<td>Maturity</td><td colspan='2'>Principal</td><td colspan='2'>Cost</td><td colspan='2'>Fair Value</td></tr>"
        + "".join(f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td><td>{r[4]}</td>"
                  f"<td></td><td>{r[5]:,}</td><td>$</td><td>{r[6]:,}</td><td>$</td><td>{r[7]:,}</td></tr>" for r in ROWS)
        + "</table></body></html>"),
    "footnotes_and_nbsp": table(HEAD, [[f"{r[0]}<sup>(1)(4)</sup>", r[1], f"{r[2]}&nbsp;<sup>(2)</sup>", r[3], r[4],
                                         f"{r[5]:,}", f"{r[6]:,}", f"{r[7]:,}&nbsp;"] for r in ROWS]),
    "market_value_label": table(HEAD[:-1] + ["Market Value"], standard()),
    "section_rows_and_subtotals": (
        "<html><body><p>(in thousands)</p><table><tr>" + "".join(f"<td>{x}</td>" for x in HEAD) + "</tr>"
        "<tr><td colspan='8'><b>First Lien Senior Secured Debt</b></td></tr>"
        + "".join("<tr>" + "".join(f"<td>{x}</td>" for x in base(r)) + "</tr>" for r in ROWS)
        + "<tr><td>Total First Lien Senior Secured Debt</td><td></td><td></td><td></td><td></td><td></td><td>18,260</td><td>17,195</td></tr>"
        "</table></body></html>"),
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_layout_variant(name):
    hs = extract_holdings(VARIANTS[name])
    assert len(hs) == 3, f"{name}: got {len(hs)} rows: {[h.raw for h in hs]}"
    for h, r in zip(hs, ROWS):
        assert h.company == r[0], name
        assert h.industry == r[1], name
        assert h.instrument == r[2], name
        assert h.spread_bps == float(r[3].split("+")[1].strip(" %")) * 100, name
        assert h.maturity == r[4], name
        assert (h.principal, h.cost, h.fair_value) == (r[5], r[6], r[7]), name
        assert h.units == "thousands", name
    beta = hs[2]
    assert beta.mark == pytest.approx(6800 / 7920, abs=1e-4) and "marked at 86% of cost" in beta.notes


def test_millions_and_negative_values():
    html = table(["Company", "Investment", "Cost", "Fair Value"],
                 [["Gamma Co.", "Term Loan", "12.5", "(0.4)"], ["Delta Inc.", "Warrants", "—", "1.25"]],
                 title="(in millions)")
    hs = extract_holdings(html)
    assert [(h.cost, h.fair_value, h.units) for h in hs] == [(12.5, -0.4, "millions"), (0.0, 1.25, "millions")]
    assert hs[1].mark is None  # no cost basis, no mark


def test_non_schedule_tables_are_ignored():
    html = "<html><body><table><tr><td>Revenue</td><td>2026</td></tr><tr><td>Interest income</td><td>1,234</td></tr></table></body></html>"
    assert extract_holdings(html) == []
