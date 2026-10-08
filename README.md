# Flight Watch — SEA / YVR → Sapporo (CTS)

Checks round-trip fares for every departure date × return date in your windows, from both
Seattle and Vancouver, saves every result to `prices.db`, alerts you on drops, and builds
`report.html` (price heatmap per airport, change since last check, cheapest-over-time chart).

## Setup (once)

Needs Python 3.10+.

```bash
cd flight-watch
pip install -r requirements.txt
python flight_watch.py run --demo     # fake prices, just to see the report — writes demo_* files
```

Open `demo_report.html` in a browser. Then edit `config.yaml` (date windows, target price)
and do a real check:

```bash
python flight_watch.py run
```

The current grid (5 departure dates Feb 10–14 × 3 return dates Feb 20–22 × 2 airports =
30 searches) takes about 3–4 minutes because it pauses between searches so Google doesn't
block you.

## Phone alerts (optional, free)

1. Install the **ntfy** app (iOS/Android) and subscribe to a hard-to-guess topic name.
2. Put that name in `config.yaml` → `alerts.ntfy_topic`.

You get one push per run when a fare is at/below `target_price` or has dropped by
`drop_percent` since the last check. Tapping it opens that search in Google Flights.

## Running it automatically

**Mac / Linux (cron)** — twice a day:
```
17 7,19 * * *  cd /path/to/flight-watch && /usr/bin/python3 flight_watch.py run >> watch.log 2>&1
```

**Windows** — Task Scheduler → Create Basic Task → Daily, repeat every 12 hours →
Program: `python`, Arguments: `flight_watch.py run`, Start in: the folder path.

**GitHub Actions (no computer needed)** — push this folder to a private GitHub repo; the
included `.github/workflows/check-fares.yml` runs twice a day and commits `prices.db` and
`report.html` back. Download `report.html` from the repo to view it. Google sometimes
blocks GitHub's servers; if runs start erroring, switch to SerpApi (below).

## Data sources

- `provider: google` (default) — free, uses the `fast-flights` library, which reads Google
  Flights directly. Unofficial: it can break when Google changes its site
  (`pip install -U fast-flights` usually fixes it).
- `provider: serpapi` — paid-but-reliable Google Flights API. Set `SERPAPI_KEY` in your
  environment (or as a GitHub secret). The free tier is 250 searches/month, so shrink the
  grid — at 30 searches/run you'd get about 8 runs a month.

## Notes

- Prices are the cheapest round-trip shown for each date pair, for the passengers/cabin/stop
  limit in the config. Use the same `currency` for both airports so YVR and SEA compare fairly.
- Change `currency`, and history in the old currency stays in the database but drops out of
  the report.
- `python flight_watch.py report` rebuilds the page without searching.
