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
RUNS_CSV = "runs.csv"
# price "" = no price this run (search failed / no matching flight). src = what triggered the run.
SERIES_COLS = ["ts", "key", "price", "desc", "src", "note"]
RUNS_COLS = ["ts", "src", "status", "options", "failed"]
DASHBOARD_RUNS = 1500  # ~1 month of runs at 30-min cadence


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _migrate_header(path: Path, cols: list[str]) -> None:
    """If an existing CSV has an older header, rewrite it with the new columns (blank-filled)."""
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == cols:
            return
        rows = list(reader)
    tmp = path.with_suffix(".migrate.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows({c: r.get(c) or "" for c in cols} for r in rows)
    os.replace(tmp, path)


def _append_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    if not new:
        _migrate_header(path, cols)
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


def append_run(ts: str, options: list[Option], series: dict, src: str, status: str, errors: dict,
               failed_keys: set[str] | frozenset = frozenset(), data: Path | None = None, top_n: int = 3) -> None:
    """Record one run: raw observations, every tracked series (gaps included) and a run-log row."""
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

    srows = []
    for k in SERIES:
        v = series.get(k)
        if v:
            note = f"higher price Rs {v['pending_rise']:,} seen, awaiting confirmation" if v.get("pending_rise") else ""
            srows.append({"ts": ts, "key": k, "price": v["price"], "desc": v["desc"], "src": src, "note": note})
        else:
            why = "search failed" if k in failed_keys else "no matching flight"
            srows.append({"ts": ts, "key": k, "price": "", "desc": "", "src": src, "note": f"no price ({why})"})
    _append_csv(data / SERIES_CSV, SERIES_COLS, srows)
    _append_csv(data / RUNS_CSV, RUNS_COLS, [{
        "ts": ts, "src": src, "status": status, "options": len(options), "failed": " ".join(sorted(errors)),
    }])


def read_series(data: Path | None = None) -> dict[str, list[list]]:
    """{key: [[ts, price|None, src, note], ...]} compressing flat stretches (same price, no note)."""
    p = (data or DATA) / SERIES_CSV
    out: dict[str, list[list]] = {}
    if not p.exists():
        return out
    with p.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            pts = out.setdefault(r["key"], [])
            price = int(r["price"]) if r.get("price") else None
            pt = [r["ts"], price, r.get("src") or "", r.get("note") or ""]
            if (len(pts) >= 2 and not pt[3] and pts[-1][1] == price and pts[-2][1] == price
                    and not pts[-1][3] and not pts[-2][3]):
                pts[-1] = pt  # extend flat segment: keep the newest timestamp/source
            else:
                pts.append(pt)
    return out


def read_runs(data: Path | None = None, limit: int = DASHBOARD_RUNS) -> list[list]:
    p = (data or DATA) / RUNS_CSV
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))[-limit:]
    return [[r["ts"], r["src"], r["status"], int(r["options"] or 0), r["failed"]] for r in rows]


def write_dashboard(ts: str, series: dict, options: list[Option], state: dict, errors: dict, cfg,
                    src: str = "", status: str = "", data: Path | None = None, docs: Path | None = None) -> None:
    docs = docs or DOCS
    latest = sorted(options, key=lambda o: (o.kind, not o.fits, o.price))
    payload = {
        "generated": ts,
        "source": src,
        "status": status,
        "trip": {
            "route": f"{cfg.origin} <-> {cfg.destination}", "adults": cfg.adults, "currency": cfg.currency,
            "outbound_dates": cfg.outbound_dates, "return_date": cfg.return_date,
            "target_price": cfg.target_price,
        },
        "labels": SERIES,
        "current": series,
        "lows": state.get("lows", {}),
        "history": read_series(data),
        "runs": read_runs(data),
        "options": [asdict(o) for o in latest],
        "health": {
            "last_success": state.get("last_success"), "fail_count": state.get("fail_count", 0),
            "errors": errors, "runs": state.get("runs", 0),
        },
    }
    _atomic_write(docs / "data.json", json.dumps(payload, separators=(",", ":")))
