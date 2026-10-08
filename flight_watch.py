#!/usr/bin/env python3
"""
Flight Watch — track fares across a grid of dates and alert on drops.

Supports a normal round trip, or an "open-jaw" trip: fly into one city (Sapporo) and home
from another (Tokyo). Open-jaw trips are priced two ways — one multi-city ticket, and two
separate one-way tickets — and the cheaper wins.

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
from zoneinfo import ZoneInfo

import yaml

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "prices.db"
REPORT_PATH = HERE / "report.html"
TZ = ZoneInfo("America/Los_Angeles")  # overridden by `timezone` in config.yaml

# A search is (kind, legs). kind: "rt" round trip, "mc" multi-city (one ticket), "ow" one-way.
# legs: tuple of (from, to, date) — date as ISO string.
TRIP = {"rt": "round-trip", "mc": "multi-city", "ow": "one-way"}


# ───────────────────────── config ─────────────────────────
def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    cfg["origins"] = [o.upper() for o in cfg["origins"]]
    cfg["destination"] = cfg["destination"].upper()
    cfg["fly_home_from"] = [a.upper() for a in (cfg.get("fly_home_from") or [])]
    for k in ("depart_from", "depart_to", "return_from", "return_to"):
        if isinstance(cfg.get(k), str):
            cfg[k] = dt.date.fromisoformat(cfg[k])
    cfg.setdefault("alerts", {})
    global TZ
    TZ = ZoneInfo(cfg.get("timezone", "America/Los_Angeles"))
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


def key_str(search) -> str:
    kind, legs = search
    return kind + ":" + "|".join(f"{a}-{b}-{d}" for a, b, d in legs)


def options_for(cfg, origin, dep, ret):
    """Ways to buy one trip. Each option = (label, [searches]); its price is the sum of the searches."""
    dest, d, r = cfg["destination"], dep.isoformat(), ret.isoformat()
    homes = cfg["fly_home_from"]
    if not homes:  # plain round trip
        return [("Round trip", [("rt", ((origin, dest, d), (dest, origin, r)))])]
    opts = []
    for h in homes:
        opts.append((f"1 ticket · home from {h}", [("mc", ((origin, dest, d), (h, origin, r)))]))
    if cfg.get("compare_one_ways", True):
        for h in homes:
            opts.append((f"2 one-ways · home from {h}",
                         [("ow", ((origin, dest, d),)), ("ow", ((h, origin, r),))]))
    return opts


def all_searches(cfg):
    seen = {}
    for o in cfg["origins"]:
        for d, r in date_combos(cfg):
            for _, searches in options_for(cfg, o, d, r):
                for s in searches:
                    seen.setdefault(key_str(s), s)
    # one-ways first (fewer, and reused by many date pairs), then tickets
    return sorted(seen.values(), key=lambda s: (s[0] != "ow", key_str(s)))


def google_flights_url(search, cfg) -> str:
    """Exact Google Flights search for these legs, trip type, stops, cabin and passengers."""
    kind, legs = search
    cur = cfg.get("currency", "USD")
    try:
        from fast_flights import FlightQuery, Passengers, create_query
        ms = cfg.get("max_stops")
        q = create_query(
            flights=[FlightQuery(date=d, from_airport=a, to_airport=b, max_stops=ms) for a, b, d in legs],
            trip=TRIP[kind], seat=cfg.get("seat", "economy"),
            passengers=Passengers(adults=int(cfg.get("adults", 1))),
            currency=cur, language="en-US",
        )
        return q.url()
    except Exception:  # library missing or changed: fall back to a plain text search
        a, b, d = legs[0]
        q = f"Flights from {a} to {b} on {d}" + (f" through {legs[-1][2]}" if kind == "rt" else "")
        return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q, "curr": cur})


# ───────────────────────── providers ─────────────────────────
# Each returns the cheapest offer for one search: {"price", "airlines", "stops"} or None.

def search_google(cfg, search):
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    kind, legs = search
    ms = cfg.get("max_stops")
    q = create_query(
        flights=[FlightQuery(date=d, from_airport=a, to_airport=b, max_stops=ms) for a, b, d in legs],
        trip=TRIP[kind],
        seat=cfg.get("seat", "economy"),
        passengers=Passengers(adults=int(cfg.get("adults", 1))),
        currency=cfg.get("currency", "USD"),
        language="en-US",
    )
    try:
        results = get_flights(q)
    except Exception as e:
        if type(e).__name__ == "FlightsNotFound":
            return None
        raise
    offers = [f for f in results if getattr(f, "price", 0)]
    if not offers:
        return None
    best = min(offers, key=lambda f: f.price)
    return {"price": int(best.price), "airlines": ", ".join(dict.fromkeys(best.airlines)),
            "stops": max(len(best.flights) - 1, 0)}


SERP_STOPS = {None: 0, 0: 1, 1: 2, 2: 3}
SERP_CLASS = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}
SERP_TYPE = {"rt": 1, "ow": 2, "mc": 3}


def search_serpapi(cfg, search):
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        sys.exit("provider is 'serpapi' but SERPAPI_KEY is not set")
    kind, legs = search
    params = {
        "engine": "google_flights", "api_key": key, "type": SERP_TYPE[kind], "hl": "en",
        "currency": cfg.get("currency", "USD"), "adults": cfg.get("adults", 1),
        "stops": SERP_STOPS.get(cfg.get("max_stops"), 0),
        "travel_class": SERP_CLASS.get(cfg.get("seat", "economy"), 1),
    }
    if kind == "mc":
        params["multi_city_json"] = json.dumps([{"departure_id": a, "arrival_id": b, "date": d} for a, b, d in legs])
    else:
        params.update(departure_id=legs[0][0], arrival_id=legs[0][1], outbound_date=legs[0][2])
        if kind == "rt":
            params["return_date"] = legs[1][2]
    with urllib.request.urlopen("https://serpapi.com/search.json?" + urllib.parse.urlencode(params), timeout=60) as r:
        data = json.load(r)
    if "error" in data and "no results" not in data["error"].lower():
        raise RuntimeError(data["error"])
    offers = [o for o in data.get("best_flights", []) + data.get("other_flights", []) if o.get("price")]
    if not offers:
        return None
    best = min(offers, key=lambda o: o["price"])
    return {"price": int(best["price"]),
            "airlines": ", ".join(dict.fromkeys(s.get("airline", "?") for s in best.get("flights", []))),
            "stops": len(best.get("layovers", []))}


def search_demo(cfg, search, _state={}):
    """Fake fares that wander between runs."""
    kind, legs = search
    seed = zlib.crc32(key_str(search).encode()) % 997
    drift = _state.setdefault("drift", random.uniform(-80, 50))
    base = {"rt": 1150, "mc": 1180, "ow": 640}[kind] + (60 if legs[0][0] == "YVR" else 0)
    price = base + seed % 260 + drift + random.uniform(-40, 40)
    if kind == "ow":
        price = price * 0.95
    return {"price": int(price), "airlines": random.choice(["ANA", "JAL", "Delta", "Air Canada", "Korean Air"]), "stops": 1}


PROVIDERS = {"google": search_google, "serpapi": search_serpapi, "demo": search_demo}


# ───────────────────────── storage ─────────────────────────
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS quotes(
        run_at TEXT, search TEXT, price INTEGER, currency TEXT, airlines TEXT, stops INTEGER)""")
    con.execute("CREATE INDEX IF NOT EXISTS ixq ON quotes(run_at, search)")
    return con


