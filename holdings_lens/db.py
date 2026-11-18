"""SQLite storage for filings and extracted holdings."""
from __future__ import annotations

import json
import os
import sqlite3

DEFAULT_DB = os.environ.get("HOLDINGS_DB", os.path.join("data", "holdings.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY,
    fund TEXT NOT NULL,
    cik TEXT,
    form TEXT,
    period TEXT,
    accession TEXT,
    url TEXT,
    path TEXT UNIQUE,
    reconciliation TEXT          -- JSON from reconcile.reconcile()
);
CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY,
    filing_id INTEGER NOT NULL REFERENCES filings(id),
    company TEXT NOT NULL,
    company_key TEXT,
    industry TEXT,
    investment_type TEXT,
    lien TEXT,
    instrument TEXT,
    interest_rate TEXT,
    rate_pct REAL,
    spread TEXT,
    base_rate TEXT,
    spread_bps REAL,
    maturity TEXT,
    principal REAL,
    cost REAL,
    fair_value REAL,
    mark REAL,
    pct_net_assets REAL,
    non_accrual INTEGER,
    pik INTEGER,
    non_income INTEGER,
    units TEXT,
    table_idx INTEGER,
    row_idx INTEGER,
    raw TEXT,
    footnotes TEXT,
    notes TEXT,
    xbrl_context TEXT,
    xbrl_member TEXT,
    xbrl_fair_value REAL
);
CREATE INDEX IF NOT EXISTS idx_holdings_filing ON holdings(filing_id);
CREATE INDEX IF NOT EXISTS idx_holdings_key ON holdings(company_key);

-- Everything in whole US dollars, with the fund and period attached.
CREATE VIEW IF NOT EXISTS v_holdings AS
SELECT h.*, f.fund, f.period,
       h.fair_value * CASE h.units WHEN 'thousands' THEN 1000 WHEN 'millions' THEN 1000000 ELSE 1 END AS fair_value_usd,
       h.cost * CASE h.units WHEN 'thousands' THEN 1000 WHEN 'millions' THEN 1000000 ELSE 1 END AS cost_usd,
       h.principal * CASE h.units WHEN 'thousands' THEN 1000 WHEN 'millions' THEN 1000000 ELSE 1 END AS principal_usd
FROM holdings h JOIN filings f ON f.id = h.filing_id;

-- The most recent filing per fund.
CREATE VIEW IF NOT EXISTS v_latest AS
SELECT v.* FROM v_holdings v
JOIN (SELECT fund, MAX(period) AS period FROM filings GROUP BY fund) l
  ON l.fund = v.fund AND l.period = v.period;
"""

HOLDING_COLS = ["company", "company_key", "industry", "investment_type", "lien", "instrument", "interest_rate",
                "rate_pct", "spread", "base_rate", "spread_bps", "maturity", "principal", "cost", "fair_value",
                "mark", "pct_net_assets", "non_accrual", "pik", "non_income", "units", "table_idx", "row_idx",
                "raw", "footnotes", "notes", "xbrl_context", "xbrl_member", "xbrl_fair_value"]


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def connect_readonly(path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Read-only connection for the agent: it physically cannot change data."""
    con = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def save_filing(con, fund, path, holdings, cik=None, form=None, period=None, accession=None, url=None,
                reconciliation=None) -> int:
    row = con.execute("SELECT id FROM filings WHERE path = ?", (path,)).fetchone()
    rec = json.dumps(reconciliation) if reconciliation is not None else None
    if row:  # re-extracting the same file replaces its holdings
        fid = row["id"]
        con.execute("DELETE FROM holdings WHERE filing_id = ?", (fid,))
        con.execute("UPDATE filings SET fund=?, cik=?, form=?, period=?, accession=?, url=?, reconciliation=? WHERE id=?",
                    (fund, cik, form, period, accession, url, rec, fid))
    else:
        fid = con.execute(
            "INSERT INTO filings (fund, cik, form, period, accession, url, path, reconciliation) VALUES (?,?,?,?,?,?,?,?)",
            (fund, cik, form, period, accession, url, path, rec)).lastrowid
    for h in holdings:
        d = h.to_dict()
        con.execute(f"INSERT INTO holdings (filing_id, {', '.join(HOLDING_COLS)}) VALUES (?{', ?' * len(HOLDING_COLS)})",
                    [fid] + [int(d[c]) if isinstance(d[c], bool) else d[c] for c in HOLDING_COLS])
    con.commit()
    return fid
