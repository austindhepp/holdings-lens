"""Command line: python -m holdings_lens <command>

  sample                      Load the bundled fictional filings (2 funds, 2 quarters) into the database
  fetch TICKER [--limit N]    Download recent 10-Q/10-K filings from SEC EDGAR
  extract FILE [FILE ...]     Extract holdings (and cross-check them against the filing's XBRL tags)
  report [--json]             Portfolio analytics: summaries, quarter-over-quarter changes, shared borrowers
  ask "QUESTION"              Ask the agent a question (needs ANTHROPIC_API_KEY)
  eval [--qa] [--dry-run]     Scorecard: extraction vs gold + XBRL, and optionally agent answers
  export-gold FILING_ID [--rows N]
                              Write a filing's extracted rows as a gold CSV to correct by hand
  export-live-db              Copy the database (read-only use) to api/holdings.db for the live ask box
  demo                        Run the test questions and save web/demo_run.json for the landing pages
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import db
from .extract import extract_holdings
from .reconcile import reconcile, summary_line

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "samples", "manifest.json")
QA = os.path.join(ROOT, "evals", "qa.json")


def cmd_extract(paths, fund=None, db_path=db.DEFAULT_DB, metas=None, quiet=False):
    con = db.connect(db_path)
    ids = []
    for i, p in enumerate(paths):
        meta = (metas or [{}] * len(paths))[i] or {}
        if not meta and os.path.exists(p + ".json"):
            meta = json.load(open(p + ".json"))
        html = open(p, encoding="utf-8", errors="replace").read()
        hs = extract_holdings(html)
        rec = reconcile(html, hs)
        fid = db.save_filing(con, fund or meta.get("fund") or os.path.basename(p), os.path.relpath(p, ROOT) if p.startswith(ROOT) else p,
                             hs, cik=meta.get("cik"), form=meta.get("form"), period=meta.get("period"),
                             accession=meta.get("accession"), url=meta.get("url"), reconciliation=rec)
        ids.append(fid)
        if not quiet:
            flagged = sum(1 for h in hs if h.notes)
            print(f"[F{fid}] {os.path.basename(p)}: {len(hs)} holdings ({flagged} flagged)")
            print("     " + summary_line(rec))
            if not hs:
                print("     No schedule-of-investments tables found. Check the file, or open an issue with the URL.")
    con.close()
    return ids


def sample_db() -> str:
    """A fresh database with only the bundled sample filings, so the test questions'
    expected answers hold even when your main database also has real filings."""
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix="holdings_lens_"), "samples.db")
    items = json.load(open(MANIFEST))
    cmd_extract([os.path.join(ROOT, m["path"]) for m in items], db_path=path, metas=items, quiet=True)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="holdings_lens", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sample")
    f = sub.add_parser("fetch"); f.add_argument("ticker"); f.add_argument("--limit", type=int, default=2)
    e = sub.add_parser("extract"); e.add_argument("files", nargs="+"); e.add_argument("--fund")
    r = sub.add_parser("report"); r.add_argument("--json", action="store_true")
    a = sub.add_parser("ask"); a.add_argument("question"); a.add_argument("--json", action="store_true")
    v = sub.add_parser("eval"); v.add_argument("--qa", action="store_true"); v.add_argument("--dry-run", action="store_true")
    v.add_argument("--manifest", default=MANIFEST); v.add_argument("--questions", default=QA)
    v.add_argument("--use-main-db", action="store_true", help="ask the test questions against --db instead of a fresh sample database")
    g = sub.add_parser("export-gold"); g.add_argument("filing_id", type=int); g.add_argument("--rows", type=int, default=30)
    x = sub.add_parser("export-live-db")
    x.add_argument("--include-samples", action="store_true", help="also keep the bundled fictional funds")
    d = sub.add_parser("demo"); d.add_argument("--questions", default=None)
    d.add_argument("--real", action="store_true",
                   help="ask real-filing questions (evals/qa_real.json) against the live database, in the live box's answer style")
    d.add_argument("--label", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "sample":
        items = json.load(open(MANIFEST))
        cmd_extract([os.path.join(ROOT, m["path"]) for m in items], db_path=args.db, metas=items)
    elif args.cmd == "fetch":
        from .fetch import fetch
        metas = fetch(args.ticker, limit=args.limit)
        print("Next: python -m holdings_lens extract " + " ".join(m["path"] for m in metas))
    elif args.cmd == "extract":
        cmd_extract(args.files, fund=args.fund, db_path=args.db)
    elif args.cmd == "report":
        from .analytics import full_report, report_markdown
        rep = full_report(db.connect(args.db))
        if args.json:
            print(json.dumps(rep, indent=2, default=str))
        else:
            md = report_markdown(rep)
            os.makedirs(os.path.join(ROOT, "evals"), exist_ok=True)
            open(os.path.join(ROOT, "evals", "report.md"), "w").write(md)
            print(md)
    elif args.cmd == "ask":
        from .agent import ask
        res = ask(args.question, db_path=args.db)
        if args.json:
            print(json.dumps(res, indent=2, default=str))
        else:
            print(res["answer"])
            c = res["citations"]
            print(f"\n-- cited {len(c['cited'])} rows; grounded={c['grounded']}; tokens={sum(res['usage'].values())}")
            if c["nonexistent"] or c["not_retrieved"]:
                print(f"!! unsupported citations: {c['nonexistent'] + c['not_retrieved']}")
    elif args.cmd == "eval":
        from .evals import eval_manifest, eval_qa, write_scorecard
        ex = eval_manifest(args.manifest, ROOT)
        qa_db = args.db if args.use_main_db else (sample_db() if (args.qa or args.dry_run) else None)
        qa = eval_qa(args.questions, db_path=qa_db, dry_run=args.dry_run) if (args.qa or args.dry_run) else None
        path = write_scorecard(ex, qa, out_dir=os.path.join(ROOT, "evals"))
        print(open(path).read())
        if qa and qa.get("dry_run"):
            print(json.dumps(qa["items"], indent=2, default=str))
    elif args.cmd == "export-gold":
        import csv
        from .evals import FIELDS
        con = db.connect(args.db)
        f = con.execute("SELECT * FROM filings WHERE id = ?", (args.filing_id,)).fetchone()
        if not f:
            raise SystemExit(f"No filing F{args.filing_id}. Run extract first.")
        rows = con.execute("SELECT * FROM holdings WHERE filing_id = ? ORDER BY id LIMIT ?", (args.filing_id, args.rows)).fetchall()
        slug = os.path.splitext(os.path.basename(f["path"]))[0]
        out = os.path.join(ROOT, "evals", "gold", slug + ".csv")
        bundled = {m["slug"] for m in json.load(open(MANIFEST))}
        if slug in bundled:
            raise SystemExit("That's a bundled sample filing; its gold file is generated. Use this for real filings.")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(FIELDS)
            for r in rows:
                w.writerow([r[k] if r[k] is not None else "" for k in FIELDS])
        mpath = os.path.join(ROOT, "evals", "real_manifest.json")
        items = json.load(open(mpath)) if os.path.exists(mpath) else []
        items = [i for i in items if i["slug"] != slug] + [{
            "slug": slug, "fund": f["fund"], "period": f["period"], "form": f["form"],
            "path": os.path.relpath(f["path"], ROOT) if os.path.isabs(f["path"]) else f["path"],
            "gold": os.path.relpath(out, ROOT), "layout": "real", "rows": len(rows)}]
        json.dump(items, open(mpath, "w"), indent=2)
        print(f"Wrote {len(rows)} rows to {out}")
        print("Now open it next to the filing and FIX every wrong cell (don't just accept it). Then run:")
        print("  python -m holdings_lens eval --manifest evals/real_manifest.json")
        print("Note: the scorer compares only the rows in this file, so it measures accuracy, not missed rows; "
              "the XBRL coverage check covers missed rows.")
    elif args.cmd == "export-live-db":
        import sqlite3
        src = db.connect(args.db)
        out = os.path.join(ROOT, "api", "holdings.db")
        if os.path.exists(out):
            os.remove(out)
        dst = sqlite3.connect(out)
        src.backup(dst)
        src.close()
        dropped = []
        if not args.include_samples:
            # The live site answers strangers: keep only real SEC filings, never the fictional practice funds.
            sample_ids = [r[0] for r in dst.execute(
                "SELECT id FROM filings WHERE REPLACE(path, '\\', '/') LIKE '%samples/%'")]
            dropped = [r[0] for r in dst.execute(
                "SELECT DISTINCT fund FROM filings WHERE REPLACE(path, '\\', '/') LIKE '%samples/%'")]
            if sample_ids:
                marks = ",".join("?" * len(sample_ids))
                dst.execute(f"DELETE FROM holdings WHERE filing_id IN ({marks})", sample_ids)
                dst.execute(f"DELETE FROM filings WHERE id IN ({marks})", sample_ids)
                dst.commit()
        n = dst.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
        funds = [r[0] for r in dst.execute("SELECT fund || ' ' || period FROM filings ORDER BY fund, period")]
        dst.execute("VACUUM")
        dst.close()
        if not funds:
            os.remove(out)
            raise SystemExit("No real filings in the database yet. Run fetch + extract first "
                             "(or pass --include-samples to publish the fictional demo funds).")
        print(f"Wrote {out} ({os.path.getsize(out) / 1e6:.1f} MB, {n} holdings) with: " + "; ".join(funds))
        if dropped:
            print("Left out the fictional sample funds: " + ", ".join(dropped))
        print("Commit it so the live ask box can read it. Re-run after you extract new filings.")
    elif args.cmd == "demo":
        from .evals import eval_manifest, eval_qa
        ex = eval_manifest(MANIFEST, ROOT)
        if args.real:
            live_db = os.path.join(ROOT, "api", "holdings.db")
            if not os.path.exists(live_db):
                raise SystemExit("No api/holdings.db yet: run export-live-db first.")
            import importlib.util
            spec = importlib.util.spec_from_file_location("live_ask", os.path.join(ROOT, "api", "ask.py"))
            live = importlib.util.module_from_spec(spec)
            os.environ.setdefault("LIVE_DB", live_db)
            spec.loader.exec_module(live)
            qa = eval_qa(args.questions or os.path.join(ROOT, "evals", "qa_real.json"), db_path=live_db,
                         style=live.LIVE_STYLE + live.data_scope())
            label = args.label or "Real SEC filings (saved answers from the live agent)"
        else:
            qa = eval_qa(args.questions or QA, db_path=sample_db())
            label = args.label or "Bundled sample filings (fictional funds)"
        out = {
            "placeholder": False, "label": label, "real": bool(args.real),
            "scorecard": {"cell_accuracy": ex["cell_accuracy"], "row_recall": ex["row_recall"],
                          "xbrl_match_rate": ex["xbrl_match_rate"], "qa_accuracy": qa["accuracy"],
                          "grounded_rate": qa["grounded_rate"], "avg_tokens": qa["avg_tokens"], "questions": qa["questions"]},
            "examples": [{"question": i["question"], "answer": i["run"]["answer"], "sources": i["run"]["sources"],
                          "correct": i["correct"], "grounded": i["grounded"], "unsupported": i["bad_citations"],
                          "tools_used": [t["tool"] for t in i["run"]["trace"]]} for i in qa["items"]],
        }
        path = os.path.join(ROOT, "web", "demo_run.json")
        with open(path, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"Saved {path}. Rebuild pages: python web/build_pages.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
