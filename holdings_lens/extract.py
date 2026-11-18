"""Extract holdings from the Schedule of Investments tables in a BDC filing (HTML).

Real EDGAR tables are messy: "$" signs sit in their own cells, labels span
several columns, company names appear only on the first row of each company,
footnote markers are glued to names, and one schedule is split across many
<table> elements. This module turns that into one clean row per holding and
keeps a pointer (table, row, raw text, XBRL context) back to the source, so
every number the agent cites can be checked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from typing import Optional

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from . import xbrl

# Header keywords per field, most specific first. Order matters: "reference
# rate and spread" must win over "rate", "amortized cost" over "cost".
FIELD_KEYWORDS: list[tuple[str, list[str]]] = [
    ("pct_net_assets", ["% of net", "percentage of net", "net assets", "% of total"]),
    ("fair_value", ["fair value", "market value"]),
    ("cost", ["amortized cost", "cost"]),
    ("principal", ["principal", "par amount", "par value", "shares", "units", "par", "face amount"]),
    ("maturity", ["maturity", "expiration"]),
    ("spread", ["spread", "reference rate", "index"]),
    ("interest_rate", ["interest rate", "coupon", "rate", "yield"]),
    ("industry", ["industry", "sector"]),
    ("company", ["portfolio company", "company", "issuer", "borrower", "name of"]),
    ("instrument", ["type of investment", "investment", "security", "instrument", "asset", "facility", "tranche"]),
]
NUMERIC = {"principal", "cost", "fair_value", "pct_net_assets"}
HEADER_HINTS = ["fair value", "cost", "principal", "maturity", "company", "industry", "issuer", "sector",
                "investment", "interest", "net assets", "shares", "par", "spread", "coupon"]
SECTION_TYPE_RE = re.compile(
    r"lien|debt|equity|preferred|warrant|subordinated|unitranche|senior secured|structured|clo\b|"
    r"common stock|investments?$", re.I)
MONEY_RE = re.compile(r"\d{1,3}(,\d{3})+|\d+\.\d+")
FOOTNOTE_RE = re.compile(r"(\s*(\(\d{1,2}\)|\([a-z]{1,2}\)|[+*#^†‡§]+))+\s*$")
FOOTNOTE_ONLY = re.compile(r"^(\(\d{1,2}\)|\([a-z]{1,2}\)|[+*#^†‡§]+)+$")
SKIP_CELL = {"", "$", "%", ")", "(", "—", "–"}
DASHES = {"—", "–", "-", "--"}
UNIT_MULT = {"thousands": 1_000, "millions": 1_000_000, "dollars": 1}

BASE_RATES = {"S": "SOFR", "SOFR": "SOFR", "SF": "SOFR", "CA": "CORRA", "A": "BBSY", "L": "LIBOR", "LIBOR": "LIBOR", "P": "Prime", "PRIME": "Prime",
              "E": "EURIBOR", "EURIBOR": "EURIBOR", "SONIA": "SONIA", "SN": "SONIA", "C": "CORRA", "CORRA": "CORRA"}
SPREAD_RE = re.compile(r"\b(SOFR|LIBOR|PRIME|EURIBOR|SONIA|CORRA|SF|SN|CA|S|L|P|E|C|A)\s*\+\s*([\d.]+)\s*(%|bps)?", re.I)


@dataclass
class Holding:
    company: str
    industry: Optional[str]
    investment_type: Optional[str]
    instrument: Optional[str]
    interest_rate: Optional[str]
    spread: Optional[str]
    maturity: Optional[str]
    principal: Optional[float]
    cost: Optional[float]
    fair_value: Optional[float]
    pct_net_assets: Optional[float]
    units: str
    table_idx: int
    row_idx: int
    raw: str
    notes: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    # derived
    base_rate: Optional[str] = None
    spread_bps: Optional[float] = None
    rate_pct: Optional[float] = None
    mark: Optional[float] = None          # fair value / cost
    lien: Optional[str] = None
    non_accrual: bool = False
    pik: bool = False
    non_income: bool = False
    company_key: str = ""
    # XBRL cross-check
    xbrl_context: Optional[str] = None
    xbrl_member: Optional[str] = None
    xbrl_fair_value: Optional[float] = None   # whole dollars, from the filing's own tag
    xbrl_concept: Optional[str] = None        # should be InvestmentOwnedAtFairValue; anything else = wrong column
    xbrl_period: Optional[str] = None         # the date of the schedule this row belongs to
    markers: list[str] = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["notes"] = "; ".join(self.notes)
        d["footnotes"] = "; ".join(self.footnotes)
        d.pop("markers", None)
        return d


# ------------------------------------------------------------------ cleaning
def clean_text(s: str) -> str:
    s = (s or "").replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", s).strip()


def strip_footnotes(s: str) -> str:
    return FOOTNOTE_RE.sub("", s or "").strip()


def parse_number(s: str) -> Optional[float]:
    """'1,234' -> 1234.0, '(1,234)' -> -1234.0, '—' -> 0.0, '' -> None."""
    s = strip_footnotes(clean_text(s)).replace("$", "").replace("%", "").strip()
    if not s:
        return None
    if s in DASHES:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_maturity(s: str) -> Optional[str]:
    """Return ISO date, or YYYY-MM when only month and year are shown."""
    s = strip_footnotes(clean_text(s)).replace("Sept.", "Sep.")
    if not s or s.upper() in {"N/A", "NA"} or s in DASHES:
        return None
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    if re.fullmatch(r"[A-Za-z]{3,9}\.?\s+\d{4}", s):  # "March 2029"
        return dateparser.parse(s, default=dateparser.parse("2000-01-01")).strftime("%Y-%m")
    try:
        return dateparser.parse(s).date().isoformat()
    except (ValueError, OverflowError):
        return None


def parse_spread(spread: Optional[str], rate: Optional[str] = None) -> tuple[Optional[str], Optional[float]]:
    """'S + 5.50%' -> ('SOFR', 550); 'SOFR + 550' -> ('SOFR', 550); 'L+475 bps' -> ('LIBOR', 475)."""
    for text in (spread, rate):
        m = SPREAD_RE.search(text or "")
        if m:
            base = BASE_RATES.get(m.group(1).upper())
            val = float(m.group(2))
            unit = (m.group(3) or "").lower()
            bps = val if unit == "bps" or (unit != "%" and val > 25) else val * 100
            return base, round(bps, 1)
    return None, None


def parse_rate(rate: Optional[str]) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", rate or "")
    return float(m.group(1)) if m else None


def company_key(name: str) -> str:
    """Normalize names so the same borrower matches across quarters and funds."""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[.,'’()]", " ", s)
    s = re.sub(r"\b(llc|l l c|inc|incorporated|corp|corporation|co|company|ltd|limited|lp|l p|plc|holdings?|group|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_SUFFIX = r"(?:L\.?\s?L\.?\s?C\.?|Inc\.?|L\.?\s?P\.?|Ltd\.?|Corp\.?|Corporation|Company|LLP|S\.?A\.?|B\.?V\.?|GmbH|SCSp|plc|Limited|Co\.?|Holdco|Topco|Midco|Bidco|AB|AS|SAS|S\.?\s?a\.?\s?r\.?\s?l\.?|ULC|N\.?V\.?)"
_SUFFIX_COMMA = re.compile(rf",\s*(?={_SUFFIX}(?:\W|$))")
_ENTITY_END = re.compile(rf"\b{_SUFFIX}\s*(?:\([^)]*\))?$", re.I)


def split_entities(name: str) -> list[str]:
    """'Pluralsight, LLC and Pluralsight Holdings, LLC and Paradigmatic Holdco LLC' -> its three entities.
    Lenders often list a borrower with its holding companies. Names are only split when every part
    looks like a legal entity, so 'Johnson and Johnson' or 'Food and Drug Co.' stay whole."""
    protected = _SUFFIX_COMMA.sub(" ", name or "")
    parts = [p.strip(" ,") for p in re.split(r",?\s+and\s+|,\s+(?=[A-Z0-9])", protected) if p.strip(" ,")]
    if len(parts) >= 2 and all(_ENTITY_END.search(p) for p in parts):
        return parts
    return [name]


def borrower_key(name: str) -> str:
    """Match key for a borrower: the first listed entity (the operating company), normalized."""
    return company_key(split_entities(name)[0])


CASH_NAME = re.compile(r"money market|liquidity funds?\b|treasury (securities )?portfolio|government (obligations|portfolio) fund", re.I)


def is_cash_equivalent(h) -> bool:
    """Money-market and similar funds listed with the schedule. Filers exclude them from 'Total
    investments', so analytics must too. XBRL says so directly; the name is a fallback without XBRL."""
    if h.xbrl_concept and h.xbrl_concept != xbrl.FAIR_VALUE and h.xbrl_concept in xbrl.FAIR_VALUE_LIKE:
        return True
    return bool(CASH_NAME.search(h.company or "")) and not h.spread_bps and not h.maturity


