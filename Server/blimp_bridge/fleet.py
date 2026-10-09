import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from .battery import BatteryMonitor
from .models import Enroll, Telemetry


def uid():
    return uuid.uuid4().hex


class Conflict(Exception):
    pass


@dataclass
class Slot:
    drone_id: str | None = None
    epoch: str = field(default_factory=uid)
    state: str = "EMPTY"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    action: dict | None = None
    last_command_seq: int = -1


@dataclass
class Device:
    link: object
    monitor: BatteryMonitor
    connection_id: str = field(default_factory=uid)
    telemetry: Telemetry | None = None
    last_seen: float = 0
    state: str = "CONNECTED"
    battery_state: str = "UNKNOWN"
    session: str | None = None
    wire_seq: int = 0
    pending: dict | None = None
    task: asyncio.Task | None = None
    io_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sync_time: float = 0
    sync_uptime: int = 0
    sync_rtt_ms: float = 0
    last_deadline: float = 0


class Fleet:
    def __init__(self, settings, factory, storage, mock=False):
        self.settings, self.factory, self.storage, self.mock = settings, factory, storage, mock
        self.server_epoch = uid()
        self.registry = {}
        self.devices = {}
        self.slots = {f"slot-{i}": Slot() for i in range(1, 5)}
        self.swaps = {}
        self.operations = {}
        self.events = deque(maxlen=500)
        self.subscribers = set()
        self.event_queue = asyncio.Queue(maxsize=2048)
        self.storage_ok = True
        self.manager_lock = asyncio.Lock()
        self.tasks = []

    async def start(self):
        await self.storage.init()
        self.registry = {r["drone_id"]: Enroll.model_validate(r) for r in await self.storage.registry()}
        self.tasks = [asyncio.create_task(self.persist_events()), asyncio.create_task(self.watchdog())]

    async def close(self):
        # One failed disconnect must not prevent closing the other links.
        async def stop(d):
            if d.task:
                d.task.cancel()
                await asyncio.gather(d.task, return_exceptions=True)
            try:
                await self.rpc(d, {"type": "disarm"})
            except Exception:
                pass  # ESP32 expiry watchdog remains the final authority.
            try:
                await asyncio.wait_for(d.link.close(), 3)
            except Exception:
                pass
        await asyncio.gather(*(stop(d) for d in list(self.devices.values())))
        try:
            await asyncio.wait_for(self.event_queue.join(), 2)
        except TimeoutError:
            pass
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def emit(self, kind, **payload):
        event = {"event_id": uid(), "server_epoch": self.server_epoch, "type": kind,
                 "utc_timestamp": time.time(), **payload}
        self.events.append(event)
        try:
            self.event_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.storage_ok = False
        for q in list(self.subscribers):
            if q.full():
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "resync_required"})
            else:
                q.put_nowait(event)
        return event

    async def persist_events(self):
        while True:
            item = await self.event_queue.get()
            try:
                await self.storage.event(item)
            except Exception:
                self.storage_ok = False
            finally:
                self.event_queue.task_done()

    async def enroll(self, request):
        async with self.manager_lock:
            if request.drone_id in self.registry:
                raise Conflict("Already enrolled; profile updates require a separate disarmed maintenance procedure")
            if len(self.registry) >= 16:
                raise Conflict("Registry capacity 16 reached")
            await self.storage.enroll(request)
            self.registry[request.drone_id] = request
            self.emit("drone.enrolled", drone_id=request.drone_id)

    async def discover(self):
        async with self.manager_lock:
            return await self.factory.discover()

    async def update_profile(self, request):
        async with self.manager_lock:
            if request.drone_id not in self.registry:
                raise KeyError(request.drone_id)
            if request.drone_id in self.devices or self.slot_for(request.drone_id):
                raise Conflict("Disconnect and release role before changing profiles")
            await self.storage.execute("UPDATE registry SET body=? WHERE id=?", (request.model_dump_json(), request.drone_id))
            self.registry[request.drone_id] = request
            self.emit("drone.profile_updated", drone_id=request.drone_id)

    async def connect(self, drone_id, address):
        async with self.manager_lock:
            if drone_id not in self.registry:
                raise Conflict("Enroll device first")
            if drone_id in self.devices or len(self.devices) >= 4:
                raise Conflict("Already connected or four BLE connections occupied")
            link = self.factory.create(address)
            d = Device(link=link, monitor=BatteryMonitor(self.registry[drone_id].battery))
            link.on_telemetry = lambda msg: self.telemetry(drone_id, d, msg)
            link.on_lost = lambda: self.lost(drone_id, d)
            self.devices[drone_id] = d
            try:
                await asyncio.wait_for(link.open(), 30)
                if link.identity["drone_id"] != drone_id:
                    raise Conflict("Identity mismatch; address is not the registered drone")
                await self.sync(d)
                d.task = asyncio.create_task(self.sender(drone_id, d))
                self.emit("drone.connected", drone_id=drone_id, connection_id=d.connection_id)
            except BaseException:
                self.devices.pop(drone_id, None)
                try:
                    await asyncio.wait_for(link.close(), 3)
                except Exception:
                    pass
                raise

    def telemetry(self, drone_id, d, raw):
        if self.devices.get(drone_id) is not d or d.state == "LOST":
            return
        try:
            t = Telemetry.model_validate(raw)
            if t.drone_id != drone_id or not d.link.identity:
                return
            if t.boot_id != d.link.identity["boot_id"]:
                self.lost(drone_id, d)
                return
            if d.telemetry and (t.telemetry_seq <= d.telemetry.telemetry_seq or t.uptime_ms < d.telemetry.uptime_ms):
                return
            if d.session and d.telemetry and t.battery_session_id != d.telemetry.battery_session_id:
                self.lost(drone_id, d)
                return
        except (ValueError, TypeError):
            return
        d.telemetry, d.last_seen = t, time.monotonic()
        key = self.slot_for(drone_id)
        if d.session and key and self.slots[key].state == "ACTIVE" and (not t.armed or t.control_session != d.session):
            d.session = None
            self.slots[key].state, self.slots[key].action = "RECOVERY_REQUIRED", None
            self.cancel_pending(d, "device_disarmed_or_session_changed")
            self.emit("slot.device_disarmed", slot_id=key, drone_id=drone_id)
        if d.state == "STALE":
            d.state = "CONNECTED"
            self.emit("drone.telemetry_restored", drone_id=drone_id)
        state = d.monitor.update(t, d.last_seen)
        if state != d.battery_state:
            d.battery_state = state
            self.emit("battery." + state.lower(), drone_id=drone_id, slot_id=self.slot_for(drone_id),
                      pack_voltage_v=t.pack_voltage_v, requires_operator_action=state != "NORMAL")

    def lost(self, drone_id, d):
        if self.devices.get(drone_id) is d and d.state != "LOST":
            d.state = "LOST"
            self.cancel_pending(d, "connection_lost")
            d.session = None
            slot_id = self.slot_for(drone_id)
            if slot_id:
                self.slots[slot_id].action = None
            self.emit("drone.lost", drone_id=drone_id, slot_id=slot_id)

    def slot_for(self, drone_id):
        return next((k for k, s in self.slots.items() if s.drone_id == drone_id), None)

    def slot(self, key):
        if key not in self.slots:
            raise Conflict("Unknown slot")
        return self.slots[key]

    def check_epoch(self, slot, epoch):
        if epoch != slot.epoch:
            raise Conflict("Stale binding epoch; refresh fleet snapshot")

    def ready(self, drone_id, require_battery=True):
        d = self.devices.get(drone_id)
        if not d or d.state != "CONNECTED" or not d.telemetry or time.monotonic() - d.last_seen > self.settings.stale_seconds:
            raise Conflict("Fresh connected telemetry required")
        if require_battery and d.battery_state != "NORMAL":
            raise Conflict("Battery must be calibrated, fresh and NORMAL")
        return d

    async def assign(self, key, request):
        s = self.slot(key)
        async with s.lock:
            self.check_epoch(s, request.expected_binding_epoch)
            if s.drone_id or s.state != "EMPTY" or self.slot_for(request.drone_id):
                raise Conflict("Role/device already assigned or replacement workflow required")
            d = self.ready(request.drone_id)
            if d.telemetry.armed:
                raise Conflict("Device must be disarmed")
            s.drone_id, s.epoch, s.state = request.drone_id, uid(), "READY_DISARMED"
            self.emit("slot.changed", slot_id=key, drone_id=s.drone_id, binding_epoch=s.epoch)
            return self.snapshot()["slots"][key]

    async def rpc(self, d, msg):
        return await asyncio.wait_for(d.link.rpc(msg), self.settings.rpc_timeout)

    async def sync(self, d):
        before = time.monotonic()
        reply = await self.rpc(d, {"type": "sync"})
        after = time.monotonic()
        rtt = (after - before) * 1000
        if rtt > self.settings.max_clock_rtt_ms or not isinstance(reply.get("uptime_ms"), int):
            raise Conflict("Clock sync too uncertain")
        d.sync_time, d.sync_uptime, d.sync_rtt_ms = after, reply["uptime_ms"], rtt

    def deadline(self, d, remaining_ms):
        age = time.monotonic() - d.sync_time
        if age > 10:
            raise Conflict("Clock synchronization expired")
        # Conservative lower estimate of peripheral uptime + margin; synchronize often.
        return int(d.sync_uptime + age * 1000 + remaining_ms - d.sync_rtt_ms - 10)

    async def arm(self, key, epoch):
        s = self.slot(key)
        async with s.lock:
            self.check_epoch(s, epoch)
            if s.state != "READY_DISARMED":
                raise Conflict("Role not ready to arm")
            if not self.storage_ok or (not self.mock and not self.settings.hardware_arming_enabled):
                raise Conflict("Hardware arming disabled or event storage unhealthy")
            d = self.ready(s.drone_id)
            p = self.registry[s.drone_id].vehicle
            if not p.validated or not p.expiry_behavior or p.profile_hash != d.link.identity["profile_hash"]:
                raise Conflict("Validated matching vehicle profile required")
            if not {"surge_yaw_rate", "expiry_watchdog"}.issubset(d.link.identity["capabilities"]):
                raise Conflict("Required firmware capabilities missing")
            async with d.io_lock:
                await self.sync(d)
                session = uid()
                try:
                    reply = await self.rpc(d, {"type": "arm", "control_session": session,
                        "expires_at_uptime_ms": self.deadline(d, self.settings.command_ttl_ms)})
                    if reply.get("armed") is not True:
                        raise Conflict("Arm not confirmed")
                except Exception:
                    s.state = "RECOVERY_REQUIRED"
                    raise
                d.session, d.wire_seq = session, 0
                d.last_deadline = time.monotonic() + self.settings.command_ttl_ms / 1000
                s.state, s.action, s.last_command_seq = "ACTIVE", None, -1
            self.emit("slot.armed", slot_id=key, drone_id=s.drone_id)
            return {"state": s.state}

    def action(self, key, request):
        s = self.slot(key)
        self.check_epoch(s, request.expected_binding_epoch)
        if s.state != "ACTIVE":
            raise Conflict("Role not active")
        self.ready(s.drone_id)
        s.action = {"action_id": uid(), "deadline": time.monotonic() + request.duration_ms / 1000}
        return {"action_id": s.action["action_id"], "duration_ms": request.duration_ms}

    def command(self, key, request):
        s = self.slot(key)
        self.check_epoch(s, request.expected_binding_epoch)
        if request.server_epoch != self.server_epoch or s.state != "ACTIVE":
            raise Conflict("Wrong server epoch or role inactive")
        d = self.ready(s.drone_id, require_battery=False)
        if d.battery_state not in ("NORMAL", "WARNING") or not d.session:
            raise Conflict("Battery status or control session blocks commands")
        if not s.action or s.action["action_id"] != request.action_id or time.monotonic() >= s.action["deadline"]:
            raise Conflict("Action missing, replaced, or expired")
        if request.command_seq <= s.last_command_seq:
            raise Conflict("Duplicate or old client command sequence")
        p = self.registry[s.drone_id].vehicle
        if not (-p.surge_max if p.reverse_supported else 0) <= request.surge_norm <= p.surge_max or abs(request.yaw_rate_rad_s) > p.yaw_rate_max:
            raise Conflict("Setpoint outside vehicle limits")
        s.last_command_seq = request.command_seq
        op = self.operation("setpoint")
        self.cancel_pending(d, "superseded")
        d.pending = {"operation_id": op["operation_id"], "slot_id": key, "binding_epoch": s.epoch,
                     "session": d.session, "action_id": request.action_id, "action_deadline": s.action["deadline"],
                     "deadline": min(s.action["deadline"], time.monotonic() + self.settings.command_ttl_ms / 1000),
                     "surge_norm": request.surge_norm, "yaw_rate_rad_s": request.yaw_rate_rad_s}
        return op

    def operation(self, kind):
        if len(self.operations) >= 1000:
            done = next((k for k, v in self.operations.items() if v["status"] != "accepted"), None)
            if done is None:
                raise Conflict("Too many pending operations")
            del self.operations[done]
        op = {"operation_id": uid(), "type": kind, "status": "accepted"}
        self.operations[op["operation_id"]] = op
        return op.copy()

    def finish(self, op_id, status, reason=None):
        self.operations[op_id].update(status=status, reason=reason)
        self.emit("command." + status, operation_id=op_id, reason=reason)

    def cancel_pending(self, d, reason):
        if d.pending:
            self.finish(d.pending["operation_id"], "rejected", reason)
            d.pending = None

    async def sender(self, drone_id, d):
        while True:
            await asyncio.sleep(1 / self.settings.command_hz)
            async with d.io_lock:
                item, d.pending = d.pending, None
                if not item:
                    continue
                try:
                    s = self.slot(item["slot_id"])
                    if s.state != "ACTIVE" or s.epoch != item["binding_epoch"] or d.session != item["session"]:
                        raise Conflict("Role or session changed")
                    self.ready(drone_id, require_battery=False)
                    if d.battery_state not in ("NORMAL", "WARNING"):
                        raise Conflict("Battery status blocks command")
                    if not s.action or s.action["action_id"] != item["action_id"]:
                        raise Conflict("Action replaced")
                    if time.monotonic() - d.sync_time > 5:
                        await self.sync(d)
                    remaining = (item["deadline"] - time.monotonic()) * 1000
                    if remaining <= d.sync_rtt_ms + 10:
                        raise Conflict("Command expired before send")
                    d.wire_seq += 1
                    await self.rpc(d, {"type": "setpoint", "control_session": d.session,
                        "command_seq": d.wire_seq, "expires_at_uptime_ms": self.deadline(d, remaining),
                        "surge_norm": item["surge_norm"], "yaw_rate_rad_s": item["yaw_rate_rad_s"]})
                    d.last_deadline = item["deadline"]
                    self.finish(item["operation_id"], "applied")
                except Exception as ex:
                    self.finish(item["operation_id"], "rejected", str(ex))
                    if not isinstance(ex, Conflict):
                        self.lost(drone_id, d)

    async def disarm_device(self, drone_id):
        d = self.devices[drone_id]
        self.cancel_pending(d, "disarm")
        async with d.io_lock:
            reply = await self.rpc(d, {"type": "disarm"})
            if reply.get("armed") is not False:
                raise Conflict("Disarm not confirmed")
            d.session = None

    async def disarm(self, key, epoch):
        s = self.slot(key)
        async with s.lock:
            self.check_epoch(s, epoch)
            if not s.drone_id:
                raise Conflict("Empty role")
            s.action, s.state = None, "DISARMING"
            try:
                await self.disarm_device(s.drone_id)
                s.state = "READY_DISARMED"
            except Exception:
                s.state = "RECOVERY_REQUIRED"
                raise
            self.emit("slot.disarmed", slot_id=key)
            return {"state": s.state}

    async def begin_swap(self, key, epoch, idempotency_key):
        s = self.slot(key)
        async with s.lock:
            previous = next((v for v in self.swaps.values() if v["idempotency_key"] == idempotency_key), None)
            if previous:
                if previous["slot_id"] != key or previous["original_epoch"] != epoch:
                    raise Conflict("Idempotency key reused for another request")
                return previous
            self.check_epoch(s, epoch)
            if not s.drone_id or s.state in ("DRAINING", "WAITING_REPLACEMENT"):
                raise Conflict("Role unavailable for swap")
            swap = {"swap_id": uid(), "slot_id": key, "old_drone_id": s.drone_id,
                    "original_epoch": epoch, "idempotency_key": idempotency_key, "state": "AWAITING_RECOVERY"}
            self.swaps[swap["swap_id"]] = swap
            s.state, s.action = "DRAINING", None
            self.cancel_pending(self.devices[s.drone_id], "swap_started")
            self.emit("swap.progress", **{k: v for k, v in swap.items() if k != "idempotency_key"})
            return swap.copy()

    async def recover(self, swap_id, physical_off):
        swap = self.swaps[swap_id]
        s = self.slot(swap["slot_id"])
        async with s.lock:
            if swap["state"] == "WAITING_REPLACEMENT":
                return swap.copy()
            if swap["state"] != "AWAITING_RECOVERY":
                raise Conflict("Invalid swap phase")
            drone_id = swap["old_drone_id"]
            d = self.devices[drone_id]
            try:
                await self.disarm_device(drone_id)
            except Exception:
                if not physical_off:
                    raise Conflict("No disarm ACK: confirm physical recovery AND power off")
            if d.task:
                d.task.cancel()
                await asyncio.gather(d.task, return_exceptions=True)
            # Keep occupied capacity if disconnect is not confirmed.
            await asyncio.wait_for(d.link.close(), 3)
            self.devices.pop(drone_id)
            s.drone_id, s.epoch, s.state = None, uid(), "WAITING_REPLACEMENT"
            swap["state"] = "WAITING_REPLACEMENT"
            self.emit("swap.progress", swap_id=swap_id, state=swap["state"], slot_id=swap["slot_id"])
            return swap.copy()

    async def replacement(self, swap_id, drone_id):
        swap = self.swaps[swap_id]
        s = self.slot(swap["slot_id"])
        async with s.lock:
            if swap["state"] == "COMPLETE" and swap.get("new_drone_id") == drone_id:
                return swap.copy()
            if not self.storage_ok or swap["state"] != "WAITING_REPLACEMENT" or self.slot_for(drone_id):
                raise Conflict("Replacement not ready")
            d = self.ready(drone_id)
            if d.telemetry.armed:
                raise Conflict("Replacement must be disarmed")
            s.drone_id, s.epoch, s.state = drone_id, uid(), "READY_DISARMED"
            s.action, s.last_command_seq = None, -1
            swap.update(state="COMPLETE", new_drone_id=drone_id)
            self.emit("slot.changed", slot_id=swap["slot_id"], drone_id=drone_id, binding_epoch=s.epoch)
            return swap.copy()

    async def disconnect_unassigned(self, drone_id, physical_off=False):
        async with self.manager_lock:
            if self.slot_for(drone_id):
                raise Conflict("Assigned device must use swap/recovery workflow")
            d = self.devices[drone_id] if physical_off else self.ready(drone_id, require_battery=False)
            if not physical_off and d.telemetry.armed:
                raise Conflict("Cannot release armed device")
            d.state = "DISCONNECTING"
            try:
                if not physical_off:
                    await self.disarm_device(drone_id)
            except Exception:
                d.state = "LOST"
                raise
            if d.task:
                d.task.cancel()
                await asyncio.gather(d.task, return_exceptions=True)
            try:
                await asyncio.wait_for(d.link.close(), 3)
            except Exception:
                d.state = "LOST"
                raise
            self.devices.pop(drone_id)

    async def watchdog(self):
        while True:
            await asyncio.sleep(.1)
            now = time.monotonic()
            for drone_id, d in list(self.devices.items()):
                if d.state == "CONNECTED" and now - d.last_seen > self.settings.stale_seconds:
                    d.state = "STALE"
                    self.cancel_pending(d, "telemetry_stale")
                    self.emit("battery.stale", drone_id=drone_id)
                key = self.slot_for(drone_id)
                if key and self.slots[key].state == "ACTIVE":
                    if d.state != "CONNECTED" or d.battery_state not in ("NORMAL", "WARNING") or now > d.last_deadline:
                        self.slots[key].state, self.slots[key].action = "RECOVERY_REQUIRED", None
                        self.cancel_pending(d, "watchdog")
                        d.session = None
                        self.emit("slot.control_expired", slot_id=key, drone_id=drone_id)

    def snapshot(self):
        now = time.monotonic()
        return {"server_epoch": self.server_epoch, "mode": "mock" if self.mock else "ble",
            "storage_ok": self.storage_ok, "hardware_arming_enabled": self.settings.hardware_arming_enabled,
            "slots": {key: {"drone_id": s.drone_id, "binding_epoch": s.epoch, "state": s.state} for key, s in self.slots.items()},
            "drones": {key: {"name": r.name, "connection_state": self.devices[key].state if key in self.devices else "DISCONNECTED",
                "connection_id": self.devices[key].connection_id if key in self.devices else None,
                "battery_state": ("STALE" if now - self.devices[key].last_seen > self.settings.stale_seconds else self.devices[key].battery_state) if key in self.devices else "UNKNOWN",
                "telemetry_age_ms": round((now - self.devices[key].last_seen) * 1000) if key in self.devices and self.devices[key].last_seen else None,
                "telemetry": self.devices[key].telemetry.model_dump() if key in self.devices and self.devices[key].telemetry else None} for key, r in self.registry.items()}}
