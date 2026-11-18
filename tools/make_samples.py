"""Generate synthetic, deliberately messy BDC filings for offline testing.

EVERYTHING HERE IS FICTIONAL: the funds, the portfolio companies and every number.

Three filings, two different layouts, so the extractor can't overfit one style:
  lakeshore_2026q2.html   Lakeshore Example Capital Corp, June 30, 2026     (layout A)
  lakeshore_2026q3.html   Lakeshore Example Capital Corp, Sept. 30, 2026    (layout A)
  wacker_2026q3.html      Wacker Street Credit Fund, Sept. 30, 2026         (layout B)

Quirks covered: "$" in separate cells, colspans, footnote superscripts, section
header rows, company names only on the first row, subtotals, a table split by
a page break, <th> headers, a different column order and labels, dashes for
zero, month/year-only maturities, non-accrual and PIK footnotes, and inline
XBRL (ix:nonFraction) tags with typed dimension members, as real filings use.

Writes samples/*.html, evals/gold/*.csv and samples/manifest.json.
"""
import csv
import json
import os
from copy import deepcopy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FL, SL, EQ = "First Lien Senior Secured Debt", "Second Lien Senior Secured Debt", "Equity Investments"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def H(sec, co, ind, inst, rate, spread, mat, prin, cost, fv, pct, flags=()):
    return dict(investment_type=sec, company=co, industry=ind, instrument=inst, interest_rate=rate,
                spread=spread, maturity=mat, principal=prin, cost=cost, fair_value=fv,
                pct_net_assets=pct, flags=list(flags))


LAKESHORE_Q3 = [
    H(FL, "Prairie Analytics, LLC", "Software", "Term Loan", "9.85%", "S + 5.50%", "2029-03-15", 24500, 24112, 24255, 3.1),
    H(FL, "Prairie Analytics, LLC", "Software", "Revolver", "9.85%", "S + 5.50%", "2029-03-15", 1200, 1180, 1188, 0.2),
    H(FL, "Calumet Health Partners, Inc.", "Health Care Services", "Term Loan", "10.40%", "S + 6.00%", "2028-11-30", 18750, 18501, 17813, 2.3),
    H(FL, "Lakefront Logistics Holdings", "Transportation", "Term Loan", "9.60%", "S + 5.25%", "2030-06-01", 32000, 31544, 31680, 4.0),
    H(FL, "Lakefront Logistics Holdings", "Transportation", "Delayed Draw Term Loan", "9.60%", "S + 5.25%", "2030-06-01", 4000, 3940, 3960, 0.5),
    H(FL, "Wabash Dental Group", "Health Care Services", "Term Loan", "11.10% (incl. 2.00% PIK)", "S + 6.75%", "2027-09-30", 12300, 12245, 10455, 1.3, ["pik"]),
    H(FL, "Kinzie Cloud Systems, Inc.", "Software", "Term Loan", "9.35%", "S + 5.00%", "2031-01-31", 41000, 40385, 41205, 5.2),
    H(FL, "Fulton Market Foods Co.", "Food Products", "Term Loan", "10.15%", "S + 5.75%", "2029-08-15", 15600, 15420, 15210, 1.9),
    H(FL, "Pilsen Industrial Supply", "Distribution", "Term Loan", "9.95%", "S + 5.50%", "2028-04-30", 21000, 20790, 20895, 2.6),
    H(FL, "Bronzeville Learning, LLC", "Education", "Term Loan", "10.65%", "S + 6.25%", "2029-12-15", 9800, 9702, 9506, 1.2),
    H(FL, "Hyde Park Insurance Services", "Insurance", "Term Loan", "9.70%", "S + 5.25%", "2030-02-28", 27500, 27088, 27363, 3.5),
    H(FL, "Ravenswood Security Software", "Software", "Term Loan", "10.20%", "S + 5.75%", "2029-05-31", 19000, 18715, 18810, 2.4),
    H(SL, "Calumet Health Partners, Inc.", "Health Care Services", "Second Lien Term Loan", "13.40%", "S + 9.00%", "2029-11-30", 8000, 7880, 7040, 0.9),
    H(SL, "Midway Aerostructures Corp.", "Aerospace Components", "Second Lien Term Loan", "12.85%", "S + 8.50%", "2030-07-15", 11000, 10835, 10945, 1.4),
    H(SL, "Kinzie Cloud Systems, Inc.", "Software", "Second Lien Term Loan", "12.60%", "S + 8.25%", "2031-07-31", 6500, 6402, 6532, 0.8),
    H(EQ, "Prairie Analytics, LLC", "Software", "Class A Units", "", "", "", 1500, 1500, 2310, 0.3),
    H(EQ, "Wabash Dental Group", "Health Care Services", "Preferred Equity", "", "", "", 2000, 2000, 640, 0.1),
    H(EQ, "Fulton Market Foods Co.", "Food Products", "Common Stock", "", "", "", 350, 350, 525, 0.1),
]


