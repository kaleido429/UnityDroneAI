import asyncio
import time
import uuid
import cbor2
from bleak import BleakClient, BleakScanner
from .codec import Reassembler, frames


class BleLink:
    def __init__(self, device, settings):
        self.settings = settings
        self.client = BleakClient(device, disconnected_callback=lambda _: self.on_lost(), pair=True)
        self.on_lost = lambda: None
        self.on_telemetry = lambda _: None
        self.pending = {}
        self.lock = asyncio.Lock()
        self.mid = 0
        self.assemblers = {"telemetry": Reassembler(), "ack": Reassembler()}
        self.identity = None

    async def open(self):
        await self.client.connect()
        self.identity = cbor2.loads(bytes(await self.client.read_gatt_char(self.settings.identity_uuid)))
        if not isinstance(self.identity, dict) or self.identity.get("protocol_version") != 1:
            raise ValueError("Unsupported identity/protocol")
        for key in ("drone_id", "boot_id", "profile_hash", "capabilities"):
            if key not in self.identity:
                raise ValueError("Incomplete Identity")
        await self.client.start_notify(self.settings.telemetry_uuid, lambda _, data: self.receive("telemetry", data))
        await self.client.start_notify(self.settings.ack_uuid, lambda _, data: self.receive("ack", data))

    def receive(self, kind, data):
        try:
            msg = self.assemblers[kind].feed(bytes(data))
            if msg is None:
                return
            if kind == "telemetry":
                # Wire units are integers; JSON/API use physical units.
                msg["pack_voltage_v"] = None if msg.get("pack_voltage_mv") is None else msg.pop("pack_voltage_mv") / 1000
                msg.pop("pack_voltage_mv", None)
                self.on_telemetry(msg)
            else:
                future = self.pending.get(msg.get("op_id"))
                if future is not None and not future.done():
                    future.set_result(msg)
        except Exception:
            # Bad packets do not refresh telemetry age; watchdog notices staleness.
            return

    async def rpc(self, message):
        async with self.lock:
            message = dict(message, op_id=uuid.uuid4().hex, protocol_version=1)
            if "surge_norm" in message:
                message["surge_milli"] = round(message.pop("surge_norm") * 1000)
                message["yaw_millirad_s"] = round(message.pop("yaw_rate_rad_s") * 1000)
            future = asyncio.get_running_loop().create_future()
            self.pending[message["op_id"]] = future
            try:
                async with asyncio.timeout(self.settings.rpc_timeout):
                    for frame in frames(message, self.mid):
                        await self.client.write_gatt_char(self.settings.control_uuid, frame, response=True)
                    self.mid = (self.mid + 1) % 65536
                    reply = await future
                if reply.get("boot_id") != self.identity["boot_id"] or reply.get("status") != "applied":
                    raise ValueError("Device rejected operation or incorrect boot/session")
                if "control_session" in message and reply.get("control_session") != message["control_session"]:
                    raise ValueError("ACK control session mismatch")
                if "command_seq" in message and reply.get("command_seq") != message["command_seq"]:
                    raise ValueError("ACK sequence mismatch")
                return reply
            finally:
                self.pending.pop(message["op_id"], None)

    async def close(self):
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("BLE disconnected"))
        await self.client.disconnect()


class BleFactory:
    def __init__(self, settings):
        self.settings = settings
        self.devices = {}

    async def discover(self):
        devices = await BleakScanner.discover(timeout=4, service_uuids=[self.settings.service_uuid])
        self.devices = {d.address: d for d in devices}
        return [{"address": d.address, "name": d.name} for d in devices]

    def create(self, address):
        if address not in self.devices:
            raise ValueError("Run discovery first; use its address")
        return BleLink(self.devices[address], self.settings)


class MockLink:
    """Protocol test double only. Does not model flight or motors."""
    def __init__(self, drone_id):
        self.identity = {"protocol_version": 1, "drone_id": drone_id, "boot_id": uuid.uuid4().hex,
                         "profile_hash": "mock-v1", "capabilities": ["surge_yaw_rate", "expiry_watchdog"]}
        self.on_telemetry = lambda _: None
        self.on_lost = lambda: None
        self.started = time.monotonic()
        self.voltage = 4.1
        self.valid = True
        self.battery_session = uuid.uuid4().hex
        self.armed = False
        self.control_session = None
        self.last_seq = None
        self.deadline = 0
        self.seq = 0
        self.task = None
        self.fail = False
        self.history = []

    def uptime(self):
        return int((time.monotonic() - self.started) * 1000)

    def emit(self):
        self.seq += 1
        self.on_telemetry({"protocol_version": 1, "drone_id": self.identity["drone_id"],
            "boot_id": self.identity["boot_id"], "telemetry_seq": self.seq, "uptime_ms": self.uptime(),
            "armed": self.armed, "battery_session_id": self.battery_session,
            "pack_voltage_v": self.voltage, "measurement_valid": self.valid,
            "control_session": self.control_session, "last_applied_seq": self.last_seq})

    async def open(self):
        self.task = asyncio.create_task(self.run())
        self.emit()

    async def run(self):
        while True:
            if self.armed and self.uptime() >= self.deadline:
                self.armed = False
                self.control_session = None
            self.emit()
            await asyncio.sleep(.1)

    async def rpc(self, msg):
        await asyncio.sleep(0)
        if self.fail:
            raise ConnectionError("Injected device failure")
        self.history.append(dict(msg))
        if msg["type"] == "arm":
            if msg["expires_at_uptime_ms"] <= self.uptime():
                raise ValueError("Expired arm")
            self.armed, self.control_session = True, msg["control_session"]
            self.last_seq = None
            self.deadline = msg["expires_at_uptime_ms"]
        elif msg["type"] == "setpoint":
            if not self.armed or self.control_session != msg["control_session"]:
                raise ValueError("Not armed/session mismatch")
            if self.last_seq is not None and msg["command_seq"] <= self.last_seq:
                raise ValueError("Old sequence")
            if msg["expires_at_uptime_ms"] <= self.uptime():
                raise ValueError("Expired command")
            self.last_seq = msg["command_seq"]
            self.deadline = msg["expires_at_uptime_ms"]
        elif msg["type"] == "disarm":
            self.armed = False
            self.control_session = None
        self.emit()
        return {"status": "applied", "boot_id": self.identity["boot_id"],
                "uptime_ms": self.uptime(), "armed": self.armed,
                "control_session": msg.get("control_session"), "command_seq": msg.get("command_seq")}

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.on_lost()


class MockFactory:
    async def discover(self):
        return [{"address": f"mock-{i}", "name": f"Test Blimp {i}"} for i in range(1, 8)]

    def create(self, address):
        if address not in {f"mock-{i}" for i in range(1, 8)}:
            raise ValueError("Unknown test device")
        return MockLink(address)
