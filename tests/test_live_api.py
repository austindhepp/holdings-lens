"""Tests for the live ask endpoint (api/ask.py), with the Anthropic API mocked."""
import importlib
import json
import os
import sys
import threading
import urllib.request
from http.server import HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "api"))

from holdings_lens import db  # noqa: E402
from holdings_lens.extract import extract_holdings  # noqa: E402


@pytest.fixture()
def live(tmp_path, monkeypatch):
    items = json.load(open(os.path.join(ROOT, "samples", "manifest.json")))
    p = str(tmp_path / "live.db")
    con = db.connect(p)
    for m in items:
        db.save_filing(con, m["fund"], m["path"], extract_holdings(open(os.path.join(ROOT, m["path"])).read()), period=m["period"])
    con.close()
    monkeypatch.setenv("LIVE_DB", p)
    monkeypatch.setenv("LIVE_PER_VISITOR_PER_HOUR", "2")
    import ask
    ask = importlib.reload(ask)
    return ask


def fake_post_factory():
    calls = []

    def fake_post(payload):
        calls.append(payload)
        assert payload["max_tokens"] <= 1200
        if len(calls) == 1:
            return {"content": [{"type": "tool_use", "id": "t1", "name": "run_sql",
                                 "input": {"query": "SELECT id, company FROM v_latest ORDER BY fair_value_usd DESC LIMIT 1"}}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}
        hid = json.loads(payload["messages"][-1]["content"][0]["content"])["rows"][0]["id"]
        return {"content": [{"type": "text", "text": f"Kinzie Cloud Systems is the largest [H{hid}]."}],
                "usage": {"input_tokens": 20, "output_tokens": 8}}
    return fake_post


def test_validation(live):
    assert live.validate({"question": "  "})[1]
    assert live.validate({"question": "x" * 301})[1]
    assert live.validate({"question": "What is the largest holding?"}) == ("What is the largest holding?", None)


def test_rate_limits(live):
    assert live.check_limits("1.2.3.4", now=1000) is None
    assert live.check_limits("1.2.3.4", now=1001) is None
    assert "per hour" in live.check_limits("1.2.3.4", now=1002)
    assert live.check_limits("1.2.3.4", now=1000 + 3700) is None  # window rolls over
    assert live.check_limits("5.6.7.8", now=1003) is None           # other visitors unaffected


def test_answer_is_grounded_and_trimmed(live):
    r = live.answer("largest?", post=fake_post_factory())
    assert "Kinzie" in r["answer"] and r["grounded"] is True and r["unsupported"] == []
    assert "trace" not in r and len(r["sources"]) == 1


def test_http_endpoint_end_to_end(live, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(live, "_base_post", fake_post_factory())
    srv = HTTPServer(("127.0.0.1", 0), live.handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/api/ask"
    try:
        health = json.loads(urllib.request.urlopen(url).read())
        assert health == {"ok": True, "database": True, "key_configured": True}
        req = urllib.request.Request(url, data=json.dumps({"question": "largest?"}).encode(),
                                     headers={"Content-Type": "application/json"})
        out = json.loads(urllib.request.urlopen(req).read())
        assert out["grounded"] and "Kinzie" in out["answer"]
        bad = urllib.request.Request(url, data=b'{"question": ""}', headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 400
    finally:
        srv.shutdown()


def test_missing_key_is_a_clean_error(live, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    srv = HTTPServer(("127.0.0.1", 0), live.handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/ask", data=b'{"question":"hi"}',
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 503 and "configured" in json.loads(e.value.read())["error"]
    finally:
        srv.shutdown()


def test_export_live_db_drops_sample_funds(tmp_path, monkeypatch):
    import sqlite3, subprocess, sys, os, shutil
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    work = tmp_path / "repo"
    shutil.copytree(root, work, ignore=shutil.ignore_patterns(".venv", "data", "web", "outreach", "docs", ".git", "__pycache__"))
    dbp = tmp_path / "h.db"
    run = lambda *a: subprocess.run([sys.executable, "-m", "holdings_lens", "--db", str(dbp), *a],
                                    cwd=work, capture_output=True, text=True)
    assert run("sample").returncode == 0
    r = run("export-live-db")
    assert r.returncode != 0 and "No real filings" in (r.stderr + r.stdout)
    con = sqlite3.connect(dbp)
    con.execute("UPDATE filings SET path='data/filings/REAL_10-Q.html', fund='Real Fund' WHERE id=(SELECT MIN(id) FROM filings)")
    con.commit(); con.close()
    r = run("export-live-db")
    assert r.returncode == 0, r.stderr
    live = sqlite3.connect(work / "api" / "holdings.db")
    funds = {f for (f,) in live.execute("SELECT fund FROM filings")}
    assert funds == {"Real Fund"}
    assert live.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] > 0
    assert "Left out" in r.stdout
