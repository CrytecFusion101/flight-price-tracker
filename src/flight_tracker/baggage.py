"""Heuristic: does the base economy fare include a checked bag?

Google Flights does not expose baggage allowance in results, so we classify
by airline. Always verify on the booking page.
  yes     – full-service carrier, checked bag included in economy
  likely  – usually included on international fares (fare-family dependent)
  no      – low-cost carrier, checked bag costs extra
  unknown – not in the table
"""

from __future__ import annotations

YES = (
    "Air India", "Singapore Airlines", "Malaysia Airlines", "THAI", "Garuda",
    "Cathay", "Emirates", "Qatar", "SriLankan", "Vistara", "China Southern",
    "China Eastern", "Air China", "Korean Air", "Japan Airlines", "Etihad",
)
LIKELY = ("IndiGo", "Air India Express", "Batik", "Malindo", "Lion Air", "Thai Smile")
NO = ("Vietjet", "VietJet", "Scoot", "AirAsia", "Air Asia", "Jetstar", "Thai Lion", "Citilink")

_RANK = {"no": 0, "unknown": 1, "likely": 2, "yes": 3}


def classify_one(airline: str) -> str:
    # Check most specific lists first ("Air India Express" before "Air India").
    for names, verdict in ((NO, "no"), (LIKELY, "likely"), (YES, "yes")):
        if any(n.lower() in airline.lower() for n in names):
            return verdict
    return "unknown"


def classify(airlines: list[str]) -> str:
    """Worst verdict across all carriers on the itinerary."""
    if not airlines:
        return "unknown"
    return min((classify_one(a) for a in airlines), key=_RANK.__getitem__)


def bag_ok(verdict: str) -> bool:
    return verdict != "no"