def lakeshore_q2():
    """The prior quarter: Bronzeville not yet funded, one position since exited,
    and Wabash / Calumet not yet marked down."""
    rows = [deepcopy(h) for h in LAKESHORE_Q3 if h["company"] != "Bronzeville Learning, LLC"]
    for h in rows:
        if h["company"] == "Wabash Dental Group" and h["instrument"] == "Term Loan":
            h.update(fair_value=11760, cost=12260, pct_net_assets=1.5, flags=[])
            h["interest_rate"] = "11.10%"
        if h["company"] == "Wabash Dental Group" and h["instrument"] == "Preferred Equity":
            h.update(fair_value=1450, pct_net_assets=0.2)
        if h["company"] == "Calumet Health Partners, Inc." and h["investment_type"] == SL:
            h.update(fair_value=7720, pct_net_assets=1.0)
        if h["company"] == "Kinzie Cloud Systems, Inc." and h["instrument"] == "Term Loan":
            h.update(principal=35000, cost=34460, fair_value=35070, pct_net_assets=4.5)
    rows.insert(9, H(FL, "Cicero Packaging Co.", "Containers & Packaging", "Term Loan", "10.05%", "S + 5.75%", "2028-10-31",
                     14000, 13860, 13930, 1.8))
    return rows


WACKER_Q3 = [
    H(FL, "Prairie Analytics LLC", "Software", "First lien term loan", "9.90%", "SOFR + 550", "2029-03-15", 18000, 17730, 17910, 4.4),
    H(FL, "Wabash Dental Group", "Healthcare Providers", "First lien term loan", "11.10%", "SOFR + 675", "2027-09-30", 9000, 8960, 7020, 1.7, ["pik"]),
    H(FL, "Kinzie Cloud Systems, Inc.", "Software", "First lien term loan", "9.35%", "SOFR + 500", "2031-01-31", 22000, 21670, 22175, 5.4),
    H(FL, "Lakefront Logistics Holdings, LLC", "Transportation & Logistics", "First lien term loan", "9.70%", "SOFR + 525", "2030-06-01", 15000, 14790, 14850, 3.6),
    H(FL, "Logan Square Media, Inc.", "Media", "First lien term loan", "12.40%", "SOFR + 800", "2027-03-31", 10500, 10290, 4725, 1.2, ["non_accrual"]),
    H(FL, "Evanston Robotics Corp.", "Industrial Machinery", "First lien term loan", "10.25%", "SOFR + 575", "2030-11-30", 16500, 16253, 16335, 4.0),
    H(FL, "Oak Lawn Medical Billing, LLC", "Health Care Technology", "Unitranche term loan", "12.00% (incl. 3.00% PIK)", "SOFR + 700", "2029-06-30", 13000, 12805, 12350, 3.0, ["pik"]),
    H(FL, "Andersonville Pet Health", "Veterinary Services", "First lien term loan", "10.60%", "SOFR + 600", "2029-09-30", 11200, 11032, 11088, 2.7),
    H(EQ, "Logan Square Media, Inc.", "Media", "Common equity", "", "", "", 750, 750, 0, 0.0, ["non_income"]),
    H(EQ, "Evanston Robotics Corp.", "Industrial Machinery", "Warrants", "", "", "", 120, 0, 410, 0.1, ["non_income"]),
]

