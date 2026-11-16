"""Offline tests: no network. Fake fast-flights results drive the full pipeline."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight

from flight_tracker import analyze, baggage, store
from flight_tracker import config as config_mod
from flight_tracker import main as main_mod
from flight_tracker.fetch import build_specs, fetch_all, to_option

CFG = config_mod.load()
NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)


def seg(frm, to, dep, arr):
    def sdt(s):
        d, t = s.split(" ")
        y, m, dd = map(int, d.split("-"))
        hh, mm = map(int, t.split(":"))
        return SimpleDatetime((y, m, dd), (hh, mm))

    return SingleFlight(Airport(frm, frm), Airport(to, to), sdt(dep), sdt(arr), 300, "A321")


def flight(price, airline, *segs):
    return Flights("x", price, [airline], list(segs), CarbonEmission(0, 0))


AI_OUT = flight(51046, "Air India", seg("DEL", "DPS", "2027-03-28 20:45", "2027-03-29 07:20"))
AI_RET = flight(54117, "Air India", seg("DPS", "DEL", "2027-04-03 15:10", "2027-04-03 20:45"))
AI_RT = flight(96096, "Air India", seg("DEL", "DPS", "2027-03-28 20:45", "2027-03-29 07:20"))


def fake_fetch(prices=None, fail_ids=()):
    """Return a fetch_fn that serves canned results keyed by query kind."""
    prices = prices or {}

    def fn(q):
        url = q.url()
        # Identify the query by which spec produced it.
        for spec in build_specs(CFG):
            from flight_tracker.fetch import to_query

            if to_query(spec, CFG).url() == url:
                if spec.id in fail_ids:
                    raise ConnectionError("blocked")
                base = {"out": AI_OUT, "ret": AI_RET, "rt": AI_RT}[spec.kind]
                if spec.kind in ("out", "rt") and spec.legs[0][0] != "2027-03-28":
                    return []
                f = Flights(base.type, prices.get(spec.kind, base.price), base.airlines, base.flights, base.carbon)
                return [f]
        raise AssertionError("unknown query")

    return fn


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path / "data")
    monkeypatch.setattr(store, "DOCS", tmp_path / "docs")
    sent = []
    monkeypatch.setattr(main_mod.notify, "send", lambda *a, **k: sent.append((a, k)) or True)
    return tmp_path, sent


# ---------- config ----------
def test_config_valid():
    assert CFG.adults == 2 and CFG.origin == "DEL" and CFG.destination == "DPS"
    assert len(build_specs(CFG)) == 8


def test_config_rejects_typo(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(Path(config_mod.CONFIG_PATH).read_text().replace("adults", "adutls"))
    with pytest.raises(ValueError):
        config_mod.load(p)


# ---------- parsing / validation ----------
def test_to_option_fits_and_ideal():
    spec = next(s for s in build_specs(CFG) if s.id == "out-0328-0s")
    o = to_option(AI_OUT, spec, CFG, "u")
    assert o.fits and o.ideal and o.nonstop and o.bag == "yes"
    assert o.duration_min == 485  # 20:45 IST -> 07:20 WITA = 10h35 local - 2h30 tz


def test_return_morning_not_fit():
    spec = next(s for s in build_specs(CFG) if s.kind == "ret")
    f = flight(52214, "Air India", seg("DPS", "DEL", "2027-04-03 08:40", "2027-04-03 14:15"))
    assert not to_option(f, spec, CFG, "u").fits


@pytest.mark.parametrize("bad", [
    flight(500, "Air India", seg("DEL", "DPS", "2027-03-28 20:45", "2027-03-29 07:20")),       # price too low
    flight(51046, "Air India", seg("BOM", "DPS", "2027-03-28 20:45", "2027-03-29 07:20")),     # wrong origin
    flight(51046, "Air India", seg("DEL", "DPS", "2027-03-27 20:45", "2027-03-28 07:20")),     # wrong date
    flight(51046, "Air India", seg("DEL", "DPS", "2027-03-28 20:45", "2027-03-28 20:50")),     # impossible duration
])
def test_to_option_rejects_garbage(bad):
    spec = next(s for s in build_specs(CFG) if s.id == "out-0328-0s")
    with pytest.raises(ValueError):
        to_option(bad, spec, CFG, "u")


def test_fetch_all_retries_then_records_error():
    calls = []

    def boom(q):
        calls.append(1)
        raise ConnectionError("nope")

    opts, errors = fetch_all(build_specs(CFG)[:1], CFG, fetch_fn=boom, sleep=lambda s: None)
    assert opts == [] and len(errors) == 1 and len(calls) == 3


# ---------- baggage ----------
@pytest.mark.parametrize("airlines,expected", [
    (["Air India"], "yes"), (["Air India Express"], "likely"), (["IndiGo"], "likely"),
    (["Singapore Airlines", "Scoot"], "no"), (["Vietjet"], "no"), (["THAI"], "yes"), (["Zzz Air"], "unknown"),
])
def test_baggage(airlines, expected):
    assert baggage.classify(airlines) == expected


# ---------- change detection ----------
def test_detect_drop_rise_newlow_target():
    state = {"last": {"best_trip": 100000}, "lows": {"best_trip": 98000}}
    ev = analyze.detect_changes({"best_trip": {"price": 95000}}, state, CFG)
    kinds = {e.kind for e in ev}
    assert "new_low" in kinds
    ev = analyze.detect_changes({"best_trip": {"price": 89000}}, state, CFG)
    assert "target" in {e.kind for e in ev}
    ev = analyze.detect_changes({"best_trip": {"price": 100500}}, state, CFG)
    assert ev == []  # below thresholds
    ev = analyze.detect_changes({"best_trip": {"price": 104000}}, state, CFG)
    assert [e.kind for e in ev] == ["rise"]


# ---------- end to end ----------
def test_pipeline_first_run_then_drop(tmp_store):
    tmp, sent = tmp_store
    state: dict = {}
    assert main_mod.run(CFG, state, NOW, fetch_fn=fake_fetch(), sleep=lambda s: None) == 0
    assert state["last"]["best_trip"] == 96096
    assert "started" in sent[-1][0][0]

    sent.clear()
    assert main_mod.run(CFG, state, NOW, fetch_fn=fake_fetch({"rt": 90000}), sleep=lambda s: None) == 0
    assert sent and "LOW" in sent[0][0][0]
    data = json.loads((tmp / "docs" / "data.json").read_text())
    assert data["current"]["best_trip"]["price"] == 90000
    assert len(data["history"]["best_trip"]) == 2
    assert (tmp / "data" / "series.csv").exists()


def test_partial_failure_does_not_fake_alert(tmp_store):
    _, sent = tmp_store
    state: dict = {}
    main_mod.run(CFG, state, NOW, fetch_fn=fake_fetch(), sleep=lambda s: None)
    sent.clear()
    # RT query fails -> best_trip would "jump" to the pricier combo; must not alert or overwrite.
    fails = {s.id for s in build_specs(CFG) if s.kind == "rt"}
    assert main_mod.run(CFG, state, NOW, fetch_fn=fake_fetch(fail_ids=fails), sleep=lambda s: None) == 0
    assert sent == [] and state["last"]["best_trip"] == 96096


def test_total_failure_alerts_after_threshold(tmp_store):
    _, sent = tmp_store
    state: dict = {}
    every = {s.id for s in build_specs(CFG)}
    codes = [main_mod.run(CFG, state, NOW, fetch_fn=fake_fetch(fail_ids=every), sleep=lambda s: None)
             for _ in range(CFG.failure_alert_after)]
    assert codes[-1] == 1 and codes[0] == 0
    assert len(sent) == 1 and "FAILING" in sent[0][0][0]
    main_mod.run(CFG, state, NOW, fetch_fn=fake_fetch(), sleep=lambda s: None)
    assert any("recovered" in s[0][0] for s in sent)


def test_stops_after_trip():
    assert main_mod.run(CFG, {}, datetime(2027, 4, 4, tzinfo=UTC), fetch_fn=lambda q: 1 / 0) == 0


def test_corrupt_state_recovers(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "state.json").write_text("{not json")
    assert store.load_state(d) == {}
    assert (d / "state.corrupt.json").exists()