def lien_of(investment_type: Optional[str], instrument: Optional[str]) -> str:
    t = f"{investment_type or ''} {instrument or ''}".lower()
    if "unitranche" in t or "one stop" in t or "one-stop" in t:
        return "unitranche"
    if "first lien" in t or "1st lien" in t or "senior secured" in t and "second" not in t:
        return "first_lien"
    if "second lien" in t or "2nd lien" in t:
        return "second_lien"
    if "subordinated" in t or "mezzanine" in t:
        return "subordinated"
    if any(k in t for k in ("equity", "stock", "units", "warrant", "preferred")):
        return "equity"
    return "other"


# ------------------------------------------------------------------ table walking
def row_cells(tr) -> tuple[list[tuple[str, int, object]], list[str]]:
    """Cells as (text, colspan, element) plus the footnote markers found in <sup> tags."""
    out, markers = [], []
    for td in tr.find_all(["td", "th"], recursive=False):
        for sup in td.find_all("sup"):
            txt = clean_text(sup.get_text())
            markers += re.findall(r"\d{1,2}", txt)
            sup.replace_with(" " + txt)
        for br in td.find_all("br"):
            br.replace_with(" ")
        try:
            span = int(td.get("colspan", 1))
        except ValueError:
            span = 1
        out.append((clean_text(td.get_text(" ")), max(1, min(span, 50)), td))
    return out, markers


