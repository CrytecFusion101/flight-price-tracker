"""Turn raw options into tracked price series and detect alert-worthy changes."""

from __future__ import annotations

from dataclasses import dataclass

from .baggage import bag_ok

# key -> human label. All prices are totals for all passengers.
SERIES: dict[str, str] = {
    "best_trip": "Best trip (non-stop, fits times, bag OK)",
    "rt_nonstop_fit": "Round-trip ticket, non-stop, lands 29 Mar",
    "combo_nonstop": "2 one-way tickets, non-stop, preferred times",
    "out_nonstop_fit": "Outbound one-way, non-stop, lands 29 Mar",
    "out_ideal": "Outbound one-way, lands 29 Mar before noon",
    "out_any_fit": "Outbound one-way, <=1 stop, lands 29 Mar",
    "ret_nonstop_fit": "Return one-way 3 Apr, non-stop, departs afternoon",
    "ret_any_fit": "Return one-way 3 Apr, <=1 stop, departs afternoon",
    "cheapest_any": "Cheapest possible (ignores time/stop/bag prefs)",
}


def _best(options, pred):
    c = [o for o in options if pred(o)]
    return min(c, key=lambda o: o.price) if c else None


def _single(o) -> dict | None:
    return {"price": o.price, "desc": o.describe(), "url": o.url} if o else None


def _combo(a, b) -> dict | None:
    if not (a and b):
        return None
    return {"price": a.price + b.price, "desc": f"OUT {a.describe()} | RET {b.describe()}", "url": a.url}


def compute_series(options) -> dict[str, dict]:
    def ok(o):
        return o.fits and bag_ok(o.bag)

    out_ns = _best(options, lambda o: o.kind == "out" and o.nonstop and ok(o))
    out_any = _best(options, lambda o: o.kind == "out" and ok(o))
    out_ideal = _best(options, lambda o: o.kind == "out" and o.ideal and bag_ok(o.bag))
    ret_ns = _best(options, lambda o: o.kind == "ret" and o.nonstop and ok(o))
    ret_any = _best(options, lambda o: o.kind == "ret" and ok(o))
    rt_ns = _best(options, lambda o: o.kind == "rt" and o.nonstop and ok(o))
    cheap = _combo(_best(options, lambda o: o.kind == "out"), _best(options, lambda o: o.kind == "ret"))

    combo_ns = _combo(out_ns, ret_ns)
    rt = _single(rt_ns)
    if rt:
        rt["desc"] = "Round trip, OUT " + rt["desc"] + " (return = Google's cheapest pairing)"
    candidates = [s for s in (combo_ns, rt) if s]
    best = dict(min(candidates, key=lambda s: s["price"])) if candidates else None

    series = {
        "best_trip": best,
        "rt_nonstop_fit": rt,
        "combo_nonstop": combo_ns,
        "out_nonstop_fit": _single(out_ns),
        "out_ideal": _single(out_ideal),
        "out_any_fit": _single(out_any),
        "ret_nonstop_fit": _single(ret_ns),
        "ret_any_fit": _single(ret_any),
        "cheapest_any": cheap,
    }
    return {k: v for k, v in series.items() if v}


@dataclass
class Event:
    key: str
    kind: str  # drop | rise | new_low | target
    old: int | None
    new: int
    priority: int
    suspicious: bool = False

    def line(self) -> str:
        label = SERIES.get(self.key, self.key)
        warn = " [UNVERIFIED big jump]" if self.suspicious else ""
        if self.kind == "target":
            return f"TARGET HIT: {label} now Rs {self.new:,}{warn}"
        if self.old is None:
            return f"{label}: Rs {self.new:,}"
        d = self.new - self.old
        arrow = "DOWN" if d < 0 else "UP"
        tag = " NEW ALL-TIME LOW" if self.kind == "new_low" else ""
        return f"{arrow} {label}: Rs {self.old:,} -> Rs {self.new:,} ({d:+,}, {d / self.old * 100:+.1f}%){tag}{warn}"


def detect_changes(series: dict, state: dict, cfg) -> list[Event]:
    last, lows = state.get("last", {}), state.get("lows", {})
    events: list[Event] = []
    for key in cfg.watch_series:
        cur = series.get(key)
        if not cur:
            continue
        p, prev, low = cur["price"], last.get(key), lows.get(key)
        if prev is None:
            continue
        d = p - prev
        pct = abs(d) / prev * 100
        if abs(d) >= cfg.min_change_inr and pct >= cfg.min_change_pct:
            suspicious = pct >= cfg.suspicious_change_pct
            if d < 0 and low is not None and p < low:
                events.append(Event(key, "new_low", prev, p, 5, suspicious))
            elif d < 0:
                events.append(Event(key, "drop", prev, p, 4, suspicious))
            else:
                events.append(Event(key, "rise", prev, p, 2, suspicious))
    best = series.get("best_trip")
    if cfg.target_price and best:
        p, prev = best["price"], last.get("best_trip")
        if p <= cfg.target_price and (prev is None or prev > cfg.target_price):
            events.append(Event("best_trip", "target", prev, p, 5))
    return events


def update_state_prices(series: dict, state: dict) -> None:
    last = state.setdefault("last", {})
    lows = state.setdefault("lows", {})
    for k, v in series.items():
        last[k] = v["price"]
        lows[k] = min(lows.get(k, v["price"]), v["price"])
