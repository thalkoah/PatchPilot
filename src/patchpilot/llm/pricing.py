"""Local price table. A missing or unverified entry never becomes a number."""

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal("0.000001")


class ModelPrice(BaseModel):
    """USD per million tokens. `verified` must be true or the entry is ignored."""

    model_config = ConfigDict(extra="forbid")

    prompt_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    verified: bool


@dataclass(frozen=True, slots=True)
class PricingTable:
    """Prices the operator verified. Keys are `provider:model`."""

    prices: dict[str, ModelPrice]


def load_pricing(path: str | None) -> PricingTable | None:
    """Read a local JSON file. Any problem yields no prices, not a guessed cost."""
    if path is None:
        return None
    file = Path(path)
    try:
        raw = file.read_text(encoding="utf-8")
        payload: Any = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("pricing file rejected")
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        logger.warning("pricing file rejected")
        return None
    prices = payload.get("prices")
    if not isinstance(prices, dict) or not prices:
        logger.warning("pricing file rejected")
        return None
    parsed: dict[str, ModelPrice] = {}
    for key, value in prices.items():
        if not isinstance(key, str) or ":" not in key:
            logger.warning("pricing file rejected")
            return None
        try:
            item = ModelPrice.model_validate(value)
        except ValidationError:
            logger.warning("pricing file rejected")
            return None
        if item.verified is not True:
            continue
        parsed[key] = item
    if not parsed:
        logger.warning("pricing file rejected")
        return None
    return PricingTable(prices=parsed)


def estimate_cost_usd(
    table: PricingTable | None,
    *,
    provider: str,
    model: str,
    prompt_tokens: int | None,
    output_tokens: int | None,
) -> Decimal | None:
    """Return a cost only for a verified price and reported token counts."""
    if table is None or prompt_tokens is None or output_tokens is None:
        return None
    price = table.prices.get(f"{provider}:{model}")
    if price is None:
        return None
    try:
        cost = (
            Decimal(prompt_tokens) * price.prompt_usd_per_million
            + Decimal(output_tokens) * price.output_usd_per_million
        ) / _MILLION
    except (InvalidOperation, ArithmeticError):
        return None
    if not cost.is_finite() or cost < 0:
        return None
    return cost.quantize(_QUANTUM)