def row_width(tr) -> int:
    """Number of grid columns in a row, without modifying it."""
    w = 0
    for td in tr.find_all(["td", "th"], recursive=False):
        try:
            w += max(1, min(int(td.get("colspan", 1)), 50))
        except ValueError:
            w += 1
    return w


def expand(cells, fill_span: bool) -> tuple[list[str], list]:
    grid, els = [], []
    for text, span, el in cells:
        grid.append(text)
        els.append(el)
        grid.extend([text if fill_span else ""] * (span - 1))
        els.extend([el] * (span - 1))
    return grid, els


def is_header_row(texts: list[str]) -> bool:
    joined = " ".join(t for t in texts if t).lower()
    if not joined:
        return False
    hits = sum(1 for k in HEADER_HINTS if k in joined)
    return hits >= 2 and not MONEY_RE.search(re.sub(r"\(\d+\)", "", joined))


# Tables that carry fair value and cost columns but are NOT the schedule of investments:
# affiliate roll-forwards (purchases, sales, income during the period, ending fair value; these repeat
# holdings already in the schedule) and derivative tables (swaps, forwards on the fund's own debt).
ACTIVITY_WORDS = ["purchases", "redemptions", "sales (cost)", "interest income", "dividend income",
                  "realized gain", "unrealized gain", "transfers in", "transfers out", "fee income", "other income"]
