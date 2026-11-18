import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from holdings_lens import analytics, db
from holdings_lens.agent import Tools, ask
from holdings_lens.evals import answer_ok, eval_manifest, numbers_in
from holdings_lens.extract import company_key, extract_holdings, parse_maturity, parse_number, parse_spread
from holdings_lens.reconcile import reconcile

MANIFEST = os.path.join(ROOT, "samples", "manifest.json")
ITEMS = json.load(open(MANIFEST))
PATH = {m["slug"]: os.path.join(ROOT, m["path"]) for m in ITEMS}


@pytest.fixture(scope="module")
def dbpath(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("db") / "h.db")
    con = db.connect(p)
    for m in ITEMS:
        html = open(PATH[m["slug"]]).read()
        hs = extract_holdings(html)
        db.save_filing(con, m["fund"], m["path"], hs, period=m["period"], reconciliation=reconcile(html, hs))
    con.close()
    return p


# ----------------------------------------------------------------- parsing helpers
def test_numbers_and_dates():
    assert parse_number("(1,234)") == -1234
    assert parse_number("$ 12.5 (3)") == 12.5
    assert parse_number("—") == 0.0
    assert parse_number("") is None
    assert parse_maturity("3/15/2029") == "2029-03-15"
    assert parse_maturity("Mar. 15, 2029") == "2029-03-15"
    assert parse_maturity("Sept. 30, 2029") == "2029-09-30"
    assert parse_maturity("03/2029") == "2029-03"
    assert parse_maturity("March 2029") == "2029-03"


@pytest.mark.parametrize("text,expected", [
    ("S + 5.50%", ("SOFR", 550)), ("SOFR + 550", ("SOFR", 550)), ("L+475 bps", ("LIBOR", 475)),
    ("P + 2.25%", ("Prime", 225)), ("", (None, None)),
])
def test_spread_parsing(text, expected):
    assert parse_spread(text) == expected


def test_company_key_matches_across_funds():
    assert company_key("Prairie Analytics, LLC") == company_key("Prairie Analytics LLC")
    assert company_key("Lakefront Logistics Holdings") == company_key("Lakefront Logistics Holdings, LLC")
    assert company_key("Wabash Dental Group") == company_key("Wabash Dental")


# ----------------------------------------------------------------- extraction vs gold
def test_extraction_matches_gold_on_both_layouts():
    r = eval_manifest(MANIFEST, ROOT)
    assert r["row_recall"] == 1.0 and r["row_precision"] == 1.0
    assert r["cell_accuracy"] == 1.0, [p["mistakes"] for p in r["filings"]]
    assert r["xbrl_match_rate"] == 1.0 and r["xbrl_totals_match"]


def test_flags_from_footnotes():
    hs = extract_holdings(open(PATH["wacker_2026q3"]).read())
    logan = next(h for h in hs if h.company.startswith("Logan") and h.lien != "equity")
    assert logan.non_accrual and "non-accrual" in logan.footnotes[0].lower()
    assert {h.company for h in hs if h.pik} == {"Wabash Dental Group", "Oak Lawn Medical Billing, LLC"}
    assert all(h.units == "thousands" for h in hs)


# ----------------------------------------------------------------- XBRL cross-check catches real errors
def test_reconcile_detects_a_missed_row():
    html = open(PATH["lakeshore_2026q3"]).read()
    # Simulate a parser miss: the row and its XBRL tags stay, but the visible value becomes unreadable.
    broken = html.replace(">27,363</ix:nonFraction>", ">27,363 n/m</ix:nonFraction>")
    assert broken != html
    r = reconcile(broken, extract_holdings(broken))
    assert any("Hyde Park" in m for m in r["tagged_but_not_extracted"])
    assert r["coverage"] < 1.0 and not r["totals_match"]


def test_reconcile_detects_a_wrong_column_mapping():
    html = open(PATH["wacker_2026q3"]).read()
    swapped = html.replace("<th colspan=\"2\">Amortized Cost</th><th colspan=\"2\">Fair Value</th>",
                           "<th colspan=\"2\">Fair Value</th><th colspan=\"2\">Amortized Cost</th>")
    assert swapped != html
    r = reconcile(swapped, extract_holdings(swapped))
    assert r["rows_matched"] == 0 and r["mismatches"][0]["problem"].startswith("read the InvestmentOwnedAtCost")


# ----------------------------------------------------------------- analytics
def test_quarter_over_quarter(dbpath):
    con = db.connect_readonly(dbpath)
    ch = analytics.changes(con, 1, 2)
    assert [p["company"] for p in ch["new_positions"]] == ["Bronzeville Learning, LLC"]
    assert [p["company"] for p in ch["exited_positions"]] == ["Cicero Packaging Co."]
    worst = ch["remarked"][0]
    assert worst["company"] == "Wabash Dental Group" and worst["change_pts"] < -10
    assert any(r["company"].startswith("Kinzie") for r in ch["resized"])


def test_cross_fund_dispersion(dbpath):
    ov = analytics.overlap(db.connect_readonly(dbpath))
    top = ov["shared_borrowers"][0]
    assert top["borrower"] == "Wabash Dental Group" and top["mark_dispersion_pts"] == 7.0
    assert {s["company_key"] for s in ov["shared_borrowers"]} >= {"prairie analytics", "kinzie cloud systems"}


