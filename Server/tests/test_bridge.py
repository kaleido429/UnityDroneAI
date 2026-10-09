import asyncio
import time
import pytest
from fastapi.testclient import TestClient
from blimp_bridge.app import create_app
from blimp_bridge.models import Settings, Enroll, Assign, Action, Command, Telemetry, BatteryProfile
from blimp_bridge.fleet import Fleet, Conflict
from blimp_bridge.storage import Storage
from blimp_bridge.transport import MockFactory
from blimp_bridge.battery import BatteryMonitor
from blimp_bridge.codec import frames, Reassembler


def enrollment(i):
    return Enroll(drone_id=f"mock-{i}", name=f"Blimp {i}",
        battery=BatteryProfile(validated=True, warning_voltage=3.7, replace_voltage=3.5,
                               critical_voltage=3.2, dwell_seconds=0),
        vehicle={"validated": True, "profile_hash": "mock-v1", "surge_max": .4,
                 "yaw_rate_max": .5, "expiry_behavior": "neutral_disarm"})


async def make_fleet(tmp_path, count=4):
    f = Fleet(Settings(command_ttl_ms=1000), MockFactory(), Storage(tmp_path / "test.db"), mock=True)
    await f.start()
    for i in range(1, 8):
        await f.enroll(enrollment(i))
    for i in range(1, count + 1):
        await f.connect(f"mock-{i}", f"mock-{i}")
        await f.assign(f"slot-{i}", Assign(drone_id=f"mock-{i}", expected_binding_epoch=f.slots[f"slot-{i}"].epoch))
    return f


def command(f, slot, action, seq):
    return Command(server_epoch=f.server_epoch, expected_binding_epoch=f.slots[slot].epoch,
                   action_id=action, command_seq=seq, surge_norm=.1, yaw_rate_rad_s=.1)


def test_swap_keeps_other_three_running(tmp_path):
    async def scenario():
        f = await make_fleet(tmp_path)
        pump = None
        try:
            identities = {k: d.connection_id for k, d in f.devices.items()}
            actions = {}
            for key, s in f.slots.items():
                await f.arm(key, s.epoch)
                actions[key] = f.action(key, Action(expected_binding_epoch=s.epoch, duration_ms=10000))["action_id"]
            sessions = {k: d.session for k, d in f.devices.items()}
            async def send_others():
                n = 0
                while True:
                    for key in ("slot-1", "slot-3", "slot-4"):
                        f.command(key, command(f, key, actions[key], n))
                    n += 1
                    await asyncio.sleep(.12)
            pump = asyncio.create_task(send_others())
            old_command = command(f, "slot-2", actions["slot-2"], 0)
            f.devices["mock-2"].link.voltage = 3.6
            for _ in range(20):
                f.devices["mock-2"].link.emit()
            assert f.devices["mock-2"].battery_state == "WARNING"
            swap = await f.begin_swap("slot-2", f.slots["slot-2"].epoch, "swap-test")
            await asyncio.sleep(.25)
            await f.recover(swap["swap_id"], False)
            await f.connect("mock-5", "mock-5")
            await f.replacement(swap["swap_id"], "mock-5")
            await asyncio.sleep(.25)
            assert not f.devices["mock-5"].telemetry.armed
            assert f.slots["slot-2"].state == "READY_DISARMED"
            with pytest.raises(Conflict):
                f.command("slot-2", old_command)
            for key in ("mock-1", "mock-3", "mock-4"):
                assert f.devices[key].connection_id == identities[key]
                assert f.devices[key].session == sessions[key]
                assert sum(m["type"] == "setpoint" for m in f.devices[key].link.history) >= 3
            assert any(e["type"] == "battery.warning" for e in f.events)
        finally:
            if pump:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            await f.close()
    asyncio.run(scenario())


def test_connection_limit_duplicate_assignment_and_idempotency(tmp_path):
    async def scenario():
        f = await make_fleet(tmp_path)
        try:
            with pytest.raises(Conflict):
                await f.connect("mock-5", "mock-5")
            swap = await f.begin_swap("slot-2", f.slots["slot-2"].epoch, "same")
            again = await f.begin_swap("slot-2", f.slots["slot-2"].epoch, "same")
            assert swap["swap_id"] == again["swap_id"]
            with pytest.raises(Conflict):
                await f.begin_swap("slot-3", f.slots["slot-3"].epoch, "same")
            with pytest.raises(Conflict):
                await f.assign("slot-1", Assign(drone_id="mock-2", expected_binding_epoch=f.slots["slot-1"].epoch))
        finally:
            await f.close()
    asyncio.run(scenario())


def test_lost_device_does_not_cancel_other_sender(tmp_path):
    async def scenario():
        f = await make_fleet(tmp_path, 2)
        try:
            for key in ("slot-1", "slot-2"):
                await f.arm(key, f.slots[key].epoch)
            a = f.action("slot-2", Action(expected_binding_epoch=f.slots["slot-2"].epoch, duration_ms=2000))
            f.devices["mock-1"].link.fail = True
            await f.devices["mock-1"].link.close()
            swap = await f.begin_swap("slot-1", f.slots["slot-1"].epoch, "lost")
            with pytest.raises(Conflict):
                await f.recover(swap["swap_id"], False)
            op = f.command("slot-2", command(f, "slot-2", a["action_id"], 0))
            await asyncio.sleep(.2)
            assert f.operations[op["operation_id"]]["status"] == "applied"
            await f.recover(swap["swap_id"], True)
            assert f.devices["mock-2"].state == "CONNECTED"
        finally:
            await f.close()
    asyncio.run(scenario())