DERIVATIVE_WORDS = ["counterparty", "notional", "swap", "hedged item", "settlement date", "company receives", "company pays"]


def not_a_schedule(header_grid: list[str]) -> bool:
    text = " ".join(header_grid).lower()
    activity = sum(w in text for w in ACTIVITY_WORDS)
    return activity >= 2 or any(w in text for w in DERIVATIVE_WORDS)


def map_columns(header_grid: list[str]) -> dict[str, list[int]]:
    cols: dict[str, list[int]] = {}
    for pos, label in enumerate(header_grid):
        lab = strip_footnotes(re.sub(r"\(\d+\)", " ", label)).lower()
        if not lab:
            continue
        for fname, kws in FIELD_KEYWORDS:
            if any((re.search(rf"\b{re.escape(k)}\b", lab) if len(k) <= 4 else k in lab) for k in kws):
                cols.setdefault(fname, []).append(pos)
                break
    return cols


def value_in_span(grid, positions, width, look_right=False) -> tuple[str, Optional[int]]:
    """First meaningful text in a column's span (numbers may sit one or two cells
    right of their label because of "$" cells). Returns (text, position)."""
    if not positions:
        return "", None
    lo, hi = min(positions), max(positions)
    own = range(lo, hi + 1)
    for p in own:
        if p < len(grid) and grid[p] not in SKIP_CELL:
            return grid[p], p
    for p in own:  # a dash inside the column's own span means zero
        if p < len(grid) and grid[p] in DASHES:
            return grid[p], p
    if look_right:  # only then peek right, and stop at the first dash or value
        for p in range(hi + 1, min(hi + 3, width)):
            if p < len(grid) and grid[p] in DASHES:
                return grid[p], p
            if p < len(grid) and grid[p] not in SKIP_CELL:
                return grid[p], p
    return "", None


def text_in_span(grid, positions) -> str:
    """All meaningful text in a column's span, joined: 'SF +' | '4.50 %' | '(i)' -> 'SF + 4.50 %'."""
    if not positions:
        return ""
    parts = [grid[p] for p in range(min(positions), max(positions) + 1)
             if p < len(grid) and grid[p] not in SKIP_CELL and not FOOTNOTE_ONLY.match(grid[p])]
    return " ".join(dict.fromkeys(parts))


def table_units(table, fv_scale: Optional[int], doc_units: str) -> str:
    """Units for one table: the caption just above it, then the XBRL scale, then the document."""
    seen = 0
    for txt in table.find_all_previous(string=True, limit=400):
        t = clean_text(txt)
        if not t:
            continue
        seen += 1
        low = t.lower()
        if "in thousands" in low:
            return "thousands"
        if "in millions" in low:
            return "millions"
        if seen > 25:
            break
    if fv_scale == 3:
        return "thousands"
    if fv_scale == 6:
        return "millions"
    return doc_units


def _header_layouts(tables) -> dict:
    out = {}
    for i, t in enumerate(tables):
        hg = []
        for tr in t.find_all("tr")[:12]:
            g = [x for txt, sp, _ in row_cells_peek(tr) for x in [txt] * sp]
            if is_header_row(g):
                hg = [((hg[j] if j < len(hg) else "") + " " + (g[j] if j < len(g) else "")).strip()
                      for j in range(max(len(hg), len(g)))]
            elif hg:
                break
        if not hg:
            continue
        cols = map_columns(hg)
        if "fair_value" in cols and ("cost" in cols or "principal" in cols) and \
                any(k in cols for k in ("maturity", "spread", "interest_rate", "company")):
            if "company" not in cols:
                first = min(p for ps in cols.values() for p in ps)
                if first > 0:
                    cols["company"] = list(range(0, first))
            out[i] = (cols, len(hg))
    return out


