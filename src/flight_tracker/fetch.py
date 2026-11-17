"""Query Google Flights (via fast-flights) with retries and strict validation."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .baggage import classify
from .config import Config

log = logging.getLogger(__name__)
ATTEMPTS = 3


@dataclass(frozen=True)
class QuerySpec:
    id: str
    kind: str  # "out" | "ret" | "rt"
    legs: tuple[tuple[str, str, str], ...]  # (date, from, to)
    max_stops: int


@dataclass
class Option:
    query: str
    kind: str
    price: int  # total for all passengers (round-trip total for kind="rt")
    airlines: str
    stops: int
    dep: str  # local ISO "2027-03-29T07:55"
    arr: str
    duration_min: int
    nonstop: bool
    fits: bool
    ideal: bool
    bag: str
    url: str

    def describe(self) -> str:
        d, a = datetime.fromisoformat(self.dep), datetime.fromisoformat(self.arr)
        stops = "non-stop" if self.nonstop else f"{self.stops} stop"
        return f"{self.airlines} {stops} {d:%d %b %H:%M}->{a:%d %b %H:%M} (bag: {self.bag})"


def build_specs(cfg: Config) -> list[QuerySpec]:
    o, d, r = cfg.origin, cfg.destination, cfg.return_date
    stop_levels = sorted({0, cfg.max_stops})
    rtag = r[5:].replace("-", "")
    specs: list[QuerySpec] = []
    for day in cfg.outbound_dates:
        tag = day[5:].replace("-", "")
        specs += [QuerySpec(f"out-{tag}-{s}s", "out", ((day, o, d),), s) for s in stop_levels]
    specs += [QuerySpec(f"ret-{rtag}-{s}s", "ret", ((r, d, o),), s) for s in stop_levels]
    for day in cfg.outbound_dates:
        tag = day[5:].replace("-", "")
        specs.append(QuerySpec(f"rt-{tag}-{rtag}-0s", "rt", ((day, o, d), (r, d, o)), 0))
    return specs


def to_query(spec: QuerySpec, cfg: Config):
    from fast_flights import FlightQuery, Passengers, create_query

    return create_query(
        flights=[FlightQuery(date=dt, from_airport=f, to_airport=t, max_stops=spec.max_stops) for dt, f, t in spec.legs],
        trip="round-trip" if spec.kind == "rt" else "one-way",
        seat="economy",
        passengers=Passengers(adults=cfg.adults),
        currency=cfg.currency,
        language="en",
    )


def _dt(sdt) -> datetime:
    y, m, d = sdt.date
    hh, mm = sdt.time
    return datetime(y, m, d, hh or 0, mm or 0)


def to_option(result, spec: QuerySpec, cfg: Config, url: str) -> Option:
    """Convert one fast-flights result; raise ValueError if it fails sanity checks."""
    segs = result.flights
    if not segs:
        raise ValueError("no segments")
    day, o, d = spec.legs[0]
    if segs[0].from_airport.code != o or segs[-1].to_airport.code != d:
        raise ValueError(f"route mismatch {segs[0].from_airport.code}->{segs[-1].to_airport.code}")
    dep, arr = _dt(segs[0].departure), _dt(segs[-1].arrival)
    if dep.date().isoformat() != day:
        raise ValueError(f"date mismatch {dep.date()} != {day}")
    price = int(result.price)
    if not cfg.min_price <= price <= cfg.max_price:
        raise ValueError(f"price out of bounds {price}")
    elapsed = (arr - timedelta(hours=cfg.tz_offsets[d])) - (dep - timedelta(hours=cfg.tz_offsets[o]))
    minutes = int(elapsed.total_seconds() // 60)
    if not 60 <= minutes <= 48 * 60:
        raise ValueError(f"implausible duration {minutes}m")

    stops = len(segs) - 1
    if spec.kind == "ret":
        fits = dep.hour >= cfg.return_depart_after_hour
        ideal = fits
    else:
        fits = arr.date().isoformat() == cfg.outbound_arrive_date
        ideal = fits and arr.hour < cfg.outbound_ideal_arrive_before_hour
    airlines = list(result.airlines or [])
    return Option(
        query=spec.id, kind=spec.kind, price=price, airlines=" + ".join(airlines) or "?",
        stops=stops, dep=dep.isoformat(timespec="minutes"), arr=arr.isoformat(timespec="minutes"),
        duration_min=minutes, nonstop=stops == 0, fits=fits, ideal=ideal,
        bag=classify(airlines), url=url,
    )


class BlockedPage(RuntimeError):
    """Google served a page without a results payload (consent wall / bot check)."""


class EmptyResult(RuntimeError):
    """Results payload present but empty; retried because it is often transient."""


# Pre-accepted consent cookies + India region so datacenter IPs skip the consent wall.
CONSENT_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}
GOOGLE_FLIGHTS = "https://www.google.com/travel/flights"


def _page_hint(html: str) -> str:
    import re

    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    return f"title={(m.group(1).strip() if m else '?')[:60]!r} len={len(html)}"


def _default_fetch(q):
    from fast_flights.parser import parse
    from primp import Client

    # Fresh client per attempt = fresh fingerprint/cookie jar.
    client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
    res = client.get(GOOGLE_FLIGHTS, params={**q.params(), "gl": "IN"}, cookies=CONSENT_COOKIES)
    html = res.text
    if "ds:1" not in html:
        raise BlockedPage(f"no results payload ({_page_hint(html)})")
    result = parse(html)
    if not result:
        raise EmptyResult(f"empty results ({_page_hint(html)})")
    return result


def fetch_all(
    specs: list[QuerySpec],
    cfg: Config,
    fetch_fn: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[Option], dict[str, str]]:
    """Run every query. Returns (options, errors-by-query-id). Never raises."""
    fetch_fn = fetch_fn or _default_fetch
    options: list[Option] = []
    errors: dict[str, str] = {}
    for i, spec in enumerate(specs):
        if i:
            sleep(random.uniform(1.5, 4.0))  # be polite, avoid rate limits
        q = to_query(spec, cfg)
        url = q.url()
        raw, last = None, None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                raw = list(fetch_fn(q))
                break
            except Exception as e:  # noqa: BLE001 - any provider failure is retryable
                if type(e).__name__ == "FlightsNotFound":
                    raw = []
                    break
                last = e
                log.warning("%s attempt %d failed: %s", spec.id, attempt, e)
                if attempt < ATTEMPTS:
                    sleep(5 * attempt**2)
        if raw is None and isinstance(last, EmptyResult):
            raw = []  # consistently empty with a real results payload: genuinely no flights
        if raw is None:
            errors[spec.id] = f"{type(last).__name__}: {last}"[:300]
            continue
        good, bad = [], []
        for r in raw:
            try:
                good.append(to_option(r, spec, cfg, url))
            except (ValueError, AttributeError, TypeError) as e:
                bad.append(str(e))
        if raw and not good:
            errors[spec.id] = f"all {len(raw)} results invalid: {bad[:3]}"
        elif bad:
            log.info("%s: dropped %d invalid results: %s", spec.id, len(bad), bad[:3])
        log.info("%s: %d options", spec.id, len(good))
        options += good
    return options, errors
