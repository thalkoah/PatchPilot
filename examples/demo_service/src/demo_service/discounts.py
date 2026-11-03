"""Discount rules for the demo service.

Known tiers return a percent. An unknown tier is not validated here: the lookup
raises KeyError, and the HTTP layer turns that into a 500 response. The intended
behavior is HTTP 422 with a validation error.
"""

KNOWN_TIERS: dict[str, int] = {
    "standard": 0,
    "silver": 10,
    "gold": 20,
}


def discount_percent(customer_tier: str) -> int:
    return KNOWN_TIERS[customer_tier]
