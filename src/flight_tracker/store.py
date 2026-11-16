"""Persistence: append-only CSV history, JSON state, dashboard data. Atomic writes."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, fields
from pathlib import Path

from .analyze import SERIES
from .config import ROOT
from .fetch import Option

DATA = ROOT / "data"
DOCS = ROOT / "docs"
SERIES_CSV = "series.csv"
SERIES_COLS = ["ts", "key", "price", "desc"]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _append_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def load_state(data: Path | None = None) -> dict:
    p = (data or DATA) / "state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupt state must not kill tracking; keep a copy for inspection.
        os.replace(p, p.with_suffix(".corrupt.json"))
        return {}


def save_state(state: dict, data: Path | None = None) -> None:
    _atomic_write((data or DATA) / "state.json", json.dumps(state, indent=2, sort_keys=True))


def append_run(ts: str, options: list[Option], series: dict, data: Path | None = None, top_n: int = 3) -> None:
    # Observations: top-N per query, sharded by month to keep files small.
    data = data or DATA
    by_q: dict[str, list[Option]] = {}
    for o in options:
        by_q.setdefault(o.query, []).append(o)
    rows = []
    for opts in by_q.values():
        for o in sorted(opts, key=lambda o: o.price)[:top_n]:
            r = asdict(o)
            r.pop("url")
            rows.append({"ts": ts, **r})
    cols = ["ts"] + [f.name for f in fields(Option) if f.name != "url"]
    _append_csv(data / "observations" / f"{ts[:7]}.csv", cols, rows)
    _append_csv(data / SERIES_CSV, SERIES_COLS, [{"ts": ts, "key": k, **v} for k, v in series.items()])


def read_series(data: Path | None = None) -> dict[str, list[list]]:
    """{key: [[ts, price], ...]} keeping only change points (+ the latest point)."""
    p = (data or DATA) / SERIES_CSV
    out: dict[str, list[list]] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pts = out.setdefault(r["key"], [])
            price = int(r["price"])
            if len(pts) >= 2 and pts[-1][1] == price and pts[-2][1] == price:
                pts[-1][0] = r["ts"]  # extend flat segment instead of adding a point
            else:
                pts.append([r["ts"], price])
    return out


def write_dashboard(ts: str, series: dict, options: list[Option], state: dict, errors: dict, cfg,
                    data: Path | None = None, docs: Path | None = None) -> None:
    docs = docs or DOCS
    latest = sorted(options, key=lambda o: (o.kind, not o.fits, o.price))
    payload = {
        "generated": ts,
        "trip": {
            "route": f"{cfg.origin} <-> {cfg.destination}", "adults": cfg.adults, "currency": cfg.currency,
            "outbound_dates": cfg.outbound_dates, "return_date": cfg.return_date,
            "target_price": cfg.target_price,
        },
        "labels": SERIES,
        "current": series,
        "lows": state.get("lows", {}),
        "history": read_series(data),
        "options": [asdict(o) for o in latest],
        "health": {
            "last_success": state.get("last_success"), "fail_count": state.get("fail_count", 0),
            "errors": errors, "runs": state.get("runs", 0),
        },
    }
    _atomic_write(docs / "data.json", json.dumps(payload, separators=(",", ":")))
