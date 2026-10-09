import asyncio
import hmac
import anyio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from .fleet import Conflict, Fleet
from .models import Model, Enroll, Assign, Binding, Action, Command, Recovery, Replacement, MockUpdate
from .storage import Storage
from .transport import BleFactory, MockFactory


class Connect(Model):
    address: str


def create_app(settings, data_directory, token, mock=False):
    data_directory.mkdir(parents=True, exist_ok=True)
    factory = MockFactory() if mock else BleFactory(settings)
    fleet = Fleet(settings, factory, Storage(data_directory / "bridge.sqlite3"), mock)

    @asynccontextmanager
    async def lifespan(app):
        await fleet.start()
        try:
            yield
        finally:
            await fleet.close()

    app = FastAPI(title="Blimp Bridge", version="0.1.0", lifespan=lifespan)
    app.state.fleet = fleet

    def authenticated(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "Bearer token required")

    auth = [Depends(authenticated)]

    @app.exception_handler(Conflict)
    async def conflict_handler(request, ex):
        return JSONResponse(status_code=409, content={"detail": str(ex)})

    @app.exception_handler(KeyError)
    async def missing_handler(request, ex):
        return JSONResponse(status_code=404, content={"detail": "Unknown ID"})

    @app.exception_handler(ValueError)
    async def value_handler(request, ex):
        return JSONResponse(status_code=422, content={"detail": str(ex)})

    @app.exception_handler(TimeoutError)
    @app.exception_handler(ConnectionError)
    async def transport_handler(request, ex):
        return JSONResponse(status_code=503, content={"detail": str(ex) or "Device timeout"})

    @app.get("/health")
    async def health():
        return {"status": "ok" if fleet.storage_ok else "degraded", "mode": "mock" if mock else "ble"}

    @app.get("/v1/fleet", dependencies=auth)
    async def snapshot():
        return fleet.snapshot()

    @app.post("/v1/discovery", dependencies=auth)
    async def discover():
        return await fleet.discover()

    @app.post("/v1/drones/enroll", dependencies=auth, status_code=201)
    async def enroll(request: Enroll):
        await fleet.enroll(request)
        return {"drone_id": request.drone_id}

    @app.post("/v1/drones/{drone_id}/connect", dependencies=auth)
    async def connect(drone_id: str, request: Connect):
        await fleet.connect(drone_id, request.address)
        return {"state": "CONNECTED"}

    @app.put("/v1/drones/{drone_id}/profile", dependencies=auth)
    async def update_profile(drone_id: str, request: Enroll):
        if drone_id != request.drone_id:
            raise Conflict("Device ID cannot change")
        await fleet.update_profile(request)
        return {"drone_id": drone_id}

    @app.post("/v1/drones/{drone_id}/release-recovered", dependencies=auth)
    async def release_recovered(drone_id: str, request: Recovery):
        if not request.physically_powered_off:
            raise Conflict("Physical recovery and power-off confirmation required")
        await fleet.disconnect_unassigned(drone_id, physical_off=True)
        return {"state": "DISCONNECTED"}

    @app.post("/v1/drones/{drone_id}/disconnect", dependencies=auth)
    async def disconnect(drone_id: str):
        await fleet.disconnect_unassigned(drone_id)
        return {"state": "DISCONNECTED"}

    @app.post("/v1/slots/{slot}/assign", dependencies=auth)
    async def assign(slot: str, request: Assign):
        return await fleet.assign(slot, request)

    @app.post("/v1/slots/{slot}/arm", dependencies=auth)
    async def arm(slot: str, request: Binding):
        return await fleet.arm(slot, request.expected_binding_epoch)

    @app.post("/v1/slots/{slot}/disarm", dependencies=auth)
    async def disarm(slot: str, request: Binding):
        return await fleet.disarm(slot, request.expected_binding_epoch)

    @app.post("/v1/slots/{slot}/actions", dependencies=auth)
    async def action(slot: str, request: Action):
        return fleet.action(slot, request)

    @app.post("/v1/slots/{slot}/commands", dependencies=auth, status_code=202)
    async def command(slot: str, request: Command):
        return fleet.command(slot, request)

    @app.get("/v1/operations/{operation_id}", dependencies=auth)
    async def operation(operation_id: str):
        return fleet.operations[operation_id]

    @app.post("/v1/slots/{slot}/swaps", dependencies=auth)
    async def swap(slot: str, request: Binding, idempotency_key: str = Header(min_length=1, max_length=100)):
        return await fleet.begin_swap(slot, request.expected_binding_epoch, idempotency_key)

    @app.post("/v1/swaps/{swap_id}/confirm-recovered", dependencies=auth)
    async def recover(swap_id: str, request: Recovery):
        return await fleet.recover(swap_id, request.physically_powered_off)

    @app.post("/v1/swaps/{swap_id}/replacement", dependencies=auth)
    async def replace(swap_id: str, request: Replacement):
        return await fleet.replacement(swap_id, request.drone_id)

    @app.get("/v1/events/recent", dependencies=auth)
    async def recent():
        return list(fleet.events)

    @app.websocket("/v1/events")
    async def events(ws: WebSocket):
        if not hmac.compare_digest(ws.headers.get("authorization", ""), "Bearer " + token):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = asyncio.Queue(maxsize=128)
        fleet.subscribers.add(queue)
        async def consume_disconnect():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
        receiver = asyncio.create_task(consume_disconnect())
        item = None
        try:
            await ws.send_json({"type": "snapshot", "fleet": fleet.snapshot()})
            while not receiver.done():
                item = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait([item, receiver], timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if item not in done:
                    item.cancel()
                    await asyncio.gather(item, return_exceptions=True)
                if receiver in done:
                    break
                await asyncio.wait_for(ws.send_json(item.result() if item in done else {"type": "heartbeat"}), 3)
                if item in done and item.result().get("type") == "resync_required":
                    await ws.close(code=1013)
                    break
        except (WebSocketDisconnect, TimeoutError, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            fleet.subscribers.discard(queue)
            with anyio.CancelScope(shield=True):
                receiver.cancel()
                pending = [receiver]
                if item is not None:
                    item.cancel()
                    pending.append(item)
                await asyncio.gather(*pending, return_exceptions=True)

    if mock:
        @app.post("/v1/mock/{drone_id}/battery", dependencies=auth)
        async def mock_battery(drone_id: str, request: MockUpdate):
            link = fleet.devices[drone_id].link
            link.voltage, link.valid = request.pack_voltage_v, request.measurement_valid
            if request.battery_session_id:
                link.battery_session = request.battery_session_id
            link.emit()
            return {"updated": True}

        @app.post("/v1/mock/{drone_id}/lose", dependencies=auth)
        async def mock_lose(drone_id: str):
            d = fleet.devices[drone_id]
            d.link.fail = True
            await d.link.close()
            return {"state": "LOST"}

    return app
