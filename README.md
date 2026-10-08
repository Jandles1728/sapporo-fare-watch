# Flight Watch — SEA / YVR → Sapporo (CTS)

Checks fares for every departure date × return date in your windows, from both Seattle and
Vancouver: fly **into Sapporo (CTS)** and **home from Tokyo (Haneda or Narita)**. Each trip is
priced two ways — one multi-city ticket, and two separate one-way tickets — and the cheaper
one wins. Saves every result to `prices.db`, alerts you on drops, and builds `report.html`
(price heatmap per airport, one-way ticket prices, cheapest-over-time chart).

**Live report:** https://jandles1728.github.io/sapporo-fare-watch/ — refreshed after every
check. Click any fare to open that exact search in Google Flights.

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

The current setup (4 departure dates × 3 return dates × 2 home airports × 2 Tokyo airports,
priced as one ticket and as one-ways) is 68 searches and takes about 7 minutes because it pauses between searches so Google doesn't
block you.

## Phone alerts (optional, free)

1. Install the **ntfy** app (iOS/Android) and subscribe to a hard-to-guess topic name.
   Friends can subscribe to the same topic to get the same alerts.
2. On GitHub: repo **Settings → Secrets and variables → Actions → New repository secret**,
   name `NTFY_TOPIC`, value = your topic. (Running locally? Set the `NTFY_TOPIC` environment
   variable, or put it in `config.yaml` → `alerts.ntfy_topic` — but not in a public repo.)

You get one push per run when a fare is at/below `target_price` or has dropped by
`drop_percent` since the last check. Tapping it opens that search in Google Flights.

## Running it automatically

**Mac / Linux (cron)** — twice a day:
```
17 7,19 * * *  cd /path/to/flight-watch && /usr/bin/python3 flight_watch.py run >> watch.log 2>&1
```

**Windows** — Task Scheduler → Create Basic Task → Daily, repeat every 12 hours →
Program: `python`, Arguments: `flight_watch.py run`, Start in: the folder path.

**GitHub Actions (no computer needed)** — this is how it runs now. The included
`.github/workflows/check-fares.yml` runs twice a day, commits `prices.db`, `report.html` and
`last_run.log` back, and publishes the report to GitHub Pages (Settings → Pages → Source:
GitHub Actions). Run it on demand from Actions → Check Sapporo fares → Run workflow. Google
sometimes blocks GitHub's servers; if runs start erroring, switch to SerpApi (below).

## Data sources

- `provider: google` (default) — free, uses the `fast-flights` library, which reads Google
  Flights directly. Unofficial: it can break when Google changes its site
  (`pip install -U fast-flights` usually fixes it).
- `provider: serpapi` — paid-but-reliable Google Flights API. Set `SERPAPI_KEY` in your
  environment (or as a GitHub secret). The free tier is 250 searches/month, so shrink the
  grid — at 68 searches/run you'd get about 3 runs a month, so set `compare_one_ways: false` or
  trim `fly_home_from`.

## Notes

- Want a normal round trip again? Delete `fly_home_from` from `config.yaml`.
- Prices are the cheapest fare Google shows for each date pair, for the passengers/cabin/stop
  limit in the config. Use the same `currency` for both airports so YVR and SEA compare fairly.
- Change `currency`, and history in the old currency stays in the database but drops out of
  the report.
- `python flight_watch.py report` rebuilds the page without searching.
