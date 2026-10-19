"""Detect multi-leg institutional structures from delayed prints.

A desk rarely buys a single contract: it trades packages — risk reversals,
vertical spreads, collars, strangles. With delayed data we can't see the
actual tape, so combos are inferred from co-occurring prints in the same
scan: same underlying + expiry, compatible strikes, and a side_bias that
agrees with the structure (buyish/sellish from last-vs-mid).

Combos are *evidence*, not proof: each combo carries a confidence and the
reasoning, and the institutional score gets a bonus instead of a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Leg:
    contract: str
    call_put: str
    strike: float
    side_bias: str
    premium_usd: float
    inst_score: float = 0.0


@dataclass
class Combo:
    combo_type: str          # RISK_REVERSAL | CALL_SPREAD | PUT_SPREAD | COLLAR | STRANGLE
    underlying: str
    expiry: str
    legs: list[Leg] = field(default_factory=list)
    direction: str = "NEUTRAL"   # BULLISH | BEARISH | NEUTRAL | VOL_LONG
    confidence: float = 0.0      # 0..1
    label_es: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "combo_type": self.combo_type,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "direction": self.direction,
            "confidence": round(self.confidence, 2),
            "label_es": self.label_es,
            "reason": self.reason,
            "premium_usd": round(sum(l.premium_usd for l in self.legs), 0),
            "legs": [
                {
                    "contract": l.contract,
                    "call_put": l.call_put,
                    "strike": l.strike,
                    "side_bias": l.side_bias,
                    "premium_usd": round(l.premium_usd, 0),
                }
                for l in self.legs
            ],
        }


def _leg(a: dict[str, Any]) -> Leg:
    return Leg(
        contract=str(a.get("contract") or ""),
        call_put=str(a.get("call_put") or "?"),
        strike=float(a.get("strike") or 0),
        side_bias=str(a.get("side_bias") or "unknown"),
        premium_usd=float(a.get("premium_usd") or 0),
        inst_score=float(a.get("inst_score") or 0),
    )


def _buyish(side: str) -> bool:
    return side in ("buyish", "buy")


def _sellish(side: str) -> bool:
    return side in ("sellish", "sell")


def _conf(legs: list[Leg], side_agrees: bool) -> float:
    """0..1 from premium balance and side agreement."""
    if not legs:
        return 0.0
    prems = [l.premium_usd for l in legs]
    balance = min(prems) / max(max(prems), 1.0)  # 1 = equal size legs
    size = min(1.0, sum(prems) / 250_000.0)      # full credit ≥ $250k package
    base = 0.35 + 0.35 * balance + 0.30 * size
    return round(max(0.05, min(0.95, base + (0.15 if side_agrees else -0.25))), 2)


def detect_combos(alerts: list[dict[str, Any]]) -> list[Combo]:
    """Find multi-leg structures among institutional alerts of one scan."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in alerts:
        if not a.get("contract"):
            continue
        key = (str(a.get("underlying") or "").upper(), str(a.get("expiry") or ""))
        groups.setdefault(key, []).append(a)

    combos: list[Combo] = []
    for (und, expiry), items in groups.items():
        legs = [_leg(a) for a in items]
        calls = [l for l in legs if l.call_put == "C"]
        puts = [l for l in legs if l.call_put == "P"]

        # --- Risk reversal: long call + short put (bullish) or long put + short call (bearish)
        for c in calls:
            for p in puts:
                if abs(c.strike - p.strike) / max(p.strike, 1) > 0.10:
                    continue
                if _buyish(c.side_bias) and _sellish(p.side_bias):
                    combos.append(Combo(
                        "RISK_REVERSAL", und, expiry, [c, p], "BULLISH",
                        _conf([c, p], True), "Risk reversal alcista",
                        f"call {c.strike:g} comprada + put {p.strike:g} vendida",
                    ))
                elif _buyish(p.side_bias) and _sellish(c.side_bias):
                    combos.append(Combo(
                        "RISK_REVERSAL", und, expiry, [p, c], "BEARISH",
                        _conf([p, c], True), "Risk reversal bajista",
                        f"put {p.strike:g} comprada + call {c.strike:g} vendida",
                    ))

        # --- Vertical spreads: two same-side legs, both bought, different strikes
        for side_legs, cp, name, label in (
            (calls, "C", "CALL_SPREAD", "Call spread"),
            (puts, "P", "PUT_SPREAD", "Put spread"),
        ):
            bought = [l for l in side_legs if _buyish(l.side_bias)]
            for i in range(len(bought)):
                for j in range(i + 1, len(bought)):
                    a, b = bought[i], bought[j]
                    if abs(a.strike - b.strike) / max(a.strike, 1) < 0.02:
                        continue
                    lo, hi = sorted([a, b], key=lambda l: l.strike)
                    direction = "BULLISH" if cp == "C" else "BEARISH"
                    combos.append(Combo(
                        name, und, expiry, [lo, hi], direction,
                        _conf([lo, hi], True), f"{label} {direction.lower()}",
                        f"comprado {lo.strike:g}/{hi.strike:g}",
                    ))

        # --- Collar: long put + short call (protective, mildly bullish/neutral).
        # Skipped when the same pair already qualified as a risk reversal.
        rr_pairs = {
            tuple(sorted(l.contract for l in cb.legs))
            for cb in combos if cb.combo_type == "RISK_REVERSAL"
        }
        for p in puts:
            for c in calls:
                if _buyish(p.side_bias) and _sellish(c.side_bias):
                    if tuple(sorted([p.contract, c.contract])) in rr_pairs:
                        continue
                    combos.append(Combo(
                        "COLLAR", und, expiry, [p, c], "NEUTRAL",
                        _conf([p, c], True) * 0.9, "Collar (cobertura)",
                        f"put {p.strike:g} comprada + call {c.strike:g} vendida",
                    ))

        # --- Long strangle/straddle: call + put both bought, similar strike
        for c in calls:
            for p in puts:
                if not (_buyish(c.side_bias) and _buyish(p.side_bias)):
                    continue
                if abs(c.strike - p.strike) / max(p.strike, 1) > 0.06:
                    continue
                # avoid double counting with risk reversals (those need a sell leg)
                combos.append(Combo(
                    "STRANGLE", und, expiry, [c, p], "VOL_LONG",
                    _conf([c, p], True), "Strangle comprado (apuesta a movimiento)",
                    f"call {c.strike:g} + put {p.strike:g} comprados",
                ))

    # Deduplicate: keep the highest-confidence combo per leg set
    seen: dict[tuple, Combo] = {}
    for cb in combos:
        key = (cb.combo_type, tuple(sorted(l.contract for l in cb.legs)))
        if key not in seen or cb.confidence > seen[key].confidence:
            seen[key] = cb
    out = sorted(seen.values(), key=lambda c: c.confidence, reverse=True)
    return out


def combos_for_alert(alert: dict[str, Any], combos: list[Combo]) -> list[Combo]:
    """Combos that include this alert's contract."""
    contract = str(alert.get("contract") or "")
    return [c for c in combos if any(l.contract == contract for l in c.legs)]
