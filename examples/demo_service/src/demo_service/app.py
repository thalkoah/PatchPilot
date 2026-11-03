"""HTTP wrapper around the discount rules. It does not catch an unknown tier."""

from fastapi import FastAPI

from demo_service.discounts import discount_percent

app = FastAPI(title="Demo service", version="0.1.0")


@app.get("/discount")
def get_discount(customer_tier: str) -> dict[str, object]:
    percent = discount_percent(customer_tier)
    return {"customer_tier": customer_tier, "percent": percent}
