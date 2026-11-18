# Publishing Holdings Lens with the live "Ask" box

Your pages and the live agent are hosted together on **Vercel** (free tier); the code lives on **GitHub**. Your API key is stored as a secret on Vercel and never appears in the web page or the repo.

## 1. Prepare (Windows terminal, in the project folder)

```
.venv\Scripts\activate
python -m pytest -q
python -m holdings_lens export-live-db
python web/build_pages.py
xcopy web\dist docs /E /I /Y
```

- `export-live-db` writes `api\holdings.db`: a copy of your database (sample funds plus Golub) that the live box reads.
- `docs\` is the folder Vercel serves.

## 2. Put the code on GitHub

1. Install Git if `git --version` doesn't work: https://git-scm.com/download/win (accept the defaults).
2. On github.com, create a **public** repository named `holdings-lens`, with no README.
3. In your terminal (replace `YOUR_USERNAME`):

```
git init
git add .
git status
git commit -m "Holdings Lens with live ask box"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/holdings-lens.git
git push -u origin main
```

Before you commit, check the list `git status` prints. It must **not** include `outreach/contacts.csv`, anything in `data/`, or any file containing your API key. `.gitignore` already excludes them.

## 3. Create a dedicated API key with a spending cap

1. In console.anthropic.com, create a **new** key just for the website, for example `holdings-lens-live`.
2. In the console's limits or billing settings, set a **monthly spend limit** you're comfortable with, such as $10–20. This is the hard cap that protects you if the page gets traffic. Check the console for where it currently lives.

## 4. Deploy on Vercel

1. Go to https://vercel.com and sign up **with your GitHub account**.
2. Click **Add New → Project**, then find `holdings-lens` and click **Import**.
3. Framework Preset: **Other**. Leave the build settings empty; `vercel.json` handles them.
4. Open **Environment Variables** and add `ANTHROPIC_API_KEY`, with the new key as its value.
   - Optional: add `HOLDINGS_MODEL` to use a cheaper model for public questions.
5. Click **Deploy** and wait about a minute. You'll get a URL like `https://holdings-lens-xxxx.vercel.app`.

## 5. Check it

- Open `https://YOUR-URL.vercel.app/api/ask`. It should show `{"ok": true, "database": true, "key_configured": true}`.
- Open `https://YOUR-URL.vercel.app/for/morningstar/` and ask a question in the live box.

## 6. Point your links at the live site

In `web\site.json`, set:

```
"site_url": "https://YOUR-URL.vercel.app",
"github": "https://github.com/YOUR_USERNAME/holdings-lens",
```

Then rebuild and push. Vercel redeploys automatically on every push.

```
python web/build_pages.py
xcopy web\dist docs /E /I /Y
git add .
git commit -m "Live site links"
git push
```

## Guardrails (already built in)

| Guardrail | Default | Change with Vercel env var |
|---|---|---|
| Question length | 300 characters | `LIVE_MAX_QUESTION_CHARS` |
| Questions per visitor per hour | 5 | `LIVE_PER_VISITOR_PER_HOUR` |
| Questions per day (per server instance) | 150 | `LIVE_DAILY_CAP` |
| Agent tool steps per question | 6 | `LIVE_MAX_TURNS` |
| Output tokens per model call | 1,200 | `LIVE_MAX_TOKENS` |

The per-visitor and daily limits are best effort, because serverless instances don't share memory. The spend limit from step 3 is your real ceiling. To turn the live box off, set `"ask_url": ""` in `web\site.json` and rebuild.