def load_runs(con, cur):
    """{run_at: {search_key: (price, airlines, stops)}} in time order."""
    runs = {}
    for run_at, s, p, a, st in con.execute(
            "SELECT run_at, search, price, airlines, stops FROM quotes WHERE currency=? ORDER BY run_at", (cur,)):
        runs.setdefault(run_at, {})[s] = (p, a, st)
    return runs


def best_per_trip(cfg, quotes):
    """{(origin, dep, ret): best} where best = {price, label, parts:[(search, price, airlines)]}."""
    out = {}
    for o in cfg["origins"]:
        for d, r in date_combos(cfg):
            best = None
            for label, searches in options_for(cfg, o, d, r):
                got = [quotes.get(key_str(s)) for s in searches]
                if not all(got):
                    continue
                total = sum(g[0] for g in got)
                if best is None or total < best["price"]:
                    best = {"price": total, "label": label,
                            "parts": [(s, g[0], g[1]) for s, g in zip(searches, got)]}
            if best:
                out[(o, d.isoformat(), r.isoformat())] = best
    return out


# ───────────────────────── run ─────────────────────────
def run(cfg, demo=False):
    search_fn = PROVIDERS["demo" if demo else cfg.get("provider", "google")]
    con = db()
    cur = cfg.get("currency", "USD")
    run_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    searches = all_searches(cfg)
    print(f"Running {len(searches)} searches ({run_at})")
    ok = failed = 0

    for i, s in enumerate(searches, 1):
        kind, legs = s
        label = f"{TRIP[kind]:>10}  " + "  +  ".join(f"{a}→{b} {dt.date.fromisoformat(d):%b %d}" for a, b, d in legs)
        try:
            offer = search_fn(cfg, s)
        except ImportError as e:
            sys.exit(f"Setup problem, stopping: {e}. Run: pip install -r requirements.txt")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(searches)}] {label}: ERROR {type(e).__name__}: {str(e)[:120]}")
            offer = None
        if offer:
            con.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?)",
                        (run_at, key_str(s), offer["price"], cur, offer["airlines"], offer["stops"]))
            con.commit()
            ok += 1
            print(f"  [{i}/{len(searches)}] {label}: {cur} {offer['price']:,}  {offer['airlines']}")
        if not demo and i < len(searches):
            time.sleep(cfg.get("pause_seconds", 4) + random.uniform(0, 3))

    print(f"Done: {ok} prices saved, {failed} errors.")

    # Alerts are based on the best way to buy each trip, compared with the previous check.
    runs = load_runs(con, cur)
    order = list(runs)
    now = best_per_trip(cfg, runs.get(run_at, {}))
    prev = best_per_trip(cfg, runs[order[-2]]) if len(order) > 1 else {}
    al, alerts = cfg["alerts"], []
    for (o, d, r), b in sorted(now.items(), key=lambda kv: kv[1]["price"]):
        p, why = b["price"], []
        if al.get("target_price") and p <= al["target_price"]:
            why.append(f"under target {al['target_price']:,}")
        pb = prev.get((o, d, r))
        if pb and al.get("drop_percent") and (pb["price"] - p) / pb["price"] * 100 >= al["drop_percent"]:
            why.append(f"down {pb['price'] - p:,} from {pb['price']:,}")
        if why:
            dd, rr = dt.date.fromisoformat(d), dt.date.fromisoformat(r)
            alerts.append((p, f"{o} {dd:%b %d}–{rr:%b %d}: {cur} {p:,} ({b['label']}) — {', '.join(why)}",
                           google_flights_url(b["parts"][0][0], cfg)))
    if alerts:
        print("\nALERTS:")
        for _, msg, _ in alerts:
            print("  " + msg)
        notify(cfg, alerts)
    build_report(cfg)
    return 0 if ok else 1


