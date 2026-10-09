import asyncio
import pytest
from blimp_bridge.transport import BleLink
from blimp_bridge.models import Settings
from blimp_bridge.codec import Reassembler, frames


class FakeGatt:
    def __init__(self, link, bad_ack=False):
        self.link = link
        self.reassembler = Reassembler()
        self.bad_ack = bad_ack
        self.received = None

    async def write_gatt_char(self, char, data, response):
        assert response is True
        assert len(data) <= 20
        msg = self.reassembler.feed(data)
        if msg:
            self.received = msg
            reply = {"op_id": msg["op_id"], "status": "applied", "boot_id": "boot",
                     "control_session": "wrong" if self.bad_ack else msg["control_session"],
                     "command_seq": msg["command_seq"]}
            for fragment in frames(reply, 100):
                self.link.receive("ack", fragment)


@pytest.mark.parametrize("bad_ack", [False, True])
def test_actual_ble_rpc_fragmentation_and_ack_fencing(bad_ack):
    async def scenario():
        link = BleLink("00:00:00:00:00:01", Settings())
        link.identity = {"boot_id": "boot"}
        link.client = FakeGatt(link, bad_ack)
        message = {"type": "setpoint", "control_session": "session", "command_seq": 7,
                   "surge_norm": .15, "yaw_rate_rad_s": -.1, "expires_at_uptime_ms": 2000}
        if bad_ack:
            with pytest.raises(ValueError, match="session mismatch"):
                await link.rpc(message)
        else:
            assert (await link.rpc(message))["command_seq"] == 7
            assert link.client.received["surge_milli"] == 150
            assert link.client.received["yaw_millirad_s"] == -100
        assert not link.pending
    asyncio.run(scenario())
