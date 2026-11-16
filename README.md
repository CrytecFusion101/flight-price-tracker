# DEL ⇄ Bali (DPS) flight price tracker

Watches Google Flights fares for **2 adults, economy**, Delhi → Denpasar on **28 Mar 2027 (landing 29 Mar) or 29 Mar 2027**, returning **3 Apr 2027** (afternoon departure preferred). Non-stop and checked-bag-included fares are preferred.

- **Every 30 min** a GitHub Actions job runs 8 queries (non-stop + ≤1-stop one-ways each way, plus non-stop round trips).
- **History** is appended to `data/series.csv` (tracked series) and `data/observations/YYYY-MM.csv` (top-3 raw options per query).
- **Dashboard** (`docs/index.html`, hosted on Vercel; it loads live data from this repo, so it never needs a redeploy): price history, a naive 7-day trend projection, all-time lows, target, and every current option with fit and baggage flags.
- **Push alerts** via [ntfy](https://ntfy.sh) (and optionally Telegram) when a watched series drops, rises, hits a new low, or reaches `target_price`. You also get a daily digest and a "tracker broken" alert after 3 failed runs in a row.

## Tracked series (totals for 2 passengers)
| key | meaning |
|---|---|
| `best_trip` | cheapest of the non-stop round trip vs two non-stop one-ways, all fitting your times and with a checked bag likely included |
| `rt_nonstop_fit` | non-stop round-trip ticket, outbound lands 29 Mar |
| `combo_nonstop` | non-stop out (lands 29 Mar) + non-stop back (departs ≥12:00 on 3 Apr), as two tickets |
| `out_*` / `ret_*` | single legs (non-stop, ≤1 stop, lands before noon) |
| `cheapest_any` | ignores all preferences, for reference |

## Fail-safes
- Each query is retried 3× with backoff, and the job pauses between queries.
- Every result is validated: route, date, price bounds, plausible duration. Garbage results are dropped.
- If a query fails, the series built from it is not compared, so an outage never looks like a price jump.
- Price jumps ≥40% are flagged `UNVERIFIED` in alerts.
- Corrupt state is quarantined instead of crashing the run. All files are written atomically.
- The scheduled job runs the test suite before touching any data. The job turns red, and GitHub emails you, after 3 failed runs in a row.
- Tracking stops automatically after 3 Apr 2027.

## Baggage
Google Flights doesn't expose baggage allowances, so `bag` is a per-airline heuristic (`yes` / `likely` / `no` / `unknown`) from [baggage.py](src/flight_tracker/baggage.py). Always confirm on the booking page.

## Local use
Install [uv](https://docs.astral.sh/uv/), then:
```bash
uv sync
uv run flight-tracker --dry-run      # live fetch, prints results, writes nothing
uv run pytest -q                     # offline tests
NTFY_TOPIC=your-topic uv run flight-tracker --test-notify
```
Settings live in [config.toml](config.toml): dates, time windows, thresholds and target price.
