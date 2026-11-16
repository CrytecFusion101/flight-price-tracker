"""Load and validate config.toml (fail fast on typos / bad values)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.toml"


@dataclass(frozen=True)
class Config:
    origin: str
    destination: str
    adults: int
    currency: str
    outbound_dates: list[str]
    outbound_arrive_date: str
    outbound_ideal_arrive_before_hour: int
    return_date: str
    return_depart_after_hour: int
    max_stops: int
    stop_tracking_after: str
    tz_offsets: dict[str, float]
    min_change_inr: int
    min_change_pct: float
    target_price: int
    digest_hour_utc: int
    failure_alert_after: int
    watch_series: list[str]
    min_price: int
    max_price: int
    suspicious_change_pct: float


def load(path: Path = CONFIG_PATH) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    flat: dict = {}
    for section in ("trip", "alerts", "sanity"):
        flat.update(raw.pop(section, {}))
    if raw:
        raise ValueError(f"unknown config sections: {sorted(raw)}")
    names = {f.name for f in fields(Config)}
    unknown, missing = set(flat) - names, names - set(flat)
    if unknown or missing:
        raise ValueError(f"config keys unknown={sorted(unknown)} missing={sorted(missing)}")
    cfg = Config(**flat)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    from .analyze import SERIES

    for d in [*cfg.outbound_dates, cfg.outbound_arrive_date, cfg.return_date, cfg.stop_tracking_after]:
        date.fromisoformat(d)
    for code in (cfg.origin, cfg.destination):
        if len(code) != 3 or not code.isupper():
            raise ValueError(f"bad IATA code {code!r}")
        if code not in cfg.tz_offsets:
            raise ValueError(f"tz_offsets missing {code}")
    if not 1 <= cfg.adults <= 9:
        raise ValueError("adults must be 1..9")
    if not 0 <= cfg.min_price < cfg.max_price:
        raise ValueError("bad sanity price bounds")
    if max(cfg.outbound_dates) > cfg.return_date:
        raise ValueError("return before outbound")
    bad = [s for s in cfg.watch_series if s not in SERIES]
    if bad:
        raise ValueError(f"unknown watch_series {bad}; valid: {sorted(SERIES)}")
