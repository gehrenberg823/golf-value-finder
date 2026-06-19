#!/usr/bin/env python3
"""Golf value finder — upload simulation projections (CSV), compare to live Kalshi.

Local web app: click "Upload Projections CSV", pick the file, and it fetches fresh
Kalshi order-book prices for the current tournament and shows where your model
disagrees with the market (edge + EV%). Re-upload any time (e.g. between rounds).

Markets: outright winner, make cut, top 20, top 10, top 5.

Run:
    pip install flask requests
    python3 app.py          # then open http://127.0.0.1:5000
"""
from __future__ import annotations
import csv, io, os, re, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import requests
from flask import Flask, request, redirect, render_template_string

app = Flask(__name__)
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
HDR = {"User-Agent": "Mozilla/5.0"}

# DataGolf in-play predictions feed (probabilities for win / make cut / top N).
DATAGOLF_KEY = os.environ.get("DATAGOLF_KEY") or "6c6632b4c094506ccd5e06e1b6d9"
DATAGOLF_TOUR = {        # our tour name -> DataGolf tour code
    "PGA Tour": "pga", "LIV Golf": "liv",
    "DP World Tour": "euro", "Korn Ferry Tour": "kft",
}
DATAGOLF_COLS = ["player_name", "win", "make_cut", "top_5", "top_10", "top_20",
                 "current_pos", "current_score", "today", "thru", "round"]
SCORE_COLS = ["current_pos", "current_score", "today", "thru", "round"]

# CSV column -> display label (display order).
MARKET_DEFS = [
    ("win",      "Outright Winner"),
    ("make_cut", "Make the Cut"),
    ("top_20",   "Top 20"),
    ("top_10",   "Top 10"),
    ("top_5",    "Top 5"),
]

# Per-tour Kalshi series (only the markets that tour actually offers on Kalshi).
TOUR_SERIES = {
    "PGA Tour":        {"win": "KXPGATOUR", "make_cut": "KXPGAMAKECUT",
                        "top_20": "KXPGATOP20", "top_10": "KXPGATOP10", "top_5": "KXPGATOP5"},
    "LIV Golf":        {"win": "KXLIVTOUR", "top_10": "KXLIVTOP10", "top_5": "KXLIVTOP5"},
    "DP World Tour":   {"win": "KXDPWORLDTOUR", "make_cut": "KXDPWORLDTOURMAKECUT"},
    "Korn Ferry Tour": {"win": "KXKFTOUR"},
}
TOURS = list(TOUR_SERIES)


def tour_markets(tour):
    """[(col, series, label)] for a tour, in display order, only markets it offers."""
    s = TOUR_SERIES.get(tour, {})
    return [(col, s[col], label) for col, label in MARKET_DEFS if col in s]

# Latest result kept in memory so a page reload shows the last upload.
STATE = {"rows": None, "filename": None, "when": None, "event": None, "warnings": [],
         "tour": "PGA Tour", "csv_text": None}


# ------------------------------------------------------------- helpers --------
def _get(path, tries=4):
    delay = 0.4
    for _ in range(tries):
        try:
            r = requests.get(KALSHI + path, headers=HDR, timeout=25)
            if r.status_code == 429:
                time.sleep(delay); delay *= 2; continue
            return r.json()
        except Exception:
            time.sleep(delay); delay *= 2
    return {}


def fetch_datagolf(tour: str) -> str:
    """Pull DataGolf's in-play feed for a tour and return CSV text in the app's
    projection format (the same columns a manual upload would have)."""
    code = DATAGOLF_TOUR.get(tour)
    if not code:
        raise ValueError(f"DataGolf has no tour code for {tour!r}")
    url = (f"https://feeds.datagolf.com/preds/in-play?tour={code}"
           f"&dead_heat=no&odds_format=percent&key={DATAGOLF_KEY}")
    data = requests.get(url, headers=HDR, timeout=40).json()
    rows = data.get("data") if isinstance(data, dict) else data
    if not rows:
        raise ValueError(f"DataGolf returned no players for {tour} "
                         f"(no in-play tournament right now?)")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=DATAGOLF_COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c) for c in DATAGOLF_COLS})
    return buf.getvalue()