FILINGS = [
    dict(slug="lakeshore_2026q2", fund="Lakeshore Example Capital Corp", period="2026-06-30", layout="A", rows=lakeshore_q2()),
    dict(slug="lakeshore_2026q3", fund="Lakeshore Example Capital Corp", period="2026-09-30", layout="A", rows=LAKESHORE_Q3),
    dict(slug="wacker_2026q3", fund="Wacker Street Credit Fund", period="2026-09-30", layout="B", rows=WACKER_Q3),
]

GOLD_FIELDS = ["investment_type", "company", "industry", "instrument", "interest_rate", "spread", "maturity",
               "principal", "cost", "fair_value", "pct_net_assets", "non_accrual", "pik"]


def date_text(iso, style):
    if not iso:
        return "", ""
    y, m, d = iso.split("-")
    if style == 0:
        return f"{int(m)}/{int(d)}/{y}", iso
    if style == 1:
        return f"{MONTHS[int(m) - 1][:3]}. {int(d)}, {y}", iso
    return f"{m}/{y}", f"{y}-{m}"  # month/year only: the gold keeps only what the document shows


def money(v):
    return "—" if v == 0 else f"{v:,}"


class XBRL:
    """Collects contexts and writes inline-XBRL tags around numbers."""

    def __init__(self, period):
        self.period, self.contexts, self.n = period, [], 0

    def ctx(self, member=None):
        self.n += 1
        cid = f"c-{self.n}"
        seg = ""
        if member:
            seg = ('<xbrli:segment><xbrldi:typedMember dimension="us-gaap:InvestmentIdentifierAxis">'
                   f'<us-gaap:InvestmentIdentifierAxis.domain>{member}</us-gaap:InvestmentIdentifierAxis.domain>'
                   '</xbrldi:typedMember></xbrli:segment>')
        self.contexts.append(f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000000000</xbrli:identifier>'
                             f'{seg}</xbrli:entity><xbrli:period><xbrli:instant>{self.period}</xbrli:instant></xbrli:period></xbrli:context>')
        return cid

    @staticmethod
    def tag(name, cid, value_text, scale=3):
        if value_text in ("", "—"):
            return value_text
        return (f'<ix:nonFraction name="us-gaap:{name}" contextRef="{cid}" unitRef="usd" decimals="-3" '
                f'scale="{scale}" format="ixt:num-dot-decimal">{value_text}</ix:nonFraction>')

    def header(self):
        return ('<div style="display:none"><ix:header><ix:resources>' + "".join(self.contexts) +
                '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit></ix:resources></ix:header></div>')


FOOTNOTES = {
    "A": ["(1) All investments are non-controlled, non-affiliated.", "(2) Fictional sample.",
          "(3) Rates as of period end; \"S\" is SOFR.", "(4) Level 3 fair value.",
          "(5) A portion of interest is paid-in-kind (PIK)."],
    "B": ["(1) Fictional sample. (2) Valued using significant unobservable inputs.",
          "(6) Interest includes a payment-in-kind (PIK) component.",
          "(7) Loan was on non-accrual status as of the reporting date.",
          "(8) Non-income producing investment."],
}
FLAG_MARK = {"A": {"pik": "(5)"}, "B": {"pik": "(6)", "non_accrual": "(7)", "non_income": "(8)"}}


