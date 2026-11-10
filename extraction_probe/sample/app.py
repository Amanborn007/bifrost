"""Sample FastAPI application for extraction probe testing."""

from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel

from extraction_probe.sample.service import OrderService
from extraction_probe.sample.repository import OrderRepository
from extraction_probe.sample.events import EventBus

app = FastAPI(title="Order Service", version="1.0.0")


class OrderRequest(BaseModel):
    customer_id: str
    tenant_plan: str
    weight_kg: float
    shipping_method: str = "standard"


class OrderResponse(BaseModel):
    order_id: str
    status: str
    weight_kg: float


def get_order_service() -> OrderService:
    repo = OrderRepository()
    bus = EventBus()
    return OrderService(repo=repo, event_bus=bus)


@app.post("/orders", response_model=OrderResponse)
async def create_order(
    request: OrderRequest,
    service: OrderService = Depends(get_order_service),
) -> OrderResponse:
    """Create a new order."""
    result = await service.process_order(
        customer_id=request.customer_id,
        tenant_plan=request.tenant_plan,
        weight_kg=request.weight_kg,
        shipping_method=request.shipping_method,
    )
    return OrderResponse(
        order_id=result["id"],
        status=result["status"],
        weight_kg=result["weight_kg"],
    )


@app.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    service: OrderService = Depends(get_order_service),
) -> OrderResponse:
    """Retrieve an order by ID."""
    result = await service.get_order(order_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderResponse(
        order_id=result["id"],
        status=result["status"],
        weight_kg=result["weight_kg"],
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
