"""Entry point: fetch -> validate -> store -> detect changes -> notify."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime

from . import config as config_mod
from . import notify, store
from .analyze import SERIES, compute_series, detect_changes, update_state_prices
from .fetch import build_specs, fetch_all

log = logging.getLogger("flight_tracker")

# Which query kinds each series is built from. A series is only compared/alerted
# when all of its inputs succeeded this run (a failed query must not look like a price jump).
DEPENDS = {
    "best_trip": {"out", "ret", "rt"}, "rt_nonstop_fit": {"rt"}, "combo_nonstop": {"out", "ret"},
    "cheapest_any": {"out", "ret"},
}


def _deps(key: str) -> set[str]:
    return DEPENDS.get(key, {key.split("_")[0]})


def run(cfg, state: dict, now: datetime, fetch_fn=None, sleep=None, write=True) -> int:
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if now.date() > date.fromisoformat(cfg.stop_tracking_after):
        log.info("Trip date passed; tracking stopped.")
        return 0

    specs = build_specs(cfg)
    kwargs = {"fetch_fn": fetch_fn}
    if sleep:
        kwargs["sleep"] = sleep
    options, errors = fetch_all(specs, cfg, **kwargs)
    state["runs"] = state.get("runs", 0) + 1
    state["last_errors"] = errors

    if not options or len(errors) == len(specs):
        state["fail_count"] = state.get("fail_count", 0) + 1
        log.error("Run failed (%d consecutive): %s", state["fail_count"], errors)
        if state["fail_count"] == cfg.failure_alert_after:
            notify.send("Flight tracker is FAILING",
                        f"{state['fail_count']} runs in a row got no prices.\n" +
                        "\n".join(f"{k}: {v}" for k, v in list(errors.items())[:4]), priority=4, tags=["warning"])
        if write:
            store.save_state(state)
            store.write_dashboard(ts, {}, [], state, errors, cfg)
        return 1 if state["fail_count"] >= cfg.failure_alert_after else 0

    if state.get("fail_count", 0) >= cfg.failure_alert_after:
        notify.send("Flight tracker recovered", f"Prices flowing again after {state['fail_count']} failed runs.",
                    priority=2, tags=["white_check_mark"])
    state["fail_count"] = 0
    state["last_success"] = ts

    series = compute_series(options)
    failed_kinds = {s.kind for s in specs if s.id in errors}
    trusted = {k: v for k, v in series.items() if not (_deps(k) & failed_kinds)}
    first_run = not state.get("last")
    events = detect_changes(trusted, state, cfg)
    update_state_prices(trusted, state)

    best = series.get("best_trip")
    click = best["url"] if best else None
    if first_run:
        notify.send("Flight tracker started: DEL-DPS", _summary(series, state), priority=3, click=click, tags=["airplane"])
    elif events:
        top = max(events, key=lambda e: e.priority)
        head = series.get(top.key, {}).get("price", top.new)
        title = {"new_low": "New LOW", "drop": "Price DROP", "rise": "Price up", "target": "TARGET HIT"}[top.kind]
        body = "\n".join(e.line() for e in events)
        if best:
            body += f"\n\nBest now: Rs {best['price']:,}\n{best['desc']}"
        notify.send(f"{title}: DEL-DPS Rs {head:,}", body, priority=top.priority, click=click,
                    tags=["chart_with_downwards_trend" if top.kind != "rise" else "chart_with_upwards_trend"])

    today = now.date().isoformat()
    if not first_run and now.hour >= cfg.digest_hour_utc and state.get("last_digest") != today:
        notify.send("Daily DEL-DPS price digest", _summary(series, state), priority=2, click=click, tags=["calendar"])
        state["last_digest"] = today
    elif first_run:
        state["last_digest"] = today

    if errors:
        log.warning("Partial run, failed queries: %s", errors)
    if not write:
        print(_summary(series, state))
        for o in sorted(options, key=lambda o: (o.kind, o.price)):
            print(f"  [{o.query}] Rs {o.price:,} fits={o.fits} ideal={o.ideal} {o.describe()}")
    else:
        store.append_run(ts, options, series)
        store.save_state(state)
        store.write_dashboard(ts, series, options, state, errors, cfg)
    log.info("OK: %d options, %d series, %d events", len(options), len(series), len(events))
    return 0


def _summary(series: dict, state: dict) -> str:
    lows = state.get("lows", {})
    lines = []
    for k, label in SERIES.items():
        if k in series:
            p = series[k]["price"]
            low = lows.get(k)
            lines.append(f"{label}: Rs {p:,}" + (f" (low Rs {low:,})" if low and low < p else ""))
    best = series.get("best_trip")
    if best:
        lines.append(f"\nBest: {best['desc']}")
    return "\n".join(lines) + "\n(prices = total for all passengers)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DEL<->DPS flight price tracker")
    ap.add_argument("--check-config", action="store_true", help="validate config and exit")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print; no writes, no notifications")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = config_mod.load()
    if args.check_config:
        print(f"config OK: {len(build_specs(cfg))} queries per run")
        return 0
    if args.test_notify:
        return 0 if notify.send("Flight tracker test", "Notifications work.", priority=3, tags=["bell"]) else 1
    if args.dry_run:
        import os
        os.environ["NOTIFY_DISABLED"] = "1"
        state = store.load_state()
        return run(cfg, state, datetime.now(UTC), write=False)
    state = store.load_state()
    return run(cfg, state, datetime.now(UTC))


if __name__ == "__main__":
    sys.exit(main())