def test_summary_metrics(dbpath):
    s = analytics.summary(db.connect_readonly(dbpath), 3)
    assert s["non_accrual"]["count"] == 1 and s["non_accrual"]["pct_of_fv"] == pytest.approx(4.42, abs=0.01)
    assert 500 < s["weighted_avg_spread_bps"] < 700
    assert sum(b["pct_of_debt"] or 0 for b in s["mark_distribution"]) == pytest.approx(100, abs=0.1)


# ----------------------------------------------------------------- agent
def test_sql_tool_is_read_only(dbpath):
    t = Tools(dbpath)
    assert "error" in json.loads(t.run_sql("DELETE FROM holdings"))
    assert "error" in json.loads(t.run_sql("SELECT 1; DROP TABLE holdings"))
    out = json.loads(t.run_sql("SELECT id, company FROM v_latest ORDER BY fair_value_usd DESC LIMIT 1"))
    assert out["rows"][0]["company"] == "Kinzie Cloud Systems, Inc."


def test_analytics_tool_marks_ids_as_retrieved(dbpath):
    t = Tools(dbpath)
    out = json.loads(t.call("compare_filings", {"old_filing_id": 1, "new_filing_id": 2}))
    expected = {p["id"] for p in out["new_positions"] + out["exited_positions"]} | {r["id"] for r in out["remarked"]}
    assert expected <= t.seen_ids
    assert out["from"]["id"] not in t.seen_ids or out["from"]["id"] in expected  # filing ids are not counted
    bad = json.loads(t.call("compare_filings", {"old_filing_id": "x"}))
    assert "error" in bad


def test_agent_loop_and_citation_check(dbpath):
    calls = []

    def fake_post(payload):
        calls.append(payload)
        if len(calls) == 1:
            return {"content": [{"type": "tool_use", "id": "t1", "name": "shared_borrowers", "input": {}}],
                    "usage": {"input_tokens": 100, "output_tokens": 20}}
        top = json.loads(payload["messages"][-1]["content"][0]["content"])["shared_borrowers"][0]
        hid = top["funds"][0]["ids"][0]
        return {"content": [{"type": "text", "text": f"Wabash Dental Group has the widest gap [H{hid}]. Also [H9999]."}],
                "usage": {"input_tokens": 150, "output_tokens": 30}}

    r = ask("widest gap?", db_path=dbpath, post=fake_post)
    assert r["citations"]["nonexistent"] == [9999]
    assert r["citations"]["grounded"] is False
    assert r["usage"] == {"input_tokens": 250, "output_tokens": 50}


def test_citation_of_unseen_row_is_flagged(dbpath):
    r = ask("q", db_path=dbpath, post=lambda p: {"content": [{"type": "text", "text": "Answer [H1]"}], "usage": {}})
    assert r["citations"]["not_retrieved"] == [1]


def test_answer_checker_understands_units():
    assert numbers_in("$222.3 million") == [222300000.0]
    assert answer_ok("about $222.3M of first lien", {"check": "number", "tolerance": 0.005}, 222_340_000)
    assert not answer_ok("$250M", {"check": "number", "tolerance": 0.005}, 222_340_000)


def test_changes_aggregate_tranches_and_pair_renames(tmp_path):
    """Real funds hold several tranches of one loan and rename borrowers between quarters."""
    from holdings_lens.extract import Holding, company_key
    def H(co, fv, cost, inst="One stop"):
        return Holding(company=co, industry="Software", investment_type="Debt investments", instrument=inst,
                       interest_rate=None, spread=None, maturity=None, principal=cost, cost=cost, fair_value=fv,
                       pct_net_assets=None, units="thousands", table_idx=0, row_idx=0, raw="",
                       lien="unitranche", company_key=company_key(co), mark=fv / cost)
    p = str(tmp_path / "c.db")
    con = db.connect(p)
    db.save_filing(con, "Fund", "q1", [H("Acme Purchaser III & Jet Tools Ltd.", 100, 100), H("Beta LLC", 50, 50), H("Beta LLC", 50, 50)], period="2026-03-31")
    db.save_filing(con, "Fund", "q2", [H("Acme Purchaser III, Inc.", 100, 100), H("Beta LLC", 40, 50), H("Beta LLC", 40, 50)], period="2026-06-30")
    ch = analytics.changes(db.connect_readonly(p), 1, 2)
    assert ch["fair_value_usd"] == {"from": 200000.0, "to": 180000.0, "change": -20000.0}
    assert not ch["new_positions"] and not ch["exited_positions"]
    assert ch["renamed"][0]["to"] == "Acme Purchaser III, Inc."
    beta = next(r for r in ch["remarked"] if r["company"] == "Beta LLC")
    assert beta["prior_mark"] == 1.0 and beta["mark"] == 0.8 and len(beta["ids"]) == 2


def test_rows_read_through_views_count_as_retrieved(dbpath):
    t = Tools(dbpath)
    out = json.loads(t.run_sql("SELECT id, company FROM v_latest ORDER BY fair_value_usd DESC LIMIT 3"))
    assert {r["id"] for r in out["rows"]} <= t.seen_ids


def test_aliased_id_columns_count_as_retrieved(dbpath):
    t = Tools(dbpath)
    out = json.loads(t.run_sql("SELECT a.id AS old_id, b.id AS new_id, a.filing_id FROM v_holdings a JOIN v_holdings b "
                               "ON a.company_key = b.company_key AND a.filing_id = 1 AND b.filing_id = 2 LIMIT 3"))
    got = {r["old_id"] for r in out["rows"]} | {r["new_id"] for r in out["rows"]}
    assert got <= t.seen_ids and 1 not in t.seen_ids - got  # filing_id values are not counted