def notify(cfg, alerts):
    topic = os.environ.get("NTFY_TOPIC") or cfg["alerts"].get("ntfy_topic")
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
        print("Notification sent")
    except Exception as e:
        print(f"Notification failed: {e}")


# ───────────────────────── report ─────────────────────────
SERIES = {0: "var(--s1)", 1: "var(--s2)"}
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"]  # light→dark = pricier→cheaper
E = html.escape


def fmt_day(iso):
    return f"{dt.date.fromisoformat(iso):%a %b %d}"


def tag_for(label):
    if label.startswith("1 ticket"):
        return label.split()[-1]          # HND / NRT
    if label.startswith("2 one-ways"):
        return "2×" + label.split()[-1]   # 2×NRT
    return ""


def build_report(cfg):
    con = db()
    cur, dest = cfg.get("currency", "USD"), cfg["destination"]
    homes = cfg["fly_home_from"]
    runs = load_runs(con, cur)
    order = list(runs)
    if not order:
        REPORT_PATH.write_text("<p>No data yet — run <code>python flight_watch.py run</code>.</p>")
        return
    latest_run = order[-1]
    quotes = runs[latest_run]
    best = best_per_trip(cfg, quotes)
    prev = best_per_trip(cfg, runs[order[-2]]) if len(order) > 1 else {}
    history = [best_per_trip(cfg, runs[k]) for k in order]
    origins = cfg["origins"]
    combos = [(d.isoformat(), r.isoformat()) for d, r in date_combos(cfg)]
    deps = sorted({d for d, _ in combos})
    rets = sorted({r for _, r in combos})

    def part_links(b, cls="leg"):
        bits = []
        for s, p, a in b["parts"]:
            kind, legs = s
            route = " + ".join(f"{x}→{y}" for x, y, _ in legs)
            bits.append(f'<a class="{cls}" href="{E(google_flights_url(s, cfg))}" target="_blank" rel="noopener">'
                        f'{route} · {cur} {p:,} · {E(a)} ↗</a>')
        return "".join(bits)

    # headline tiles: cheapest trip per origin
    tiles = []
    for i, o in enumerate(origins):
        mine = [(b["price"], k, b) for k, b in best.items() if k[0] == o]
        if not mine:
            continue
        p, (_, d, r), b = min(mine, key=lambda t: t[0])
        lows = [h[(o, dd, rr)]["price"] for h in history for (oo, dd, rr) in h if oo == o and (dd, rr) in combos]
        tiles.append(f"""<div class="tile">
          <div class="tlabel"><span class="sw" style="background:{SERIES[i % 2]}"></span>Cheapest from {o} now</div>
          <div class="big">{cur} {p:,}</div>
          <div class="sub">{fmt_day(d)} → {fmt_day(r)} · {E(b['label'])}</div>
          <div class="legs">{part_links(b)}</div>
          <div class="sub">Lowest seen: {cur} {min(lows):,}</div></div>""")

    # heatmap per origin: best way to buy each date pair
    vals = [b["price"] for b in best.values()] or [0]
    lo, hi = min(vals), max(vals)

    def shade(p):
        t = 0 if hi == lo else (hi - p) / (hi - lo)
        return SEQ[min(int(t * len(SEQ)), len(SEQ) - 1)]

    route = f"{dest}, home from {'/'.join(homes)}" if homes else dest
    grids = []
    for o in origins:
        trs = []
        for d in deps:
            tds = []
            for r in rets:
                b = best.get((o, d, r))
                if (d, r) not in combos or not b:
                    tds.append('<td class="na">—</td>')
                    continue
                p = b["price"]
                delta = ""
                pb = prev.get((o, d, r))
                if pb and pb["price"] != p:
                    diff = p - pb["price"]
                    delta = f'<span class="d {"dn" if diff < 0 else "up"}">{"▼" if diff < 0 else "▲"}{abs(diff):,}</span>'
                bg = shade(p)
                ink = "#fff" if SEQ.index(bg) >= 3 else "#0b0b0b"
                tip = f"{o} · {fmt_day(d)} → {fmt_day(r)} · {cur} {p:,} · {b['label']}: " + \
                      " + ".join(f"{cur} {pp:,} {a}" for _, pp, a in b["parts"])
                if len(b["parts"]) > 1:
                    tip += " — opens the flight to Sapporo; the flight home is in the one-way table below"
                url = google_flights_url(b["parts"][0][0], cfg)
                tag = tag_for(b["label"])
                meta = " ".join(x for x in (delta, f'<span class="tag">{E(tag)}</span>' if tag else "") if x)
                tds.append(f'<td style="background:{bg}"><a class="cell" href="{E(url)}" target="_blank" rel="noopener" '
                           f'title="{E(tip)}" style="color:{ink}">{p:,}<span class="meta">{meta or "&nbsp;"}</span></a></td>')
            dd = dt.date.fromisoformat(d)
            trs.append(f"<tr><th>{dd:%a}<br>{dd:%b %d}</th>{''.join(tds)}</tr>")
        head = "".join(f"<th>{dt.date.fromisoformat(r):%a}<br>{dt.date.fromisoformat(r):%b %d}</th>" for r in rets)
        grids.append(f'<div class="card"><h2>{o} → {route} → {o}</h2><div class="scroll"><table class="grid">'
                     f'<tr><th class="corner">Depart ↓<br>Return →</th>{head}</tr>{"".join(trs)}</table></div></div>')

    # one-way legs (only for open-jaw with comparison on)
    oneways = ""
    if homes and cfg.get("compare_one_ways", True):
        cards = []
        for o in origins:
            def ow_cell(a, b, d):
                s = ("ow", ((a, b, d),))
                q = quotes.get(key_str(s))
                if not q:
                    return '<td class="na">—</td>'
                return (f'<td class="ow"><a href="{E(google_flights_url(s, cfg))}" target="_blank" rel="noopener" '
                        f'title="{E(q[1])} · {q[2]} stop(s)">{q[0]:,}</a></td>')
            out_rows = "".join(f"<tr><th>{fmt_day(d)}</th>{ow_cell(o, dest, d)}</tr>" for d in deps)
            home_head = "".join(f"<th>from {h}</th>" for h in homes)
            home_rows = "".join(f"<tr><th>{fmt_day(r)}</th>{''.join(ow_cell(h, o, r) for h in homes)}</tr>" for r in rets)
            cards.append(f"""<div class="card"><h2>{o} one-way tickets</h2><div class="owgrid">
              <table class="grid"><tr><th>To {dest}</th><th></th></tr>{out_rows}</table>
              <table class="grid"><tr><th>Home to {o}</th>{home_head}</tr>{home_rows}</table></div></div>""")
        oneways = (f'<section><h2 class="sec">One-way tickets</h2><p class="muted small">Used for the "2×" prices above. '
                   f'Click a price to open that flight in Google Flights.</p><div class="grids">{"".join(cards)}</div></section>')

    # trend: cheapest trip per origin per check
    trend = {o: [] for o in origins}
    for run_at, h in zip(order, history):
        for o in origins:
            ps = [b["price"] for k, b in h.items() if k[0] == o]
            if ps:
                trend[o].append((run_at, min(ps)))
    chart = trend_svg(trend, origins, cur)

    if homes:
        sub = f"Into {dest}, home from {' or '.join(homes)}"
        key_extra = (f" · <b>{'/'.join(homes)}</b> = one multi-city ticket flying home from that airport"
                     f" · <b>2×</b> = two one-way tickets (cheaper that day)")
    else:
        sub, key_extra = f"Round trip to {dest}", ""
    REPORT_PATH.write_text(PAGE.format(
        title="Sapporo Fare Watch", sub=sub, cur=cur,
        updated=dt.datetime.fromisoformat(latest_run).astimezone(TZ).strftime("%a %b %d, %I:%M %p"),
        nruns=len(order), tiles="".join(tiles), grids="".join(grids), chart=chart, oneways=oneways,
        key_extra=key_extra,
        stops=("any" if cfg.get("max_stops") is None else f"≤{cfg['max_stops']} per flight"),
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
    out = [f'<svg viewBox="0 0 {W} {H}" class="trend" role="img" aria-label="Cheapest trip per origin over time">']
    for k in range(5):
        v = ymin + (ymax - ymin) * k / 4
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="gridl"/>'
                   f'<text x="{L - 8}" y="{Y(v) + 4:.1f}" class="ax" text-anchor="end">{v:,.0f}</text>')
    for t in (times[0], times[-1]):
        out.append(f'<text x="{X(t):.1f}" y="{H - 8}" class="ax" text-anchor="middle">'
                   f'{dt.datetime.fromisoformat(t).astimezone(TZ):%b %d}</text>')
    for i, o in enumerate(origins):
        s = trend[o]
        if not s:
            continue
        col = SERIES[i % 2]
        d = " ".join(f"{'M' if j == 0 else 'L'}{X(t):.1f},{Y(v):.1f}" for j, (t, v) in enumerate(s))
        out.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2" stroke-linejoin="round"/>')
        for t, v in s:
            out.append(f'<circle cx="{X(t):.1f}" cy="{Y(v):.1f}" r="4" fill="{col}" stroke="var(--bg)" stroke-width="2">'
                       f'<title>{o} · {dt.datetime.fromisoformat(t).astimezone(TZ):%b %d %I:%M %p} · {cur} {v:,}</title></circle>')
        lt, lv = s[-1]
        out.append(f'<text x="{X(lt) + 8:.1f}" y="{Y(lv) + 4:.1f}" class="lbl">{o} {lv:,}</text>')
    out.append("</svg>")
    legend = "".join(f'<span class="leg"><span class="sw" style="background:{SERIES[i % 2]}"></span>{o}</span>'
                     for i, o in enumerate(origins))
    return f'<div class="legend">{legend}</div>' + "".join(out)


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="robots" content="noindex"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#fcfcfb;--card:#fff;--ink:#0b0b0b;--ink2:#52514e;--line:#e6e5e0;--s1:#2a78d6;--s2:#eb6834;--dn:#0a7f0a;--up:#b42f2f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1a1a19;--card:#222220;--ink:#fff;--ink2:#c3c2b7;--line:#383835;--s1:#3987e5;--s2:#d95926;--dn:#5fd35f;--up:#ff8a8a}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:960px;margin:0 auto;padding:24px 16px 48px}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:16px;margin:0 0 12px}}h2.sec{{margin:0 0 4px}}
.muted,.sub{{color:var(--ink2)}}.sub{{font-size:13px}}.small{{font-size:13px;margin:0 0 10px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:20px 0}}
.tile,.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}}
.tlabel{{font-size:13px;color:var(--ink2)}}.big{{font-size:32px;font-weight:650;margin:2px 0 4px;font-variant-numeric:tabular-nums}}
.legs{{display:flex;flex-direction:column;gap:2px;margin:6px 0}}a.leg{{font-size:13px;color:var(--s1);text-decoration:none}}a.leg:hover{{text-decoration:underline}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:0}}
.grids{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}}
.grid{{border-collapse:separate;border-spacing:2px;width:100%;font-variant-numeric:tabular-nums}}
.grid th{{font-weight:500;font-size:13px;color:var(--ink2);text-align:left;padding:4px 6px;white-space:nowrap}}
.grid td{{border-radius:4px;padding:0;text-align:right;white-space:nowrap}}
.grid td a.cell{{display:block;padding:6px 8px;border-radius:4px;text-decoration:none;font-weight:600;outline-offset:-2px}}
.grid td a.cell:hover,.grid td a.cell:focus-visible{{outline:2px solid var(--ink)}}
.grid td.na{{padding:6px 8px;color:var(--ink2);background:transparent}}
.meta{{display:flex;justify-content:flex-end;align-items:center;gap:4px;margin-top:1px;min-height:15px}}.tag{{font-size:10px;font-weight:500;opacity:.9;letter-spacing:.02em}}.meta .d{{margin-left:0}}
.grid td.ow{{background:transparent;border:1px solid var(--line)}}.grid td.ow a{{display:block;padding:5px 8px;color:var(--ink);text-decoration:none;font-weight:600}}.grid td.ow a:hover{{color:var(--s1)}}
.owgrid{{display:grid;grid-template-columns:1fr 1.6fr;gap:12px;align-items:start}}
.scroll{{overflow-x:auto}}.grid th.corner{{font-size:11px;line-height:1.3}}
.d{{font-size:10px;margin-left:6px;padding:0 3px;border-radius:3px;background:var(--card)}}.d.dn{{color:var(--dn)}}.d.up{{color:var(--up)}}
.trend{{width:100%;height:auto}}.gridl{{stroke:var(--line);stroke-width:1}}.ax{{fill:var(--ink2);font-size:11px}}.lbl{{fill:var(--ink);font-size:12px;font-weight:600}}
.legend{{display:flex;gap:16px;font-size:13px;color:var(--ink2);margin-bottom:6px}}
.key{{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px;color:var(--ink2);margin:10px 0 0}}.key i{{width:22px;height:10px;border-radius:2px;display:inline-block}}
section{{margin-top:24px}}
@media (max-width:520px){{.owgrid{{grid-template-columns:1fr}}.card{{padding:12px}}.grid td a.cell{{padding:5px 6px}}.grid th{{padding:4px 3px}}}}
</style></head><body><main>
<h1>{title}</h1>
<div class="muted">{sub} · {adults} adult · stops {stops} · prices in {cur} · updated {updated} · {nruns} check(s) so far</div>
<div class="tiles">{tiles}</div>
<section><div class="grids">{grids}</div>
<div class="key">Pricier <i style="background:#cde2fb"></i><i style="background:#9ec5f4"></i><i style="background:#6da7ec"></i><i style="background:#3987e5"></i><i style="background:#256abf"></i><i style="background:#184f95"></i> Cheaper · total trip price · ▼▲ change since previous check{key_extra} · click a price to open it in Google Flights</div></section>
{oneways}
<section class="card"><h2>Cheapest trip over time</h2>{chart}</section>
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
