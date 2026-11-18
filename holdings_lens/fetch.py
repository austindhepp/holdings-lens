"""Download BDC filings from SEC EDGAR.

The SEC asks every client to send a descriptive User-Agent with a contact
email and to stay under 10 requests per second. Set SEC_USER_AGENT, e.g.
  export SEC_USER_AGENT="Holdings Lens your.name@example.com"
"""
from __future__ import annotations

import json
import os
import time

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"


def _session() -> requests.Session:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua or "@" not in ua:
        raise SystemExit('Set SEC_USER_AGENT to "Your App your.email@example.com" (SEC requirement).')
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    return s


def _get(s, url):
    time.sleep(0.15)  # stay well under the SEC rate limit
    r = s.get(url, timeout=30)
    r.raise_for_status()
    return r


def resolve_cik(s, ticker: str) -> tuple[str, str]:
    data = _get(s, TICKERS_URL).json()
    for row in data.values():
        if row["ticker"].upper() == ticker.upper():
            return str(row["cik_str"]), row["title"]
    raise SystemExit(f"Ticker {ticker} not found in SEC ticker list")


def list_filings(s, cik: str, forms=("10-Q", "10-K"), limit=4) -> list[dict]:
    sub = _get(s, SUBMISSIONS_URL.format(cik=cik)).json()
    recent = sub["filings"]["recent"]
    out = []
    for form, acc, doc, period in zip(recent["form"], recent["accessionNumber"],
                                       recent["primaryDocument"], recent["reportDate"]):
        if form in forms:
            out.append({"form": form, "accession": acc, "doc": doc, "period": period})
        if len(out) >= limit:
            break
    return out


def fetch(ticker: str, limit: int = 2, out_dir: str = os.path.join("data", "filings")) -> list[dict]:
    """Download the latest `limit` 10-Q/10-K filings for a ticker. Returns metadata dicts."""
    s = _session()
    cik, title = resolve_cik(s, ticker)
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    for f in list_filings(s, cik, limit=limit):
        url = ARCHIVE_URL.format(cik=int(cik), acc=f["accession"].replace("-", ""), doc=f["doc"])
        path = os.path.join(out_dir, f"{ticker.upper()}_{f['period']}_{f['form']}.html")
        if not os.path.exists(path):
            html = _get(s, url).text
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html)
        meta = {"fund": title, "ticker": ticker.upper(), "cik": cik, "url": url, "path": path, **f}
        with open(path + ".json", "w") as fh:
            json.dump(meta, fh, indent=2)
        saved.append(meta)
        print(f"  {meta['form']} {meta['period']} -> {path}")
    return saved
