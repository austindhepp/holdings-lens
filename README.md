# Holdings Lens

**An AI agent for private-credit data.** It extracts every holding from the schedule-of-investments tables that business development companies (BDCs) file with the SEC, reconciles each number with the filing's own inline XBRL tags, computes the analytics a credit analyst asks for, and answers questions with a verified citation for every figure.

![CI](https://github.com/YOUR_USERNAME/holdings-lens/actions/workflows/ci.yml/badge.svg)

```
$ python -m holdings_lens sample
[F2] lakeshore_2026q3.html: 18 holdings (4 flagged)
     XBRL cross-check: 18/18 tagged rows match (100%); coverage 100% of tagged investments; totals match
[F3] wacker_2026q3.html: 10 holdings (2 flagged)
     XBRL cross-check: 9/9 tagged rows match (100%); coverage 100% of tagged investments; totals match

$ python -m holdings_lens report
## Changes: Lakeshore Example Capital Corp, 2026-06-30 → 2026-09-30
- New: Bronzeville Learning, LLC Term Loan [H28]
- Exited: Cicero Packaging Co. Term Loan [H10]
- Re-marked: Wabash Dental Group Term Loan 96% → 85% (-10.5 pts) [H24]
## Same borrower, different marks
- Wabash Dental Group: Lakeshore Example Capital Corp 85%; Wacker Street Credit Fund 78% (spread of 7.0 pts)
```
*(Output on the bundled fictional filings.)*

## Results on real filings

Two large private-credit lenders with different table layouts, each checked against the filing's own inline XBRL tags:

| Filing | Holdings extracted | XBRL-tagged rows matching | Tagged investments found | Total fair value |
|---|---|---|---|---|
| Golub Capital BDC (GBDC) 10-Q, June 30, 2026 | 1,814 | 1,683 / 1,683 | 100% | $8,196,353,000, ties to the dollar |
| Golub Capital BDC (GBDC) 10-Q, March 31, 2026 | 1,797 | 1,681 / 1,681 | 100% | $8,317,245,000, ties to the dollar |
| Ares Capital (ARCC) 10-Q, September 30, 2026 | 1,422 | 1,294 / 1,294 | 100% | $29,213M, ties (reported in millions) |
| Ares Capital (ARCC) 10-Q, June 30, 2026 | 1,439 | 1,318 / 1,318 | 100% | $29,349M, ties (reported in millions) |

Getting there meant handling what real EDGAR filings do and the synthetic samples didn't:
- two schedules in one filing (current quarter plus prior year-end), separated using XBRL context dates
- continuation tables with no header row, matched to the nearest header whose columns line up
- a blank issuer header
- spreads split across cells ("SF +" | "4.50 %")
- units stated above each table
- footnote numbers reused across the filing, resolved from the footnote block after the schedule
- unlabeled numeric subtotals
- (Ares) affiliate roll-forward tables and interest-rate-swap tables that also have "fair value" and "cost" columns but repeat or aren't investments: the first run over-counted by $5.3B and the totals check caught it
- (Ares) footnote numbers in their own column between fair value and % of net assets, with the definitions several tables further on: non-accrual loans then came out at 2.41% of cost, matching the 2.4% Ares itself reports
- (Ares) base rate and spread in separate columns ("SOFR (Q)" | "5.25 %") and non-US base rates (NIBOR, TONA, BBSY, CDOR): spreads went from 0 to 890 of 1,008 loans; the rest are fixed-rate or undrawn
- (Ares) ownership stakes named without the word "equity" ("Limited partnership interest", "Ordinary shares") and no Debt/Equity section headings
- (Ares) XBRL members spelled as names (`arcc:ATIRestorationLLCMember`), including a filer typo and one tagged by business description

The resulting report (`evals/real_report_gbdc.md`) flags 27 non-accruals ($8.2B portfolio, 0.89% of fair value), positions that newly went on non-accrual this quarter (largest $51.5M), debt re-marks, and borrowers renamed between quarters.

Fair value, cost and coverage are verified by XBRL. Text fields (names, spreads, maturities) still need a hand-checked sample: `python -m holdings_lens export-gold <filing id>`.

## Why it's built this way

- **Two independent sources must agree.** Tables are parsed from HTML, then reconciled with the inline XBRL facts most BDC filings now include. Wrong column mappings, unit mistakes and missed rows show up with no answer key.
- **One extractor, any layout.** Columns are found by header meaning, not position. A stress suite renders the same holdings in 9 layouts: reordered columns, split headers, `<th>` headers, "$" cells, footnotes, section rows, and millions vs thousands.
- **Answers you can audit.** The agent only sees data through read-only tools. A citation counts only if that row was actually retrieved during the run.
- **Measured, not claimed.** A scorecard covers cell-level accuracy against hand-checked rows, XBRL reconciliation, answer accuracy against SQL-computed truth, grounding rate, tokens and latency.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the pipeline diagram and the trade-offs.

## What it computes

| Per filing | Across quarters | Across funds |
|---|---|---|
| Lien mix, top-10 concentration, industry mix | New, exited, re-marked and resized positions | Borrowers held by several funds, matched despite different spellings |
| Weighted average spread (bps) and rate | Fair value change | Each fund's mark on the same borrower, and the dispersion between them |
| Non-accruals (% of fair value and of cost), PIK | | |
| Mark distribution, watchlist, maturity wall | | |

## Quick start

```bash
pip install -r requirements.txt
make all            # tests, load samples, scorecard, analytics report, landing pages

# Real filings from SEC EDGAR (the SEC requires a contact email in your User-Agent)
export SEC_USER_AGENT="Holdings Lens your.email@example.com"
python -m holdings_lens fetch MRCC --limit 2          # any BDC ticker
python -m holdings_lens extract data/filings/MRCC_*.html
python -m holdings_lens report

# Ask questions (needs an Anthropic API key; model defaults to claude-sonnet-5-5, override with HOLDINGS_MODEL)
export ANTHROPIC_API_KEY=...
python -m holdings_lens ask "Which borrowers did the fund mark down the most this quarter?"
python -m holdings_lens eval --qa      # score the agent on evals/qa.json
python -m holdings_lens demo           # cache answers for the landing pages
```

## Layout

| Path | What it is |
|---|---|
| `holdings_lens/extract.py` | Table detection, header mapping, row parsing, derived fields, source pointers |
| `holdings_lens/xbrl.py` · `reconcile.py` | Inline XBRL facts and the three-way cross-check |
| `holdings_lens/analytics.py` | Portfolio summary, quarter-over-quarter changes, cross-fund dispersion |
| `holdings_lens/agent.py` | Tool-using agent over the Messages API, with citation verification |
| `holdings_lens/evals.py` | Scorecard |
| `holdings_lens/fetch.py` · `db.py` | EDGAR downloader, SQLite schema and views |
| `samples/` · `tools/make_samples.py` | **Fictional** filings (2 funds, 2 quarters, 2 layouts) with XBRL tags |
| `tests/` | 54 tests, a few seconds, no network (API calls mocked) |
| `web/` | Personalized landing pages with an interactive explorer (pick filings, compare lenders, build charts) |
| `api/ask.py` · `vercel.json` | Live ask box: serverless endpoint with rate limits and a token cap |

## Honest limitations

- The bundled filings are fictional and were written to exercise known quirks. Perfect scores on them prove the logic, not real-world accuracy. Run it on real filings and hand-check 30–50 rows (`evals/gold/*.csv` shows the format) for a real number.
- XBRL tagging depth varies by filer and year. Where the schedule isn't tagged, the report says the cross-check isn't available, and the gold-row scorecard is the only check.
- Footnote conventions differ. Non-accrual and PIK detection relies on footnote text and rate strings, so new phrasings may need a keyword.
- Next steps: cross-check against XBRL dimensions beyond fair value, track positions across more quarters, and add a small UI for asking questions live.

MIT licensed.