def _layout_fits(rows, active) -> bool:
    """Does a headerless table line up with the carried schedule layout? Check a sample
    of rows: names where the company column is, numbers where fair value is."""
    cols, width = active
    hits = tried = 0
    for tr in rows[:40]:
        grid = [t for t, sp, _ in row_cells_peek(tr) for t in [t] + [""] * (sp - 1)]
        if not any(c not in SKIP_CELL for c in grid):
            continue
        tried += 1
        fv, _ = value_in_span(grid, cols["fair_value"], width, look_right=True)
        co, _ = value_in_span(grid, cols.get("company", []), width)
        if parse_number(fv) is not None and re.search(r"[A-Za-z]", co or ""):
            hits += 1
    return tried > 0 and hits / tried >= 0.3


def row_cells_peek(tr):
    """Like row_cells, but read-only (doesn't consume footnote <sup> tags)."""
    out = []
    for td in tr.find_all(["td", "th"], recursive=False):
        try:
            span = max(1, min(int(td.get("colspan", 1)), 50))
        except ValueError:
            span = 1
        out.append((clean_text(td.get_text(" ")), span, td))
    return out


def detect_units(text: str) -> str:
    t = text.lower()
    if "in thousands" in t:
        return "thousands"
    if "in millions" in t:
        return "millions"
    return "dollars"


def footnote_definitions(soup, elements=None) -> dict[str, str]:
    """Map footnote marker -> text. Filers put definitions in paragraphs, in table cells at the
    bottom of each page, or in ix:footnote elements; take the most descriptive text per marker."""
    cands: dict[str, list[str]] = {}
    pat = re.compile(r"\(\s*(\d{1,2})\s*\)\s*(.+?)(?=\s\(\s*\d{1,2}\s*\)\s|$)")
    for el in (elements if elements is not None else soup.find_all(["p", "div", "td", "ix:footnote", "footnote"])):
        if el.find(["p", "div", "td", "table"]):  # only leaf-ish blocks
            continue
        text = clean_text(el.get_text(" "))
        if not re.match(r"^\(\s*\d{1,2}\s*\)", text):
            continue
        for m in pat.finditer(text):
            body = m.group(2).strip()
            if len(body.split()) >= 4:
                cands.setdefault(m.group(1), []).append(body)
    return {k: max(v, key=lambda t: len(t.split())) for k, v in cands.items()}


def inline_markers(text: str) -> list[str]:
    """Footnote numbers written inline after a name, e.g. 'Acme, Inc.+(8)(9)(12)' -> ['8','9','12']."""
    m = FOOTNOTE_RE.search(text or "")
    return re.findall(r"\(\s*(\d{1,2})\s*\)", m.group(0)) if m else []


def find_schedule_tables(soup) -> list:
    tables = []
    for t in soup.find_all("table"):
        head = clean_text(" ".join(tr.get_text(" ") for tr in t.find_all("tr")[:6])).lower()
        has_value = "fair value" in head or "market value" in head or ("fair" in head and "value" in head)
        if has_value and ("cost" in head or "principal" in head or "par" in head):
            tables.append(t)
    return tables