def render_A(f, x):
    head = """
<tr>
 <td style="font-weight:bold">Portfolio Company<sup>(1)(2)</sup></td><td></td>
 <td style="font-weight:bold">Industry</td><td></td>
 <td style="font-weight:bold">Investment</td><td></td>
 <td style="font-weight:bold">Interest<br/>Rate<sup>(3)</sup></td><td></td>
 <td style="font-weight:bold">Reference Rate<br/>and Spread</td><td></td>
 <td style="font-weight:bold">Maturity<br/>Date</td><td></td>
 <td colspan="3" style="font-weight:bold">Principal /<br/>Shares</td><td></td>
 <td colspan="3" style="font-weight:bold">Amortized<br/>Cost</td><td></td>
 <td colspan="3" style="font-weight:bold">Fair<br/>Value<sup>(4)</sup></td><td></td>
 <td colspan="2" style="font-weight:bold">% of Net<br/>Assets</td>
</tr>"""
    title = (f'<p style="text-align:center;font-weight:bold">{f["fund"].upper()}<br/>CONSOLIDATED SCHEDULE OF INVESTMENTS (UNAUDITED)'
             f'<br/>{long_date(f["period"])}<br/>(dollars in thousands)</p>')
    parts, gold = [title, "<table>", head], []
    split_at = 9
    prev_sec = prev_co = None
    totals = {}

    def section(text):
        return f'\n<tr><td colspan="4" style="font-weight:bold;font-style:italic">{text}</td></tr>'

    def total(label, c, fv, p):
        cid = x.ctx()
        return f"""
<tr><td colspan="4" style="font-weight:bold">{label}</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td>
 <td></td><td></td><td></td><td></td><td>$</td><td style="text-align:right">{x.tag('InvestmentOwnedAtCost', cid, money(c))}</td><td></td><td></td>
 <td>$</td><td style="text-align:right">{x.tag('InvestmentOwnedAtFairValue', cid, money(fv))}</td><td></td><td></td>
 <td style="text-align:right">{p:.1f}</td><td>%</td></tr>"""

    for i, h in enumerate(f["rows"]):
        if i == split_at:
            parts.append(f"</table>\n<p style='page-break-before:always'>{f['fund'].upper()}<br/>CONSOLIDATED SCHEDULE OF INVESTMENTS (UNAUDITED) (continued)<br/>(dollars in thousands)</p>\n<table>")
            parts += [head, section(h["investment_type"] + " (continued)")]
            prev_sec, prev_co = h["investment_type"], None
        if h["investment_type"] != prev_sec:
            if prev_sec:
                parts.append(total(f"Total {prev_sec}", *totals[prev_sec]))
            parts.append(section(h["investment_type"]))
            prev_sec, prev_co = h["investment_type"], None
        show = h["company"] != prev_co
        marks = "".join(FLAG_MARK["A"][fl] for fl in h["flags"] if fl in FLAG_MARK["A"])
        co_cell = (h["company"] + (f"<sup>{marks}</sup>" if marks else "")) if show else (f"<sup>{marks}</sup>" if marks else "")
        dtext, dgold = date_text(h["maturity"], i % 3)
        cid = x.ctx(f'{h["company"]}, {h["instrument"]}')
        d = "$" if show else ""
        parts.append(f"""
<tr><td>{co_cell}</td><td></td><td>{h['industry'] if show else ''}</td><td></td><td>{h['instrument']}</td><td></td>
 <td>{h['interest_rate']}</td><td></td><td>{h['spread']}</td><td></td><td>{dtext}</td><td></td>
 <td></td><td style="text-align:right">{x.tag('InvestmentOwnedBalancePrincipalAmount', cid, money(h['principal']))}</td><td></td><td></td>
 <td>{d}</td><td style="text-align:right">{x.tag('InvestmentOwnedAtCost', cid, money(h['cost']))}</td><td></td><td></td>
 <td>{d}</td><td style="text-align:right">{x.tag('InvestmentOwnedAtFairValue', cid, money(h['fair_value']))}</td><td></td><td></td>
 <td style="text-align:right">{h['pct_net_assets']:.1f}</td><td>%</td></tr>""")
        prev_co = h["company"]
        c, fv, p = totals.get(prev_sec, (0, 0, 0.0))
        totals[prev_sec] = (c + h["cost"], fv + h["fair_value"], round(p + h["pct_net_assets"], 1))
        gold.append({**h, "maturity": dgold})
    parts.append(total(f"Total {prev_sec}", *totals[prev_sec]))
    rows = f["rows"]
    parts.append(total("Total Investments", sum(r["cost"] for r in rows), sum(r["fair_value"] for r in rows),
                       round(sum(r["pct_net_assets"] for r in rows), 1)))
    parts.append("</table>")
    return parts, gold


