from pydantic import BaseModel, Field


class BillingCheckoutRequest(BaseModel):
    plan_code: str = Field(..., min_length=1, max_length=50)


class BillingSyncRequest(BaseModel):
    order_code: int = Field(..., ge=1)
