from __future__ import annotations

from datetime import timezone

from app.core.constants import BAR_MINUTES, MARKETS, TIMEFRAME
from app.data.bar import NormalizedBar


def validate_bar(bar: NormalizedBar) -> list[str]:
    errors: list[str] = []
    if bar.market not in MARKETS:
        errors.append(f"unsupported market: {bar.market}")
    if not bar.symbol.strip():
        errors.append("symbol is empty")
    if bar.timeframe != TIMEFRAME:
        errors.append(f"timeframe must be {TIMEFRAME}")
    if bar.timestamp.tzinfo is None:
        errors.append("timestamp must be timezone-aware")
    else:
        utc = bar.timestamp.astimezone(timezone.utc)
        if utc.second != 0 or utc.microsecond != 0 or utc.minute % BAR_MINUTES != 0:
            errors.append("timestamp is not aligned to a closed 5-minute boundary")
    for name in ("open", "high", "low", "close"):
        price = getattr(bar, name)
        if price <= 0:
            errors.append(f"{name} must be positive")
    if bar.high < bar.low:
        errors.append("high must be greater than or equal to low")
    if bar.high < bar.open:
        errors.append("high must be greater than or equal to open")
    if bar.high < bar.close:
        errors.append("high must be greater than or equal to close")
    if bar.low > bar.open:
        errors.append("low must be less than or equal to open")
    if bar.low > bar.close:
        errors.append("low must be less than or equal to close")
    if bar.volume is not None and bar.volume < 0:
        errors.append("volume must be zero or positive")
    if bar.vwap is not None and bar.vwap <= 0:
        errors.append("vwap must be positive")
    if bar.trade_count is not None and bar.trade_count < 0:
        errors.append("trade_count must be zero or positive")
    if not bar.provider.strip():
        errors.append("provider is empty")
    return errors
