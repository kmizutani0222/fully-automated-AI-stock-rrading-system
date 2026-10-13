from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RankedSymbol:
    symbol: str
    net_return: float
    risk: float
    liquidity: float
    correlation: float


def rank_symbols(candidates: list[RankedSymbol]) -> list[RankedSymbol]:
    """Higher cost-adjusted return, lower risk, higher liquidity, and lower correlation rank first."""
    return sorted(candidates, key=_score, reverse=True)


def _score(candidate: RankedSymbol) -> float:
    risk = candidate.risk if candidate.risk > 0 else math.inf
    liquidity = math.log1p(max(candidate.liquidity, 0))
    diversity = max(0.0, 1 - abs(candidate.correlation))
    return candidate.net_return / risk * liquidity * diversity
