"""AI sector classification: parsing, caching/resume, retries and the sectors table (model stubbed)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from holdings_lens import db, sectors  # noqa: E402
from holdings_lens.__main__ import sample_db  # noqa: E402


def test_parse_reply_keeps_only_valid_records():
    text = 'Here you go: [{"key": "acme", "sector": "Software & SaaS", "software": true, "basis": "description", ' \
           '"confidence": "high", "reason": "Insurance software provider"}, {"key": "beta", "sector": "Made up", ' \
           '"software": false, "basis": "vibes", "confidence": "??", "reason": "x"}, {"key": "stranger", "sector": "Education"}]'
    got = sectors.parse_reply(text, ["acme", "beta"])
    assert set(got) == {"acme", "beta"}
    assert got["acme"]["sector"] == "Software & SaaS" and got["acme"]["software"] is True
    assert got["beta"] == {"sector": "Other / unclear", "software": False, "basis": "name", "confidence": "low", "reason": "x"}
    assert sectors.parse_reply("no json here", ["acme"]) == {}


def test_description_is_read_from_rows_that_have_one():
    raw = "Huskies Parent, Inc. (13) | Insurance software provider | First lien senior secured loan | 9.1 % | SOFR (Q) | 5.25 % | 10.0 | 9.9"
    assert sectors._description(raw, "Huskies Parent, Inc.", "First lien senior secured loan", None) == "Insurance software provider"
    assert sectors._description("Acme LLC+(25) | One stop | SF + | 4.50 % | 08/2029 | 1,000", "Acme LLC", "One stop", "Software") is None
    assert sectors._description("Symplr Software Inc. | Base Rate (Q) | Series C-3 preferred shares", "Symplr Software Inc.", None, None) is None


def test_classify_caches_resumes_retries_and_loads_table(tmp_path):
    con = db.connect(sample_db())
    keys = [e["key"] for e in sectors.gather(con)]
    assert len(keys) > 5
    calls = []

    def stub(payload):
        chunk = json.loads(payload["messages"][0]["content"])
        calls.append(len(chunk))
        if len(calls) == 1:  # first reply is malformed: the batch must be retried once
            return {"content": [{"type": "text", "text": "oops"}], "usage": {"input_tokens": 5, "output_tokens": 1}}
        recs = [{"key": e["key"], "sector": "Software & SaaS" if "Software" in json.dumps(e) else "Business & professional services",
                 "software": "Software" in json.dumps(e), "basis": "label", "confidence": "medium", "reason": "label says so"} for e in chunk]
        return {"content": [{"type": "text", "text": json.dumps(recs)}], "usage": {"input_tokens": 10, "output_tokens": 10}}

    cache = str(tmp_path / "sectors.json")
    res = sectors.classify(con, stub, cache, "test-model", batch=100, log=lambda *_: None)
    assert res["classified"] == len(keys) and res["missed"] == 0 and calls == [len(keys), len(keys)]
    again = sectors.classify(con, stub, cache, "test-model", batch=100, log=lambda *_: None)
    assert again["classified"] == 0 and len(calls) == 2  # nothing left to pay for
    assert sectors.load_into_db(con, sectors.load_cache(cache)) == len(keys)
    assert sectors.has_sectors(con)
    n_sw = con.execute("SELECT COUNT(*) FROM sectors WHERE is_software = 1").fetchone()[0]
    assert 0 < n_sw < len(keys)
