#!/usr/bin/env python3
"""
Flight Watch — track round-trip fares across a grid of dates and alert on drops.

  python flight_watch.py run            # check every date combo once, save, alert, rebuild report
  python flight_watch.py report         # rebuild report.html from saved history
  python flight_watch.py run --demo     # fake prices, no network — to see how it works

Schedule `run` 1–3x a day (cron / Task Scheduler / GitHub Actions; see README).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "prices.db"
REPORT_PATH = HERE / "report.html"


# ───────────────────────── config ─────────────────────────
def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    cfg["origins"] = [o.upper() for o in cfg["origins"]]
    cfg["destination"] = cfg["destination"].upper()
    for k in ("depart_from", "depart_to", "return_from", "return_to"):
        if isinstance(cfg.get(k), str):
            cfg[k] = dt.date.fromisoformat(cfg[k])
    cfg.setdefault("alerts", {})
    return cfg


def date_combos(cfg: dict):
    """Every (depart, return) pair: a return-date window if given, otherwise trip lengths."""
    def days(a, b):
        while a <= b:
            yield a
            a += dt.timedelta(days=1)
    for d in days(cfg["depart_from"], cfg["depart_to"]):
        if cfg.get("return_from"):
            for r in days(cfg["return_from"], cfg["return_to"]):
                if r > d:
                    yield d, r
        else:
            for n in cfg["trip_lengths"]:
                yield d, d + dt.timedelta(days=int(n))


def google_flights_url(origin, dest, dep, ret, currency):
    q = f"Flights from {origin} to {dest} on {dep} through {ret}"
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q, "curr": currency})


# ───────────────────────── providers ─────────────────────────
# Each returns the cheapest offer: {"price", "airlines", "stops"} or None if nothing found.

def search_google(cfg, origin, dest, dep, ret):
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    from fast_flights.exceptions import FlightsNotFound  # noqa

    ms = cfg.get("max_stops")
    q = create_query(
        flights=[
            FlightQuery(date=dep.isoformat(), from_airport=origin, to_airport=dest, max_stops=ms),
            FlightQuery(date=ret.isoformat(), from_airport=dest, to_airport=origin, max_stops=ms),
        ],
        trip="round-trip",
        seat=cfg.get("seat", "economy"),
        passengers=Passengers(adults=int(cfg.get("adults", 1))),
        currency=cfg.get("currency", "USD"),
        language="en-US",
    )
    try:
        results = get_flights(q)
    except Exception as e:  # FlightsNotFound or parse errors
        if type(e).__name__ == "FlightsNotFound":
            return None
        raise
    offers = [f for f in results if getattr(f, "price", 0)]
    if not offers:
        return None
    best = min(offers, key=lambda f: f.price)
    return {
        "price": int(best.price),
        "airlines": ", ".join(dict.fromkeys(best.airlines)),
        "stops": max(len(best.flights) - 1, 0),
    }


SERP_STOPS = {None: 0, 0: 1, 1: 2, 2: 3}
SERP_CLASS = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}


def search_serpapi(cfg, origin, dest, dep, ret):
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        sys.exit("provider is 'serpapi' but SERPAPI_KEY is not set")
    params = {
        "engine": "google_flights", "api_key": key, "type": 1, "hl": "en",
        "departure_id": origin, "arrival_id": dest,
        "outbound_date": dep.isoformat(), "return_date": ret.isoformat(),
        "currency": cfg.get("currency", "USD"), "adults": cfg.get("adults", 1),
        "stops": SERP_STOPS.get(cfg.get("max_stops"), 0),
        "travel_class": SERP_CLASS.get(cfg.get("seat", "economy"), 1),
    }
    with urllib.request.urlopen("https://serpapi.com/search.json?" + urllib.parse.urlencode(params), timeout=60) as r:
        data = json.load(r)
    if "error" in data and "no results" not in data["error"].lower():
        raise RuntimeError(data["error"])
    offers = [o for o in data.get("best_flights", []) + data.get("other_flights", []) if o.get("price")]
    if not offers:
        return None
    best = min(offers, key=lambda o: o["price"])
    return {
        "price": int(best["price"]),
        "airlines": ", ".join(dict.fromkeys(s.get("airline", "?") for s in best.get("flights", []))),
        "stops": len(best.get("layovers", [])),
    }


def search_demo(cfg, origin, dest, dep, ret, _state={}):
    """Deterministic-ish fake fares that wander between runs."""
    base = 1050 if origin == "SEA" else 1120
    seed = zlib.crc32(f"{origin}{dep}{ret}".encode()) % 997
    bump = 180 if dt.date(dep.year, 2, 12) <= dep <= dt.date(dep.year, 2, 15) else 0  # long-weekend premium
    drift = _state.setdefault("drift", random.uniform(-90, 60))
    price = base + (seed % 220) + bump + (ret - dep).days * 6 + drift + random.uniform(-40, 40)
    return {"price": int(price), "airlines": random.choice(["ANA", "Japan Airlines", "Delta, Korean Air", "Air Canada"]), "stops": 1}


PROVIDERS = {"google": search_google, "serpapi": search_serpapi, "demo": search_demo}


# ───────────────────────── storage ─────────────────────────
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS prices(
        run_at TEXT, origin TEXT, dest TEXT, depart TEXT, ret TEXT,
        price INTEGER, currency TEXT, airlines TEXT, stops INTEGER)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix ON prices(origin, depart, ret, run_at)")
    return con


def previous_and_low(con, origin, dest, dep, ret, currency):
    prev = con.execute("""SELECT price FROM prices WHERE origin=? AND dest=? AND depart=? AND ret=? AND currency=?
                          ORDER BY run_at DESC LIMIT 1""", (origin, dest, dep, ret, currency)).fetchone()
    low = con.execute("""SELECT MIN(price) FROM prices WHERE origin=? AND dest=? AND depart=? AND ret=? AND currency=?""",
                      (origin, dest, dep, ret, currency)).fetchone()
    return (prev[0] if prev else None), (low[0] if low else None)


# ───────────────────────── run ─────────────────────────
def run(cfg, demo=False):
    search = PROVIDERS["demo" if demo else cfg.get("provider", "google")]
    con = db()
    run_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    cur, dest = cfg.get("currency", "USD"), cfg["destination"]
    al = cfg["alerts"]
    alerts, ok, failed = [], 0, 0
    combos = [(o, d, r) for o in cfg["origins"] for d, r in date_combos(cfg)]
    print(f"Checking {len(combos)} itineraries ({run_at})")

    for i, (origin, dep, ret) in enumerate(combos, 1):
        label = f"{origin}→{dest} {dep:%b %d}–{ret:%b %d}"
        try:
            offer = search(cfg, origin, dest, dep, ret)
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(combos)}] {label}: ERROR {type(e).__name__}: {str(e)[:120]}")
            offer = None
        if offer:
            prev, low = previous_and_low(con, origin, dest, dep.isoformat(), ret.isoformat(), cur)
            con.execute("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?)",
                        (run_at, origin, dest, dep.isoformat(), ret.isoformat(), offer["price"], cur,
                         offer["airlines"], offer["stops"]))
            con.commit()
            ok += 1
            p = offer["price"]
            change = "" if prev is None else f" ({p - prev:+d})"
            print(f"  [{i}/{len(combos)}] {label}: {cur} {p:,}{change}  {offer['airlines']}")

            why = []
            if al.get("target_price") and p <= al["target_price"]:
                why.append(f"under target {al['target_price']:,}")
            if prev and al.get("drop_percent") and (prev - p) / prev * 100 >= al["drop_percent"]:
                why.append(f"down {prev - p:,} from {prev:,}")
            if low is not None and p < low:
                why.append("new low")
            # only notify on drops/targets, not on "new low" by itself (too chatty)
            if why and (len(why) > 1 or why[0] != "new low"):
                alerts.append((p, f"{label}: {cur} {p:,} — {', '.join(why)} [{offer['airlines']}]",
                               google_flights_url(origin, dest, dep, ret, cur)))
        if not demo and i < len(combos):
            time.sleep(cfg.get("pause_seconds", 4) + random.uniform(0, 3))

    print(f"Done: {ok} prices saved, {failed} errors.")
    if alerts:
        alerts.sort()
        print("\nALERTS:")
        for _, msg, _ in alerts:
            print("  " + msg)
        notify(cfg, alerts)
    build_report(cfg)
    return 0 if ok else 1


def notify(cfg, alerts):
    topic = cfg["alerts"].get("ntfy_topic")
    if not topic:
        return
    body = "\n".join(msg for _, msg, _ in alerts[:10])
    if len(alerts) > 10:
        body += f"\n…and {len(alerts) - 10} more"
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode(), method="POST",
        headers={"Title": f"Sapporo fares: {len(alerts)} deal(s), best {alerts[0][0]:,}",
                 "Tags": "airplane,snowflake", "Click": alerts[0][2]})
    try:
        urllib.request.urlopen(req, timeout=20)
        print(f"Notification sent to ntfy.sh/{topic}")
    except Exception as e:
        print(f"Notification failed: {e}")


# ───────────────────────── report ─────────────────────────
SERIES = {0: "var(--s1)", 1: "var(--s2)"}
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"]  # light→dark = pricier→cheaper


def build_report(cfg):
    con = db()
    cur, dest = cfg.get("currency", "USD"), cfg["destination"]
    rows = con.execute("SELECT run_at, origin, depart, ret, price, airlines, stops FROM prices WHERE dest=? AND currency=? ORDER BY run_at",
                       (dest, cur)).fetchall()
    if not rows:
        REPORT_PATH.write_text("<p>No data yet — run <code>python flight_watch.py run</code>.</p>")
        return
    latest_run = rows[-1][0]
    runs = sorted({r[0] for r in rows})
    prev_run = runs[-2] if len(runs) > 1 else None
    by = {}  # (origin, dep, ret) -> {run: (price, airlines, stops)}
    for run_at, o, d, r, p, a, s in rows:
        by.setdefault((o, d, r), {})[run_at] = (p, a, s)

    # headline: cheapest right now per origin
    origins = cfg["origins"]
    latest = {k: v[latest_run] for k, v in by.items() if latest_run in v}
    wanted = {(d.isoformat(), r.isoformat()) for d, r in date_combos(cfg)}  # only the current config's dates
    latest = {k: v for k, v in latest.items() if (k[1], k[2]) in wanted}
    tiles = []
    for i, o in enumerate(origins):
        mine = [(v[0], k) for k, v in latest.items() if k[0] == o]
        if not mine:
            continue
        p, (_, d, r) = min(mine)
        hist_low = min(v[0] for k, h in by.items() if k[0] == o and (k[1], k[2]) in wanted for v in h.values())
        dd, rr = dt.date.fromisoformat(d), dt.date.fromisoformat(r)
        tiles.append(f"""<a class="tile" href="{html.escape(google_flights_url(o, dest, dd, rr, cur))}" target="_blank">
          <div class="tlabel"><span class="sw" style="background:{SERIES[i % 2]}"></span>Cheapest from {o} now</div>
          <div class="big">{cur} {p:,}</div>
          <div class="sub">{dd:%a %b %d} → {rr:%a %b %d} · {(rr - dd).days} nights · {html.escape(latest[(o, d, r)][1])}</div>
          <div class="sub">Lowest seen: {cur} {hist_low:,}</div></a>""")

    # heatmap grids
    deps = sorted({d for d, _ in wanted})
    rets = sorted({r for _, r in wanted})
    vals = [v[0] for v in latest.values()] or [0]
    lo, hi = min(vals), max(vals)

    def shade(p):
        t = 0 if hi == lo else (hi - p) / (hi - lo)  # 1 = cheapest
        return SEQ[min(int(t * len(SEQ)), len(SEQ) - 1)]

    grids = []
    for o in origins:
        trs = []
        for d in deps:
            dd = dt.date.fromisoformat(d)
            tds = []
            for r in rets:
                rr = dt.date.fromisoformat(r)
                n = (rr - dd).days
                v = latest.get((o, d, r))
                if not v:
                    tds.append('<td class="na">—</td>')
                    continue
                p = v[0]
                prev = by[(o, d, r)].get(prev_run) if prev_run else None
                delta = ""
                if prev:
                    diff = p - prev[0]
                    if diff:
                        delta = f'<span class="d {"dn" if diff < 0 else "up"}">{"▼" if diff < 0 else "▲"}{abs(diff):,}</span>'
                bg = shade(p)
                ink = "#fff" if SEQ.index(bg) >= 3 else "#0b0b0b"
                tip = f"{o} {dd:%a %b %d} → {rr:%a %b %d} ({n} nights) · {cur} {p:,} · {v[1]} · {v[2]} stop(s)"
                url = google_flights_url(o, dest, dd, rr, cur)
                tds.append(f'<td style="background:{bg};color:{ink}" title="{html.escape(tip)}">'
                           f'<a href="{html.escape(url)}" target="_blank" style="color:{ink}">{p:,}</a>{delta}</td>')
            trs.append(f"<tr><th>{dd:%a %b %d}</th>{''.join(tds)}</tr>")
        head = "".join(f"<th>{dt.date.fromisoformat(r):%a}<br>{dt.date.fromisoformat(r):%b %d}</th>" for r in rets)
        grids.append(f'<div class="card"><h2>{o} → {dest}</h2><div class="scroll"><table class="grid"><tr><th class="corner">Depart ↓<br>Return →</th>{head}</tr>{"".join(trs)}</table></div></div>')

    # trend chart: cheapest itinerary per origin per run
    trend = {o: [] for o in origins}
    for run_at in runs:
        for o in origins:
            ps = [h[run_at][0] for k, h in by.items() if k[0] == o and run_at in h and (k[1], k[2]) in wanted]
            if ps:
                trend[o].append((run_at, min(ps)))
    chart = trend_svg(trend, origins, cur)

    REPORT_PATH.write_text(PAGE.format(
        title="Sapporo Fare Watch", dest=dest, cur=cur,
        updated=dt.datetime.fromisoformat(latest_run).astimezone().strftime("%a %b %d, %I:%M %p"),
        nruns=len(runs), tiles="".join(tiles), grids="".join(grids), chart=chart,
        stops=("any" if cfg.get("max_stops") is None else f"≤{cfg['max_stops']}"),
        adults=cfg.get("adults", 1)))
    print(f"Report written: {REPORT_PATH}")


def trend_svg(trend, origins, cur):
    pts = [p for s in trend.values() for p in s]
    if len({p[0] for p in pts}) < 2:
        return '<p class="muted">The trend line appears after the second check.</p>'
    W, H, L, R, T, B = 720, 240, 56, 70, 14, 30
    times = sorted({p[0] for p in pts})
    t0, t1 = dt.datetime.fromisoformat(times[0]), dt.datetime.fromisoformat(times[-1])
    span = max((t1 - t0).total_seconds(), 1)
    ys = [p[1] for p in pts]
    ymin, ymax = min(ys), max(ys)
    pad = max((ymax - ymin) * 0.15, 25)
    ymin, ymax = ymin - pad, ymax + pad
    X = lambda t: L + (dt.datetime.fromisoformat(t) - t0).total_seconds() / span * (W - L - R)
    Y = lambda v: T + (ymax - v) / (ymax - ymin) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" class="trend" role="img" aria-label="Cheapest fare per origin over time">']
    for k in range(5):
        v = ymin + (ymax - ymin) * k / 4
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="gridl"/>'
                   f'<text x="{L - 8}" y="{Y(v) + 4:.1f}" class="ax" text-anchor="end">{v:,.0f}</text>')
    for t in (times[0], times[-1]):
        out.append(f'<text x="{X(t):.1f}" y="{H - 8}" class="ax" text-anchor="middle">'
                   f'{dt.datetime.fromisoformat(t).astimezone():%b %d}</text>')
    for i, o in enumerate(origins):
        s = trend[o]
        if not s:
            continue
        col = SERIES[i % 2]
        d = " ".join(f"{'M' if j == 0 else 'L'}{X(t):.1f},{Y(v):.1f}" for j, (t, v) in enumerate(s))
        out.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2" stroke-linejoin="round"/>')
        for t, v in s:
            out.append(f'<circle cx="{X(t):.1f}" cy="{Y(v):.1f}" r="4" fill="{col}" stroke="var(--bg)" stroke-width="2">'
                       f'<title>{o} · {dt.datetime.fromisoformat(t).astimezone():%b %d %H:%M} · {cur} {v:,}</title></circle>')
        lt, lv = s[-1]
        out.append(f'<text x="{X(lt) + 8:.1f}" y="{Y(lv) + 4:.1f}" class="lbl">{o} {lv:,}</text>')
    out.append("</svg>")
    legend = "".join(f'<span class="leg"><span class="sw" style="background:{SERIES[i % 2]}"></span>{o}</span>'
                     for i, o in enumerate(origins))
    return f'<div class="legend">{legend}</div>' + "".join(out)


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#fcfcfb;--card:#fff;--ink:#0b0b0b;--ink2:#52514e;--line:#e6e5e0;--s1:#2a78d6;--s2:#eb6834;--dn:#0a7f0a;--up:#b42f2f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1a1a19;--card:#222220;--ink:#fff;--ink2:#c3c2b7;--line:#383835;--s1:#3987e5;--s2:#d95926;--dn:#5fd35f;--up:#ff8a8a}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:960px;margin:0 auto;padding:24px 16px 48px}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:16px;margin:0 0 12px}}
.muted,.sub{{color:var(--ink2)}}.sub{{font-size:13px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:20px 0}}
.tile,.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}}
.tile{{text-decoration:none;color:inherit;display:block}}.tile:hover{{border-color:var(--ink2)}}
.tlabel{{font-size:13px;color:var(--ink2)}}.big{{font-size:32px;font-weight:650;margin:2px 0 4px;font-variant-numeric:tabular-nums}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:0}}
.grids{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}}
.grid{{border-collapse:separate;border-spacing:2px;width:100%;font-variant-numeric:tabular-nums}}
.grid th{{font-weight:500;font-size:13px;color:var(--ink2);text-align:left;padding:4px 6px;white-space:nowrap}}
.grid td{{border-radius:4px;padding:6px 8px;text-align:right;white-space:nowrap}}.grid td a{{text-decoration:none;font-weight:600}}
.scroll{{overflow-x:auto}}.grid th.corner{{font-size:11px;line-height:1.3}}.grid td.na{{color:var(--ink2);background:transparent}}
.d{{font-size:11px;margin-left:6px;padding:0 4px;border-radius:3px;background:var(--card)}}.d.dn{{color:var(--dn)}}.d.up{{color:var(--up)}}
.trend{{width:100%;height:auto}}.gridl{{stroke:var(--line);stroke-width:1}}.ax{{fill:var(--ink2);font-size:11px}}.lbl{{fill:var(--ink);font-size:12px;font-weight:600}}
.legend{{display:flex;gap:16px;font-size:13px;color:var(--ink2);margin-bottom:6px}}
.key{{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px;color:var(--ink2);margin:10px 0 0}}.key i{{width:22px;height:10px;border-radius:2px;display:inline-block}}
section{{margin-top:24px}}
</style></head><body><main>
<h1>{title}</h1>
<div class="muted">Round trip to {dest} · {adults} adult · stops {stops} · prices in {cur} · updated {updated} · {nruns} check(s) so far</div>
<div class="tiles">{tiles}</div>
<section><div class="grids">{grids}</div>
<div class="key">Pricier <i style="background:#cde2fb"></i><i style="background:#9ec5f4"></i><i style="background:#6da7ec"></i><i style="background:#3987e5"></i><i style="background:#256abf"></i><i style="background:#184f95"></i> Cheaper · ▼▲ change since previous check · click any fare to open Google Flights</div></section>
<section class="card"><h2>Cheapest fare over time</h2>{chart}</section>
</main></body></html>"""


# ───────────────────────── cli ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "report"])
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--demo", action="store_true", help="use fake prices (no network)")
    a = ap.parse_args()
    cfg = load_config(Path(a.config))
    if a.demo:  # keep fake data away from your real history
        global DB_PATH, REPORT_PATH
        DB_PATH, REPORT_PATH = HERE / "demo_prices.db", HERE / "demo_report.html"
    if a.command == "report":
        build_report(cfg)
        return 0
    return run(cfg, demo=a.demo)


if __name__ == "__main__":
    sys.exit(main())