def norm_name(s: str) -> str:
    """'Scheffler, Scottie' or 'Scottie Scheffler' -> 'scottiescheffler'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    if "," in s:
        last, first = s.split(",", 1)
        s = f"{first.strip()} {last.strip()}"
    return re.sub(r"[^a-z]", "", s)


def current_event(series: str):
    """The open event ticker for a series (current tournament), or None."""
    evs = _get(f"/events?series_ticker={series}&status=open&limit=50").get("events", [])
    return evs[0]["event_ticker"] if evs else None


def orderbook_asks(ticker: str):
    """Executable buy prices from the order book: (yes_ask, no_ask).

    On Kalshi a NO bid at q is a YES ask at 1-q, so the price to BUY YES is
    1 - best_NO_bid, and the price to BUY NO is 1 - best_YES_bid. None if empty.
    """
    ob = _get(f"/markets/{ticker}/orderbook").get("orderbook_fp") or {}
    yes, no = ob.get("yes_dollars") or [], ob.get("no_dollars") or []
    best_yes_bid = max((float(p) for p, _ in yes), default=None)
    best_no_bid = max((float(p) for p, _ in no), default=None)
    yes_ask = (1.0 - best_no_bid) if best_no_bid is not None else None
    no_ask = (1.0 - best_yes_bid) if best_yes_bid is not None else None
    return yes_ask, no_ask


def kalshi_prices(series: str):
    """{norm_name: {price, ticker}} for the current event of a series (order-book priced)."""
    ev = current_event(series)
    if not ev:
        return {}, None
    mk = _get(f"/markets?event_ticker={ev}&limit=400").get("markets", [])
    players = [(norm_name(m.get("yes_sub_title", "")), m["ticker"])
               for m in mk if m.get("yes_sub_title")]
    out = {nm: {"yes_ask": None, "no_ask": None, "ticker": tkr} for nm, tkr in players}
    # Concurrent order-book fetch, with retry passes for players whose book comes
    # back empty (Kalshi rate-limits under concurrency, dropping ~5-7 per pass).
    todo = list(players)
    for _ in range(4):
        if not todo:
            break
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda p: orderbook_asks(p[1]), todo))
        retry = []
        for (nm, tkr), (yes_ask, no_ask) in zip(todo, results):
            if yes_ask is not None or no_ask is not None:
                out[nm] = {"yes_ask": yes_ask, "no_ask": no_ask, "ticker": tkr}
            else:
                retry.append((nm, tkr))
        todo = retry
        if todo:
            time.sleep(0.8)
    return out, ev


def market_url(series, event, ticker):
    return f"https://kalshi.com/markets/{series.lower()}/{event.lower()}?op_market_ticker={ticker}"


# ---------------------------------------------------------- core compute ------
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _rel(v):
    """Golf score relative to par: 0 -> 'E', +n / -n otherwise."""
    n = _num(v)
    if n is None:
        return None
    n = int(round(n))
    return "E" if n == 0 else (f"+{n}" if n > 0 else str(n))


def score_view(raw: dict | None):
    """Turn raw DataGolf score fields into display strings + numeric sort keys."""
    if not raw:
        return None
    pos = (raw.get("current_pos") or "").strip()
    total, today, thru = _num(raw.get("current_score")), _num(raw.get("today")), _num(raw.get("thru"))
    if not pos and total is None and thru is None:
        return None
    pos_v = _num(re.sub(r"[^0-9.]", "", pos)) if pos else None
    thru_d = ("F" if int(thru) >= 18 else str(int(thru))) if thru is not None else None
    return {
        "pos": pos or "—",            "pos_v": pos_v if pos_v is not None else 9999,
        "total_d": _rel(total) or "—", "total_v": total if total is not None else 9999,
        "today_d": _rel(today) or "—", "today_v": today if today is not None else 9999,
        "thru_d": thru_d or "—",      "thru_v": thru if thru is not None else -1,
    }


def build_rows(csv_text: str, tour: str):
    """Parse projections CSV + fetch Kalshi -> per-market sorted value rows."""
    markets = tour_markets(tour)
    reader = csv.DictReader(io.StringIO(csv_text))
    proj = {}   # norm_name -> {display, col: prob}
    for r in reader:
        nm = norm_name(r.get("player_name", ""))
        if not nm:
            continue
        disp = r["player_name"]
        if "," in disp:
            last, first = disp.split(",", 1)
            disp = f"{first.strip()} {last.strip()}"
        rec = {"display": disp}
        for col, _, _ in markets:
            try:
                rec[col] = float(r[col]) if r.get(col) not in (None, "") else None
            except (ValueError, KeyError):
                rec[col] = None
        rec["score"] = score_view({c: r.get(c) for c in SCORE_COLS})
        proj[nm] = rec

    warnings, event_label, by_market = [], None, []
    for col, series, label in markets:
        prices, ev = kalshi_prices(series)
        if ev and not event_label:
            event_label = ev.split("-")[-1]
        rows = []
        for nm, rec in proj.items():
            p = rec.get(col)
            if p is None:
                continue
            kinfo = prices.get(nm)
            link = market_url(series, ev, kinfo["ticker"]) if (kinfo and ev) else None
            # edge vs the actual price you'd PAY on each side
            cands = []
            if kinfo and kinfo["yes_ask"] is not None:
                cands.append(("Yes", kinfo["yes_ask"], p, p - kinfo["yes_ask"]))
            if kinfo and kinfo["no_ask"] is not None:
                cands.append(("No", kinfo["no_ask"], 1 - p, (1 - p) - kinfo["no_ask"]))
            side = price = edge = ev_pct = None
            if cands:
                side, price, side_fair, edge = max(cands, key=lambda c: c[3])
                ev_pct = (side_fair / price - 1.0) if price > 0 else None
            rows.append({"player": rec["display"], "proj": p, "side": side, "price": price,
                         "edge": edge, "ev_pct": ev_pct, "link": link,
                         "ticker": kinfo["ticker"] if kinfo else None,
                         "score": rec.get("score"), "key": nm})
        # sort: biggest executable edge first, unpriced last
        rows.sort(key=lambda x: (x["edge"] is None, -(x["edge"] or 0)))
        priced = sum(1 for r in rows if r["side"] is not None)
        event_url = (f"https://kalshi.com/markets/{series.lower()}/{ev.lower()}" if ev else None)
        by_market.append({"label": label, "series": series, "rows": rows, "event_url": event_url,
                          "priced": priced, "available": bool(prices)})
        if not prices:
            warnings.append(f"{label}: no Kalshi market open yet.")
    return by_market, event_label, warnings


# --------------------------------------------------------------- routes -------
@app.route("/", methods=["GET"])
def index():
    return render_template_string(PAGE, st=STATE, tours=TOURS)


@app.route("/scores", methods=["GET"])
def scores():
    """Live scores only — one DataGolf call, no Kalshi. Returns {norm_name: score_view}."""
    tour = request.args.get("tour", STATE.get("tour") or "PGA Tour")
    code = DATAGOLF_TOUR.get(tour)
    if not code:
        return {"scores": {}, "when": ""}
    url = (f"https://feeds.datagolf.com/preds/in-play?tour={code}"
           f"&dead_heat=no&odds_format=percent&key={DATAGOLF_KEY}")
    try:
        data = requests.get(url, headers=HDR, timeout=40).json()
    except Exception:
        return {"scores": {}, "when": ""}
    rows = data.get("data") if isinstance(data, dict) else data
    out = {}
    for r in (rows or []):
        nm = norm_name(r.get("player_name", ""))
        sv = score_view({c: r.get(c) for c in SCORE_COLS}) if nm else None
        if sv:
            out[nm] = sv
    when = (data.get("info") or {}).get("last_update", "") if isinstance(data, dict) else ""
    return {"scores": out, "when": when}


@app.route("/orderbook/<path:ticker>", methods=["GET"])
def orderbook(ticker):
    """Live order book for one market as executable buy ladders (best first).

    Kalshi stores resting bids: yes_dollars = YES bids, no_dollars = NO bids.
    A NO bid at q is a YES offer at 1-q, so the BUY-YES ladder is built from the
    NO bids (and BUY-NO from the YES bids). Prices returned in cents, best first.
    """
    ob = _get(f"/markets/{ticker}/orderbook").get("orderbook_fp") or {}
    # Each level is [price_dollars, resting_dollars] as strings.
    yes = ob.get("yes_dollars") or []   # YES bids
    no = ob.get("no_dollars") or []     # NO bids
    buy_yes = sorted(([round((1.0 - float(p)) * 100, 1), round(float(d))] for p, d in no),
                     key=lambda x: x[0])
    buy_no = sorted(([round((1.0 - float(p)) * 100, 1), round(float(d))] for p, d in yes),
                    key=lambda x: x[0])
    return {"buy_yes": buy_yes[:8], "buy_no": buy_no[:8]}


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("csv")
    if not f:
        return redirect("/")
    tour = request.form.get("tour", "PGA Tour")
    if tour not in TOUR_SERIES:
        tour = "PGA Tour"
    text = f.read().decode("utf-8", "replace")
    rows, event_label, warnings = build_rows(text, tour)
    STATE.update(rows=rows, filename=f.filename, event=event_label, tour=tour, csv_text=text,
                 when=datetime.now().astimezone().strftime("%a %b %-d, %Y · %-I:%M %p %Z"),
                 warnings=warnings)
    return redirect("/")


@app.route("/refresh", methods=["GET"])
def refresh():
    """Pull projections straight from the DataGolf API (no manual CSV)."""
    tour = request.args.get("tour", STATE.get("tour") or "PGA Tour")
    if tour not in TOUR_SERIES:
        tour = "PGA Tour"
    try:
        text = fetch_datagolf(tour)
    except Exception as e:
        STATE.update(tour=tour, warnings=[f"DataGolf refresh failed: {e}"])
        return redirect("/")
    rows, event_label, warnings = build_rows(text, tour)
    STATE.update(rows=rows, filename="DataGolf (live)", event=event_label, tour=tour, csv_text=text,
                 when=datetime.now().astimezone().strftime("%a %b %-d, %Y · %-I:%M %p %Z"),
                 warnings=warnings)
    return redirect("/")


@app.route("/refresh_kalshi", methods=["GET"])
def refresh_kalshi():
    """Re-fetch Kalshi order-book prices against the projections already loaded."""
    tour = STATE.get("tour") or "PGA Tour"
    text = STATE.get("csv_text")
    if not text:
        STATE.update(warnings=["Load projections first, then refresh Kalshi prices."])
        return redirect("/")
    rows, event_label, warnings = build_rows(text, tour)
    STATE.update(rows=rows, event=event_label, warnings=warnings,
                 when=datetime.now().astimezone().strftime("%a %b %-d, %Y · %-I:%M %p %Z"))
    return redirect("/")


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Golf — Projections vs Kalshi</title>
<style>
 :root{--bg:#0d1117;--card:#161b22;--line:#21262d;--txt:#e6edf3;--mut:#8b949e;--acc:#3fb950;--neg:#f85149;--g:#238636}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
 .wrap{max-width:1000px;margin:0 auto;padding:26px 18px 70px}
 h1{font-size:24px;margin:0 0 6px} .sub{color:var(--mut);font-size:12.5px;margin:0 0 18px}
 .bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:0 0 22px}
 .btn{display:inline-block;background:var(--g);color:#fff;border:none;border-radius:8px;
   padding:10px 18px;font-size:14px;font-weight:600;cursor:pointer}
 .btn:hover{filter:brightness(1.1)} .meta{color:var(--mut);font-size:12px}
 .btn-dg{background:#1f6feb} .btn-k{background:#6e40c9} .btn[disabled]{opacity:.6;cursor:wait}
 .sel{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:9px 10px;font-size:14px;cursor:pointer}
 .warn{color:#d29922;font-size:12px;margin:0 0 14px}
 .tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 16px}
 .tab{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:5px 14px;
   font-size:12.5px;color:var(--mut);cursor:pointer;user-select:none}
 .tab.on{color:var(--txt);border-color:var(--acc)}
 .mkt{display:none} .mkt.on{display:block}
 table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
 th{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);text-align:right;padding:9px 12px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
 th:first-child{text-align:left} th:hover{color:var(--txt)}
 th.sorted-asc::after{content:" ▲";font-size:9px;color:var(--acc)}
 th.sorted-desc::after{content:" ▼";font-size:9px;color:var(--acc)} td{padding:6px 12px;font-size:13px;text-align:right;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
 td:first-child{text-align:left} tr:last-child td{border-bottom:none}
 .pos{color:var(--acc);font-weight:600} .negv{color:var(--neg)} .dash{color:var(--mut)}
 td a{color:var(--txt);text-decoration:none} td a:hover{color:#58a6ff;text-decoration:underline}
 .evlink{margin:0 0 8px} .evlink a{color:#58a6ff;font-size:12px;text-decoration:none} .evlink a:hover{text-decoration:underline}
 .big{background:rgba(63,185,80,.10)}
 .empty{color:var(--mut);padding:40px;text-align:center}
 .pname{cursor:pointer;border-bottom:1px dotted var(--mut)} .pname:hover,.pname.open{color:#58a6ff}
 .extlink{color:var(--mut);text-decoration:none;font-size:11px;margin-left:4px} .extlink:hover{color:#58a6ff}
 .ob-row td{background:#0b0f14;padding:10px 16px}
 .obwrap{display:flex;gap:30px;flex-wrap:wrap}
 .obcol{min-width:140px}
 .obh{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin-bottom:5px}
 .obh.yh{color:var(--acc)} .obh.nh{color:var(--neg)}
 .obt{width:auto;background:transparent;border:none;border-radius:0}
 .obt th{font-size:9.5px;padding:1px 14px 3px 0;border:none;text-align:right;color:var(--mut)}
 .obt td{padding:1px 14px 1px 0;border:none;text-align:right;font-size:12px;color:var(--mut)}
 .obt tr.best td{color:var(--txt);font-weight:700}
 .obempty{color:var(--mut);font-size:12px}
 .auto{color:var(--mut);font-size:12px;display:inline-flex;align-items:center;gap:5px;cursor:pointer;user-select:none}
 .auto input{cursor:pointer;margin:0}
 td.flash{animation:fl 1s ease}@keyframes fl{from{background:rgba(63,185,80,.25)}to{background:transparent}}
</style></head><body><div class="wrap">
<h1>Golf — Projections vs Kalshi</h1>
<p class="sub">Upload your simulation CSV. <b>Buy</b> = the side (Yes/No) with the better edge vs the actual price you'd pay on Kalshi.
 <b>Price</b> = that side's ask; <b>Edge</b> = side probability − price. Click a player to open the market.</p>

<div class="bar">
  <form method="post" action="/upload" enctype="multipart/form-data" id="f">
    <select name="tour" class="sel">
      {% for t in tours %}<option {% if t==st.tour %}selected{% endif %}>{{ t }}</option>{% endfor %}
    </select>
    <input type="file" name="csv" accept=".csv" id="file" style="display:none" onchange="document.getElementById('f').submit()">
    <button type="button" class="btn btn-dg" onclick="dgRefresh()">↻ Refresh from DataGolf</button>
    <button type="button" class="btn btn-k" onclick="kRefresh()">↻ Refresh Kalshi Prices</button>
    <button type="button" class="btn" onclick="document.getElementById('file').click()">⬆ Upload Projections CSV</button>
  </form>
  {% if st.when %}<span class="meta"><b style="color:var(--txt)">{{ st.tour }}</b> · {{ st.filename }} · event <b style="color:var(--txt)">{{ st.event or '?' }}</b> · loaded {{ st.when }}</span>{% endif %}
  <label class="auto" title="Update live scores from DataGolf every 5 minutes (scores only — does not re-fetch projections or Kalshi prices)">
    <input type="checkbox" id="auto"> Auto-update scores (5 min)</label>
  <span id="scoreStamp" class="meta"></span>
</div>

{% for w in st.warnings %}<div class="warn">⚠ {{ w }}</div>{% endfor %}

{% if not st.rows %}
  <div class="empty">No projections loaded yet. Click <b>Upload Projections CSV</b> to begin.</div>
{% else %}
  <div class="tabs">
    {% for m in st.rows %}<div class="tab {% if loop.first %}on{% endif %}" onclick="showTab({{ loop.index0 }})">{{ m.label }} <span style="opacity:.6">({{ m.priced }})</span></div>{% endfor %}
  </div>
  {% for m in st.rows %}
  <div class="mkt {% if loop.first %}on{% endif %}" id="mkt{{ loop.index0 }}">
    {% if m.event_url %}<div class="evlink"><a href="{{ m.event_url }}" target="_blank" rel="noopener">{{ m.label }} on Kalshi ↗</a></div>{% endif %}
    {% if not m.available %}
      <div class="empty">Kalshi has no open <b>{{ m.label }}</b> market yet — showing projections only.</div>
    {% endif %}
    <table><thead><tr>
      <th onclick="sortTable(this)">Player</th>
      <th onclick="sortTable(this)">Pos</th>
      <th onclick="sortTable(this)">Total</th>
      <th onclick="sortTable(this)">Rnd</th>
      <th onclick="sortTable(this)">Thru</th>
      <th onclick="sortTable(this)">Proj</th>
      <th onclick="sortTable(this)">Buy</th>
      <th onclick="sortTable(this)">Price</th>
      <th onclick="sortTable(this)" class="sorted-desc">Edge</th>
    </tr></thead><tbody>
    {% for r in m.rows %}
      <tr data-ticker="{{ r.ticker or '' }}" data-key="{{ r.key }}" class="{% if r.edge is not none and r.edge > 0.03 %}big{% endif %}">
        <td data-v="{{ r.player }}">{% if r.ticker %}<span class="pname" onclick="toggleOB(this)">{{ r.player }}</span>{% else %}{{ r.player }}{% endif %}{% if r.link %} <a class="extlink" href="{{ r.link }}" target="_blank" rel="noopener" title="Open on Kalshi">↗</a>{% endif %}</td>
        {% set s = r.score %}
        <td data-v="{{ s.pos_v if s else '' }}">{{ s.pos if s else '—' }}</td>
        <td data-v="{{ s.total_v if s else '' }}" class="{% if s and s.total_v < 0 %}pos{% elif s and 0 < s.total_v < 9999 %}negv{% endif %}">{{ s.total_d if s else '—' }}</td>
        <td data-v="{{ s.today_v if s else '' }}" class="{% if s and s.today_v < 0 %}pos{% elif s and 0 < s.today_v < 9999 %}negv{% endif %}">{{ s.today_d if s else '—' }}</td>
        <td data-v="{{ s.thru_v if s and s.thru_v >= 0 else '' }}">{{ s.thru_d if s else '—' }}</td>
        <td data-v="{{ r.proj }}">{{ '%.2f%%'|format(r.proj*100) }}</td>
        <td data-v="{{ r.side or '' }}" class="{% if r.side=='Yes' %}pos{% elif r.side=='No' %}negv{% endif %}">{{ r.side or '—' }}</td>
        <td data-v="{{ r.price if r.price is not none else '' }}">{% if r.price is not none %}{{ '%.1f¢'|format(r.price*100) }}{% else %}<span class="dash">—</span>{% endif %}</td>
        <td data-v="{{ r.edge if r.edge is not none else '' }}" class="{% if r.edge is not none and r.edge>0 %}pos{% elif r.edge is not none and r.edge<0 %}negv{% endif %}">{% if r.edge is not none %}{{ '%+.2f'|format(r.edge*100) }}{% else %}<span class="dash">—</span>{% endif %}</td>
      </tr>
    {% endfor %}
    </tbody></table>
  </div>
  {% endfor %}
{% endif %}

<script>
// --- live scores (scores-only, no reload, no Kalshi) ---
function setCell(td, v, text, color){
  if(!td) return;
  const changed = td.textContent !== text;
  td.dataset.v = (v===''||v===null||v===undefined)?'':v;
  td.textContent = text;
  if(color){
    td.classList.remove('pos','negv');
    if(typeof v==='number' && v<0) td.classList.add('pos');
    else if(typeof v==='number' && v>0 && v<9999) td.classList.add('negv');
  }
  if(changed){ td.classList.remove('flash'); void td.offsetWidth; td.classList.add('flash'); }
}
function applyScores(map){
  document.querySelectorAll('tr[data-key]').forEach(tr=>{
    const s = map[tr.dataset.key]; if(!s) return;
    const c = tr.cells;
    setCell(c[1], s.pos_v, s.pos, false);
    setCell(c[2], s.total_v, s.total_d, true);
    setCell(c[3], s.today_v, s.today_d, true);
    setCell(c[4], (s.thru_v>=0?s.thru_v:''), s.thru_d, false);
  });
}
function pullScores(){
  const tour = document.querySelector('select[name=tour]').value;
  fetch('/scores?tour='+encodeURIComponent(tour)).then(r=>r.json()).then(d=>{
    if(d && d.scores) applyScores(d.scores);
    const m = document.getElementById('scoreStamp');
    if(m && d && d.when) m.textContent = '· scores updated '+d.when;
  }).catch(()=>{});
}
function dgRefresh(){
  const tour = document.querySelector('select[name=tour]').value;
  const b = document.querySelector('.btn-dg');
  b.textContent = '↻ Fetching DataGolf…'; b.disabled = true;
  window.location = '/refresh?tour=' + encodeURIComponent(tour);
}
function kRefresh(){
  const b = document.querySelector('.btn-k');
  b.textContent = '↻ Fetching Kalshi…'; b.disabled = true;
  window.location = '/refresh_kalshi';
}
function showTab(i){
  document.querySelectorAll('.tab').forEach((t,j)=>t.classList.toggle('on',j===i));
  document.querySelectorAll('.mkt').forEach((m,j)=>m.classList.toggle('on',j===i));
}
function ladder(rows,label,cls){
  if(!rows||!rows.length) return '<div class="obcol"><div class="obh '+cls+'">'+label+'</div><div class="obempty">no resting orders</div></div>';
  let h='<div class="obcol"><div class="obh '+cls+'">'+label+'</div><table class="obt"><tr><th>Price</th><th>Resting&nbsp;$</th></tr>';
  rows.forEach((r,i)=>{h+='<tr'+(i===0?' class="best"':'')+'><td>'+r[0].toFixed(1)+'¢</td><td>$'+r[1].toLocaleString()+'</td></tr>';});
  return h+'</table></div>';
}
function renderOB(d){
  return '<div class="obwrap">'+ladder(d.buy_yes,'Buy YES (ask)','yh')+ladder(d.buy_no,'Buy NO (ask)','nh')+'</div>';
}
function toggleOB(el){
  const tr=el.closest('tr'), tbody=tr.parentNode;
  const nxt=tr.nextElementSibling;
  if(nxt&&nxt.classList.contains('ob-row')){nxt.remove();el.classList.remove('open');return;}
  // collapse any other open book in this market for tidiness
  tbody.querySelectorAll('.ob-row').forEach(r=>{const p=r.previousElementSibling; if(p){const s=p.querySelector('.pname'); if(s)s.classList.remove('open');} r.remove();});
  el.classList.add('open');
  const row=document.createElement('tr'); row.className='ob-row';
  const td=document.createElement('td'); td.colSpan=tr.cells.length;
  td.innerHTML='<div class="obempty">Loading order book…</div>'; row.appendChild(td); tr.after(row);
  fetch('/orderbook/'+encodeURIComponent(tr.dataset.ticker))
    .then(r=>r.json()).then(d=>{td.innerHTML=renderOB(d);})
    .catch(()=>{td.innerHTML='<div class="obempty">Failed to load order book.</div>';});
}
function sortTable(th){
  const table=th.closest('table'), tbody=table.tBodies[0];
  // drop any expanded order-book rows so sorting only reorders player rows
  tbody.querySelectorAll('.ob-row').forEach(r=>r.remove());
  tbody.querySelectorAll('.pname.open').forEach(s=>s.classList.remove('open'));
  const idx=Array.from(th.parentNode.children).indexOf(th);
  const asc=!th.classList.contains('sorted-asc');   // toggle; first click on a new col = ascending
  th.parentNode.querySelectorAll('th').forEach(h=>h.classList.remove('sorted-asc','sorted-desc'));
  th.classList.add(asc?'sorted-asc':'sorted-desc');
  Array.from(tbody.rows).sort((a,b)=>{
    let va=a.cells[idx].dataset.v||'', vb=b.cells[idx].dataset.v||'';
    if(va===''&&vb==='')return 0;
    if(va==='')return 1;          // blanks ("—") always sink to the bottom
    if(vb==='')return -1;
    const na=parseFloat(va), nb=parseFloat(vb);
    if(!isNaN(na)&&!isNaN(nb))return asc?na-nb:nb-na;
    return asc?va.localeCompare(vb):vb.localeCompare(va);
  }).forEach(r=>tbody.appendChild(r));
}
// --- auto-update scores toggle (every 5 min while tab is open) ---
(function(){
  const box = document.getElementById('auto'); if(!box) return;
  box.checked = localStorage.getItem('golfAuto') === '1';
  let timer = null;
  function apply(){
    localStorage.setItem('golfAuto', box.checked ? '1' : '0');
    if(timer){ clearInterval(timer); timer = null; }
    if(box.checked){ pullScores(); timer = setInterval(()=>{ if(!document.hidden) pullScores(); }, 5*60*1000); }
  }
  box.addEventListener('change', apply);
  apply();
})();
</script>
</div></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))   # 5000 is taken by macOS AirPlay Receiver
    print(f"Golf value finder -> http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