def render_B(f, x):
    """Different filer: <th> headers, another column order and labels, no % of net
    assets column, spreads in basis points, industry in its own column after the
    instrument, footnote markers after the instrument instead of the company."""
    head = """<thead><tr>
 <th>Issuer</th><th>Asset Type</th><th>Sector</th><th>Index / Spread</th><th>Coupon</th><th>Maturity</th>
 <th colspan="2">Par Amount / Units</th><th colspan="2">Amortized Cost</th><th colspan="2">Fair Value</th><th>% of Net Assets</th>
</tr></thead>"""
    parts = [f'<p><b>{f["fund"]}</b> — Schedule of Investments as of {long_date(f["period"])} (unaudited; in thousands of U.S. dollars)</p>',
             "<table>", head, "<tbody>"]
    gold, prev_sec = [], None
    for i, h in enumerate(f["rows"]):
        if h["investment_type"] != prev_sec:
            parts.append(f'<tr><td colspan="13"><b>{h["investment_type"]}</b></td></tr>')
            prev_sec = h["investment_type"]
        marks = "".join(FLAG_MARK["B"][fl] for fl in h["flags"] if fl in FLAG_MARK["B"])
        dtext, dgold = date_text(h["maturity"], (i + 1) % 3)
        cid = x.ctx(f'{h["company"]} - {h["instrument"]}')
        parts.append(f"""<tr><td>{h['company']}</td><td>{h['instrument']}{f' <sup>{marks}</sup>' if marks else ''}</td><td>{h['industry']}</td>
 <td>{h['spread']}</td><td>{h['interest_rate']}</td><td>{dtext}</td>
 <td>$</td><td>{x.tag('InvestmentOwnedBalancePrincipalAmount', cid, money(h['principal']))}</td>
 <td>$</td><td>{x.tag('InvestmentOwnedAtCost', cid, money(h['cost']))}</td>
 <td>$</td><td>{x.tag('InvestmentOwnedAtFairValue', cid, money(h['fair_value']))}</td><td>{h['pct_net_assets']:.1f}%</td></tr>""")
        gold.append({**h, "maturity": dgold})
    cid = x.ctx()
    rows = f["rows"]
    parts.append(f"""<tr><td colspan="6"><b>Total investments</b></td><td>$</td><td></td>
 <td>$</td><td>{x.tag('InvestmentOwnedAtCost', cid, money(sum(r['cost'] for r in rows)))}</td>
 <td>$</td><td>{x.tag('InvestmentOwnedAtFairValue', cid, money(sum(r['fair_value'] for r in rows)))}</td>
 <td>{round(sum(r['pct_net_assets'] for r in rows), 1):.1f}%</td></tr></tbody></table>""")
    return parts, gold


def long_date(iso):
    y, m, d = iso.split("-")
    return f"{MONTHS[int(m) - 1]} {int(d)}, {y}"


def build():
    os.makedirs(os.path.join(ROOT, "samples"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "evals", "gold"), exist_ok=True)
    manifest = []
    for f in FILINGS:
        x = XBRL(f["period"])
        parts, gold = (render_A if f["layout"] == "A" else render_B)(f, x)
        html = ["<html><head><title>", f["fund"], " - Form 10-Q (FICTIONAL SAMPLE)</title></head><body>", x.header(),
                "<p><b>THIS IS A SYNTHETIC SAMPLE FILING FOR TESTING. THE FUND, COMPANIES AND FIGURES ARE FICTIONAL.</b></p>"]
        html += parts
        html.append("<p>" + "<br/>".join(FOOTNOTES[f["layout"]]) + "</p></body></html>")
        path = os.path.join("samples", f["slug"] + ".html")
        with open(os.path.join(ROOT, path), "w") as fh:
            fh.write("".join(html))
        gpath = os.path.join("evals", "gold", f["slug"] + ".csv")
        with open(os.path.join(ROOT, gpath), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(GOLD_FIELDS)
            for g in gold:
                w.writerow([g[k] for k in GOLD_FIELDS[:-2]] + [int("non_accrual" in g["flags"]), int("pik" in g["flags"])])
        manifest.append({"slug": f["slug"], "fund": f["fund"], "period": f["period"], "form": "10-Q",
                         "path": path, "gold": gpath, "layout": f["layout"], "rows": len(gold)})
    with open(os.path.join(ROOT, "samples", "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


if __name__ == "__main__":
    for m in build():
        print(f"{m['path']}: {m['rows']} holdings (layout {m['layout']})")