def test_stale_epoch_duplicate_latest_only_and_expiry(tmp_path):
    async def scenario():
        f = await make_fleet(tmp_path, 1)
        try:
            s = f.slots["slot-1"]
            await f.arm("slot-1", s.epoch)
            a = f.action("slot-1", Action(expected_binding_epoch=s.epoch, duration_ms=300))
            c = command(f, "slot-1", a["action_id"], 0)
            old = f.command("slot-1", c)
            with pytest.raises(Conflict):
                f.command("slot-1", c)
            new = f.command("slot-1", command(f, "slot-1", a["action_id"], 1))
            assert f.operations[old["operation_id"]]["reason"] == "superseded"
            bad = command(f, "slot-1", a["action_id"], 2).model_copy(update={"server_epoch": "old"})
            with pytest.raises(Conflict):
                f.command("slot-1", bad)
            await asyncio.sleep(.2)
            assert f.operations[new["operation_id"]]["status"] == "applied"
            await asyncio.sleep(.3)
            assert not f.devices["mock-1"].link.armed
            with pytest.raises(Conflict):
                f.command("slot-1", command(f, "slot-1", a["action_id"], 3))
        finally:
            await f.close()
    asyncio.run(scenario())


def test_restart_preserves_registry_not_assignments(tmp_path):
    async def scenario():
        f = await make_fleet(tmp_path, 1)
        epoch = f.server_epoch
        await f.close()
        other = Fleet(Settings(), MockFactory(), Storage(tmp_path / "test.db"), True)
        await other.start()
        try:
            assert len(other.registry) == 7
            assert other.server_epoch != epoch
            assert not other.devices
            assert all(s.state == "EMPTY" for s in other.slots.values())
        finally:
            await other.close()
    asyncio.run(scenario())


def telemetry(v, valid=True, battery="pack-1"):
    return Telemetry(drone_id="a", boot_id="b", telemetry_seq=1, uptime_ms=1, armed=False,
                     battery_session_id=battery, pack_voltage_v=v, measurement_valid=valid)


def test_battery_unknown_dwell_hysteresis_and_critical_latch():
    p = BatteryProfile(validated=True, warning_voltage=3.7, replace_voltage=3.5,
                       critical_voltage=3.2, dwell_seconds=2, hysteresis_v=.1)
    b = BatteryMonitor(p)
    assert b.update(telemetry(None, False), 0) == "UNKNOWN"
    assert b.update(telemetry(3.65), 1) == "UNKNOWN"
    assert b.update(telemetry(3.65), 3) == "WARNING"
    for n in range(4, 20):
        assert b.update(telemetry(3.75), n) == "WARNING"
    assert b.update(telemetry(3.1), 20) == "CRITICAL"
    assert b.update(telemetry(4.1), 21) == "CRITICAL"
    assert b.update(telemetry(4.1, battery="pack-2"), 22) == "UNKNOWN"
    assert b.update(telemetry(4.1, battery="pack-2"), 24) == "NORMAL"


def test_unknown_profile_blocks_arm(tmp_path):
    async def scenario():
        f = await make_fleet(tmp_path, 0)
        try:
            f.registry["mock-1"].battery.validated = False
            await f.connect("mock-1", "mock-1")
            with pytest.raises(Conflict):
                await f.assign("slot-1", Assign(drone_id="mock-1", expected_binding_epoch=f.slots["slot-1"].epoch))
        finally:
            await f.close()
    asyncio.run(scenario())


def test_codec_fragmentation_order_duplicates_and_limits():
    msg = {"type": "setpoint", "payload": "x" * 100}
    parts = frames(msg, 10)
    assert all(len(p) <= 20 for p in parts)
    r = Reassembler()
    assert r.feed(parts[0]) is None
    assert r.feed(parts[0]) is None
    result = None
    for p in reversed(parts[1:]):
        result = r.feed(p)
    assert result == msg
    assert r.feed(parts[0]) is None
    with pytest.raises(ValueError):
        frames({"x": "x" * 1024}, 11)
    with pytest.raises(ValueError):
        Reassembler().feed(b"bad")


def test_api_auth_validation_websocket_and_hardware_default(tmp_path):
    app = create_app(Settings(), tmp_path, "test-token", mock=True)
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/fleet").status_code == 401
        snapshot = client.get("/v1/fleet", headers=headers).json()
        assert len(snapshot["slots"]) == 4
        assert snapshot["hardware_arming_enabled"] is False
        assert client.post("/v1/drones/enroll", headers=headers, json=enrollment(1).model_dump()).status_code == 201
        assert client.post("/v1/drones/mock-1/connect", headers=headers, json={"address": "mock-1"}).status_code == 200
        with client.websocket_connect("/v1/events", headers=headers) as ws:
            assert ws.receive_json()["type"] == "snapshot"
        bad = client.post("/v1/slots/slot-1/commands", headers=headers, json={"surge_norm": 2})
        assert bad.status_code == 422
