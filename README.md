# Golf — Projections vs Kalshi (value finder)

Local web app. Click **Upload Projections CSV**, pick your simulation file, and it
fetches live Kalshi order-book prices for the current tournament and shows where
your model disagrees with the market. Re-upload anytime (e.g. between rounds) —
each upload re-fetches fresh Kalshi prices.

## CSV format
`player_name` ("Last, First" or "First Last"), then probability columns:
`win`, `top_5`, `top_10`, `top_20`, `make_cut`. (The header from a CH-model export.)

## Markets
Outright winner, make cut, top 20, top 10, top 5 — each mapped to the current open
Kalshi event for its series (`KXPGAWIN`, `KXPGAMAKECUT`, `KXPGATOP20/10/5`),
auto-discovered, so it works for the next tournament too.

## Columns
- **Proj** — your model's probability.
- **Kalshi** — vig-free market price from the order book (null fields → derived from the book).
- **Edge** — Proj − Kalshi (percentage points); green = your model is higher (YES value).
- **EV% (Yes)** — return on buying YES at the Kalshi price (Proj/Price − 1).
Rows with edge > 3 pts are highlighted.

## Run
```bash
pip install -r requirements.txt
python3 app.py            # open http://127.0.0.1:5000
```
Each upload takes ~40s (it prices every player's order book across all markets, with
retry passes so Kalshi rate-limiting doesn't drop players).

Local-only tool — run it on your machine and open the browser; nothing is published.