# ------------------------------------------------------------------ main
def extract_holdings(html: str, period: Optional[str] = None) -> list[Holding]:
    """Extract holdings. Filings often include two schedules (this period and the prior
    year-end); rows are assigned to a schedule date via their XBRL contexts and only
    `period` (default: the latest date found) is returned."""
    import warnings
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(html, "lxml")
    doc_units = detect_units(soup.get_text(" "))
    ctx = xbrl.contexts_full(soup)
    holdings: list[Holding] = []
    table_periods: dict[int, Optional[str]] = {}
    cur_type = cur_industry = None
    company_industry: dict[str, str] = {}

    all_tables = soup.find_all("table")
    layouts = _header_layouts(all_tables)  # t_idx -> (cols, width) for tables with a schedule header
    active = None  # (cols, width) of the last schedule header: carried into headerless continuation tables
    for t_idx, table in enumerate(all_tables):
        rows = table.find_all("tr")
        header_grid: list[str] = []
        body_start = 0
        for i, tr in enumerate(rows[:12]):
            g, _ = expand(row_cells(tr)[0], fill_span=True)
            if is_header_row(g):
                header_grid = [((header_grid[j] if j < len(header_grid) else "") + " " + (g[j] if j < len(g) else "")).strip()
                               for j in range(max(len(header_grid), len(g)))]
                body_start = i + 1
            elif header_grid:
                break
        tbl_width = max((row_width(tr) for tr in rows), default=0)
        if header_grid:
            cols = map_columns(header_grid)
            is_schedule = ("fair_value" in cols and ("cost" in cols or "principal" in cols)
                           and any(k in cols for k in ("maturity", "spread", "interest_rate", "company"))
                           and not not_a_schedule(header_grid))
            if not is_schedule:
                if "fair_value" in cols:
                    active = None  # a different kind of financial table: stop carrying the schedule layout
                continue
            if "company" not in cols:
                # Many filers leave the issuer column's header blank: it's whatever sits left of the first labelled column.
                first = min(p for ps in cols.values() for p in ps)
                if first > 0:
                    cols["company"] = list(range(0, first))
            width = len(header_grid)
            active = (cols, width)
        elif active:
            # Headerless continuation of the schedule on a new page: borrow the nearest schedule
            # header (before or after) whose columns actually line up with this table.
            near = sorted((k for k in layouts if abs(k - t_idx) <= 4), key=lambda k: abs(k - t_idx))
            fit = next((layouts[k] for k in near if abs(tbl_width - layouts[k][1]) <= 8 and _layout_fits(rows, layouts[k])), None)
            if fit is None:
                continue
            cols, width = fit
            body_start = 0
        else:
            continue
        cur_company = None  # never carry a company name across tables
        units = None
        periods_here: list[str] = []

        for r_idx in range(body_start, len(rows)):
            cells, markers = row_cells(rows[r_idx])
            grid, els = expand(cells, fill_span=False)
            nonempty = [c for c in grid if c not in SKIP_CELL]
            if not nonempty:
                continue
            first_text = nonempty[0]
            if re.match(r"(sub)?total\b", first_text, re.I):
                continue
            if not any(re.search(r"[A-Za-z]", c) for c in nonempty):
                continue  # an unlabeled subtotal row: numbers only

            def get(f):
                return value_in_span(grid, cols.get(f, []), width, look_right=f in NUMERIC)

            fv_text, fv_pos = get("fair_value")
            fv = parse_number(fv_text)
            if fv is None:
                label = strip_footnotes(re.sub(r"\s*\(continued\)\s*", " ", first_text, flags=re.I)).strip()
                if len(nonempty) == 1 or not MONEY_RE.search(" ".join(nonempty)):
                    if SECTION_TYPE_RE.search(label):
                        cur_type = label
                    else:
                        cur_industry = label
                    cur_company = None
                continue
            if units is None:
                units = table_units(table, xbrl.scale_in(els[fv_pos]) if fv_pos is not None else None, doc_units)

            notes = []
            company = strip_footnotes(get("company")[0])
            if company:
                cur_company = company
            elif cur_company:
                company = cur_company
                notes.append("company carried from previous row")
            else:
                company = "(unknown)"
                notes.append("no company name found")
            industry = strip_footnotes(get("industry")[0]) or company_industry.get(company) or cur_industry
            if industry:
                company_industry.setdefault(company, industry)
            instrument = strip_footnotes(get("instrument")[0]) or None
            rate = strip_footnotes(text_in_span(grid, cols.get("interest_rate", []))) or None
            spread = strip_footnotes(text_in_span(grid, cols.get("spread", []))) or None

            h = Holding(
                company=company, industry=industry, investment_type=cur_type, instrument=instrument,
                interest_rate=rate, spread=spread, maturity=parse_maturity(get("maturity")[0]),
                principal=parse_number(get("principal")[0]), cost=parse_number(get("cost")[0]), fair_value=fv,
                pct_net_assets=parse_number(get("pct_net_assets")[0]), units=units, table_idx=t_idx, row_idx=r_idx,
                raw=" | ".join(nonempty), notes=notes,
            )
            h.markers = list(dict.fromkeys(markers + inline_markers(get("company")[0]) + inline_markers(get("instrument")[0])))
            h.base_rate, h.spread_bps = parse_spread(spread, rate)
            h.rate_pct = parse_rate(rate)
            h.lien = lien_of(cur_type, instrument)
            h.company_key = borrower_key(company)
            if h.cost is not None and h.cost > 0 and fv >= 0:
                h.mark = round(fv / h.cost, 4)
            if (h.cost or 0) <= 0 and fv <= 0 and h.lien != "equity":
                h.notes.append("unfunded commitment")
            if h.mark is not None and h.mark < 0.9 and h.lien != "equity":
                h.notes.append(f"marked at {h.mark:.0%} of cost")
            if fv_pos is not None:
                f = xbrl.fact_in(els[fv_pos])
                if f:
                    h.xbrl_context, h.xbrl_fair_value, h.xbrl_concept = f
                    h.xbrl_member, h.xbrl_period = ctx.get(h.xbrl_context, (None, None))
                    if h.xbrl_period:
                        periods_here.append(h.xbrl_period)
                    if h.xbrl_concept not in xbrl.FAIR_VALUE_LIKE:
                        h.notes.append(f"XBRL says this column is {h.xbrl_concept}, not fair value")
            if is_cash_equivalent(h):
                h.investment_type, h.lien = "Cash equivalents", "cash"
                h.notes.append("cash equivalent: not part of total investments")
            holdings.append(h)
        table_periods[t_idx] = max(set(periods_here), key=periods_here.count) if periods_here else None

    # Assign every table to a schedule date (untagged continuation tables inherit the previous one),
    # then keep the requested schedule.
    known = [p for p in table_periods.values() if p]
    if known:
        filled, last = {}, None
        for t in sorted(table_periods):
            last = table_periods[t] or last
            filled[t] = last
        first_known = next(table_periods[t] for t in sorted(table_periods) if table_periods[t])
        filled = {t: (p or first_known) for t, p in filled.items()}
        target = period if period in set(filled.values()) else max(known)
        for h in holdings:
            h.xbrl_period = h.xbrl_period or filled[h.table_idx]
        holdings = [h for h in holdings if filled[h.table_idx] == target]
    _apply_footnotes(soup, all_tables, holdings)
    return holdings


