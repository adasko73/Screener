# Wheel Screener — Free Mobile Deployment

Runs your real `option_screener.py` on a schedule via GitHub Actions,
and serves results as a mobile-friendly page via GitHub Pages. Both are free.

## What's in here

- `option_screener.py` — your scanner, unmodified
- `screener_config.json` — your watchlist and filter settings
- `export_results.py` — new wrapper that runs the scan and writes `docs/results.json`
- `.github/workflows/scan.yml` — schedules the scan every 15 min, market hours, weekdays
- `docs/index.html` — the mobile page (reads `results.json`)
- `docs/results.json` — placeholder until the first real scan runs

## One-time setup

1. **Create a GitHub repo** (public — needed for free unlimited Actions minutes).
   Name it anything, e.g. `wheel-screener`.

2. **Push these files** to the repo:
   ```bash
   cd wheel-screener
   git init
   git add .
   git commit -m "Initial screener deploy"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/wheel-screener.git
   git push -u origin main
   ```

3. **Enable GitHub Pages**:
   - Repo → Settings → Pages
   - Source: "Deploy from a branch"
   - Branch: `main`, folder: `/docs`
   - Save. Your page will be live at `https://YOUR_USERNAME.github.io/wheel-screener/`

4. **Trigger the first scan manually** (don't wait for the schedule):
   - Repo → Actions → "Run option screener" → Run workflow
   - This proves the pipeline works before you rely on the cron schedule

5. **Bookmark the Pages URL on your phone.** That's it — checkable from
   anywhere, no PC required.

## Adjusting settings

- **Watchlist / DTE / filters**: edit `screener_config.json`, commit, push.
  Next scheduled (or manual) run picks it up automatically.
- **Scan frequency**: edit the `cron:` line in `.github/workflows/scan.yml`.
  GitHub's minimum interval is 5 minutes, but actual firing can lag several
  minutes under load — this is a platform limitation, not something we can
  tune away on the free tier.
- **Market hours window**: the workflow runs 13:30–20:30 UTC, which covers
  9:30am–4:30pm ET across both EST and EDT with a small buffer. No changes
  needed for daylight saving.

## Known limitations (free tier tradeoffs)

- Results can be up to ~15-20 min stale depending on GitHub's scheduler load.
  The mobile page shows a "live / stale" badge with the actual data age so
  you always know what you're looking at.
- The repo (including your watchlist and filter settings) is public.
- yfinance is an unofficial scraper of Yahoo's site — if Yahoo changes their
  page structure, scans may fail until yfinance is updated. The page will
  show ticker-level errors if this happens rather than failing silently.

## Testing changes locally before pushing

```bash
pip install -r requirements.txt
python export_results.py --config screener_config.json --out docs/results.json
python -m http.server 8000 --directory docs
# open http://localhost:8000 in a browser
```
