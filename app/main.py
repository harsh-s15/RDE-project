"""Toy order shop: the system under test.

Orders move CREATED -> PAID -> ALLOCATED -> SHIPPED -> DELIVERED. Three planted bugs are toggled
by env BUGS=allocation,stale_ui,deliver_500. Log lines carry the X-Test-Run-Id header so the
tester can pull its own run's logs without reading code.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

load_dotenv()
BUGS = {b.strip() for b in os.getenv("BUGS", "").split(",") if b.strip()}
app = FastAPI(title="Toy order shop")
orders: dict[str, dict] = {}
views: dict[str, str] = {}  # customer-facing status cache, polled by the customer page
logs: list[dict] = []


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log(run_id: str, level: str, msg: str) -> None:
    logs.append({"at": now(), "run_id": run_id, "level": level, "msg": msg})
    print(f"{level:5} [{run_id or '-'}] {msg}")


@app.middleware("http")
async def tag_and_log(request: Request, call_next):
    request.state.run_id = run_id = request.headers.get("x-test-run-id", "")
    line = f"{request.method} {request.url.path}"
    try:
        response = await call_next(request)
    except Exception as e:  # unhandled bug -> 500; the cause appears only in server logs
        log(run_id, "ERROR", f"{line} -> 500 {type(e).__name__}: {e}")
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    if request.method != "GET":  # pages poll every second; logging GETs would drown the signal
        log(run_id, "INFO", f"{line} -> {response.status_code}")
    return response


class NewOrder(BaseModel):
    customer_name: str
    email: str
    phone: str
    address: str


def public(order: dict) -> dict:
    return {k: v for k, v in order.items() if k != "run_id"}


def get_order(oid: str) -> dict:
    if oid not in orders:
        raise HTTPException(404, f"order {oid} not found")
    return orders[oid]


def move(order: dict, frm: str, to: str, refresh_view: bool = True) -> dict:
    if order["status"] != frm:
        raise HTTPException(409, f"cannot move order from {order['status']} to {to}")
    order["status"] = to
    order["history"].append({"status": to, "at": now()})
    if refresh_view:
        views[order["id"]] = to
    return public(order)


@app.post("/api/orders")
async def create_order(body: NewOrder, request: Request):
    oid, run_id = uuid.uuid4().hex[:6], request.state.run_id
    orders[oid] = {"id": oid, **body.model_dump(), "status": "CREATED",
                   "history": [{"status": "CREATED", "at": now()}], "run_id": run_id}
    views[oid] = "CREATED"
    log(run_id, "INFO", f"order {oid} created for {body.customer_name} <{body.email}>")
    return public(orders[oid])


@app.post("/api/orders/{oid}/pay")
async def pay(oid: str):
    order = get_order(oid)
    paid = move(order, "CREATED", "PAID")
    asyncio.create_task(allocate_later(order))  # system transition: no button triggers it
    return paid


async def allocate_later(order: dict) -> None:
    await asyncio.sleep(1)
    try:
        if "allocation" in BUGS:  # PLANTED BUG: allocation crashes; the order silently stays PAID
            raise RuntimeError(f"AllocationError: no warehouse slot for order {order['id']}")
        move(order, "PAID", "ALLOCATED")
        log(order["run_id"], "INFO", f"order {order['id']} allocated to warehouse BLR-1")
    except Exception as e:
        log(order["run_id"], "ERROR", f"background allocation failed: {e}")


@app.post("/api/orders/{oid}/ship")
async def ship(oid: str):  # PLANTED BUG stale_ui: the customer-facing cache is not refreshed
    return move(get_order(oid), "ALLOCATED", "SHIPPED", refresh_view="stale_ui" not in BUGS)


@app.post("/api/orders/{oid}/deliver")
async def deliver(oid: str):
    order = get_order(oid)
    if "deliver_500" in BUGS:  # PLANTED BUG: reads a field that was never set -> KeyError -> 500
        order["courier_id"]
    return move(order, "SHIPPED", "DELIVERED")


@app.get("/api/orders/{oid}")
async def read_order(oid: str):
    return public(get_order(oid))  # backend ground truth, used by the tester's checks


@app.get("/api/orders/{oid}/view")
async def read_view(oid: str):
    return {"id": get_order(oid)["id"], "status": views[oid]}


@app.get("/api/debug/logs")
async def read_logs(run_id: str):
    return [entry for entry in logs if entry["run_id"] == run_id]


@app.get("/")
@app.get("/admin")
async def page():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