def _apply_footnotes(soup, all_tables, holdings):
    """Resolve footnote markers to text and set flags. Footnote numbers are reused across a
    filing, so prefer the definitions printed right after this schedule's last table."""
    if not holdings:
        return
    last = all_tables[max(h.table_idx for h in holdings)]
    near = footnote_definitions(soup, last.find_all_next(["p", "div", "td", "ix:footnote", "footnote"], limit=1500))
    glob = footnote_definitions(soup)
    for h in holdings:
        ftexts = []
        for m in h.markers:
            text = near.get(m) or glob.get(m)
            if text:
                ftexts.append(f"({m}) {text}")
        h.footnotes = ftexts
        blob = " ".join(ftexts) + " " + (h.interest_rate or "")
        h.non_accrual = bool(re.search(r"non-?accrual status|on non-?accrual|non-?accrual\b(?! position)", blob, re.I)) and \
            not re.search(r"reversal of capitalized PIK for non-?accrual", blob, re.I)
        h.pik = bool(re.search(r"\bPIK\b|paid[- ]in[- ]kind|payment[- ]in[- ]kind|capitalized into the outstanding principal", blob, re.I))
        h.non_income = bool(re.search(r"non-?income", blob, re.I))
        if h.non_accrual:
            h.notes.append("non-accrual")
