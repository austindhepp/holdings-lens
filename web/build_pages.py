"""Build one personalized landing page per recipient.

Inputs:
  web/site.json        your name, email, GitHub, résumé link
  web/recipients.csv   slug, first_name, company, focus, note   (one row per person)
  web/demo_run.json    output of `python -m holdings_lens demo` (optional)
Output:
  web/dist/index.html                generic page
  web/dist/for/<slug>/index.html     one page per recipient

Host web/dist anywhere static (GitHub Pages, Netlify, Vercel) and send each
person their own /for/<slug>/ link.
"""
import csv
import html
import json
import os
import sys
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from holdings_lens import db as hdb  # noqa: E402
from holdings_lens.__main__ import MANIFEST, cmd_extract  # noqa: E402
from holdings_lens.analytics import full_report  # noqa: E402
from holdings_lens.evals import eval_manifest  # noqa: E402


def load_demo():
    p = os.path.join(HERE, "demo_run.json")
    if os.path.exists(p):
        return json.load(open(p))
    qs = json.load(open(os.path.join(ROOT, "evals", "qa.json")))
    return {"placeholder": True, "label": "Bundled sample filings (fictional funds)",
            "examples": [{"question": q["question"], "answer": None, "sources": [],
                          "expected": ", ".join(q["expected"]) if q["check"] == "contains" else ""} for q in qs]}


def count_tests():
    import re
    import subprocess
    out = subprocess.run([sys.executable, "-m", "pytest", "--co", "-q", os.path.join(ROOT, "tests")],
                         capture_output=True, text=True, cwd=ROOT).stdout
    m = re.search(r"(\d+) tests? collected", out)
    return int(m.group(1)) if m else 0


def page_data(demo):
    import tempfile
    items = json.load(open(MANIFEST))
    # Always build analytics from a fresh database. (ignore_cleanup_errors: Windows can't delete a file
    # that another process, e.g. antivirus, still has open; a leftover temp file is harmless.)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "h.db")
        cmd_extract([os.path.join(ROOT, m["path"]) for m in items], db_path=path, metas=items, quiet=True)
        con = hdb.connect(path)
        try:
            report = full_report(con)
        finally:
            con.close()
    ex = eval_manifest(MANIFEST, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    import test_layouts  # noqa: E402
    sc = dict(demo.get("scorecard") or {})
    sc.update(cell_accuracy=ex["cell_accuracy"], cells=ex["cells"], row_recall=ex["row_recall"],
              xbrl_match_rate=ex["xbrl_match_rate"], layouts=len(test_layouts.VARIANTS) + 1 + len({m["layout"] for m in items}),
              tests=count_tests())
    trust = []
    for m, p in zip(items, ex["filings"]):
        x = p["xbrl"]
        trust.append({"fund": m["fund"], "period": m["period"], "layout": m["layout"], "rows_matched": p["rows_matched"],
                      "rows_expected": p["rows_expected"], "cell_accuracy": p["cell_accuracy"],
                      "xbrl_matched": x.get("rows_matched", 0), "xbrl_linked": x.get("rows_linked", 0),
                      "coverage": x.get("coverage"), "totals_match": bool(x.get("totals_match"))})
    real_path = os.path.join(ROOT, "evals", "real_results.json")
    real = json.load(open(real_path)) if os.path.exists(real_path) else None
    return {
        "demo": demo, "scorecard": sc, "report": report, "trust": trust, "real": real,
        "ask_url": None,  # set per site in main()
        "trust_note": ("These bundled filings were written to exercise known quirks, so perfect scores here prove the logic, "
                       "not real-world accuracy. The same checks run unchanged on real EDGAR filings."),
    }


def render(tpl, site, data, r=None):
    first = (r or {}).get("first_name", "").strip()
    company = (r or {}).get("company", "").strip()
    focus = (r or {}).get("focus", "").strip()
    note = (r or {}).get("note", "").strip()
    c, fo = html.escape(company), html.escape(focus)
    target = f"the {fo} at {c}" if ("'" in company or company.endswith("s")) else f"{c}'s {fo}"
    if r and first:
        headline = f"Hi {html.escape(first)}, I built this with {target} in mind."
    elif r:
        headline = f"Built with {target} in mind."
    else:
        headline = "Clean, cited data from messy fund filings."
    subject = quote(f"Coffee? (Holdings Lens{' / ' + company if company else ''})")
    rep = {
        "{{TITLE_SUFFIX}}": f" · for {html.escape(company)}" if company else "",
        "{{NAME}}": html.escape(site["name"]),
        "{{HEADLINE}}": headline,
        "{{NOTE_BLOCK}}": f'<div class="note">{html.escape(note)}</div>' if note else "",
        "{{CTA_TEXT}}": html.escape(site.get("cta_text", "")),
        "{{MAILTO}}": f"mailto:{site['email']}?subject={subject}",
        "{{GITHUB}}": html.escape(site["github"]),
        "{{RESUME_LINK}}": (f'<a class="btn ghost" href="{html.escape(site["resume_url"])}">Résumé</a>'
                            if site.get("resume_url") else ""),
        "{{DATA_LABEL}}": html.escape(data["demo"].get("label", "")),
        "{{DATA_JSON}}": json.dumps(data, default=str).replace("</", "<\\/"),
    }
    out = tpl
    for k, v in rep.items():
        out = out.replace(k, v)
    return out


def main():
    site = json.load(open(os.path.join(HERE, "site.json")))
    tpl = open(os.path.join(HERE, "templates", "page.html")).read()
    data = page_data(load_demo())
    data["ask_url"] = site.get("ask_url", "/api/ask") or None  # "" in site.json turns the live box off
    dist = os.path.join(HERE, "dist")
    os.makedirs(dist, exist_ok=True)
    open(os.path.join(dist, "index.html"), "w").write(render(tpl, site, data))
    n = 0
    for r in csv.DictReader(open(os.path.join(HERE, "recipients.csv"))):
        slug = r["slug"].strip().lower()
        if not slug or not slug.replace("-", "").isalnum():
            print(f"skipping bad slug: {slug!r}")
            continue
        d = os.path.join(dist, "for", slug)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "index.html"), "w").write(render(tpl, site, data, r))
        n += 1
        print(f"  /for/{slug}/")
    print(f"Built generic page + {n} personalized pages in {dist}")


if __name__ == "__main__":
    main()
