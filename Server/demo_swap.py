"""Run against a fresh --mock server. Tests protocol only, never connects a drone."""
import argparse
import asyncio
from pathlib import Path
import httpx


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", type=Path, default=Path(__file__).parent / "data/mock/api-token.txt")
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{args.port}", headers={"Authorization": "Bearer " + token}) as client:
        async def call(method, path, body=None, **kwargs):
            response = await client.request(method, path, json=body, **kwargs)
            response.raise_for_status()
            return response.json()
        if (await call("GET", "/health"))["mode"] != "mock":
            raise RuntimeError("Demo is allowed only on mock servers")
        snapshot = await call("GET", "/v1/fleet")
        if any(s["drone_id"] for s in snapshot["slots"].values()):
            raise RuntimeError("Use a fresh mock server (restart first)")
        for i in range(1, 8):
            if f"mock-{i}" not in snapshot["drones"]:
                await call("POST", "/v1/drones/enroll", {
                    "drone_id": f"mock-{i}", "name": f"Test Blimp {i}",
                    "battery": {"validated": True, "warning_voltage": 3.7, "replace_voltage": 3.5,
                                "critical_voltage": 3.2, "dwell_seconds": 0},
                    "vehicle": {"validated": True, "profile_hash": "mock-v1", "surge_max": .4,
                                "yaw_rate_max": .5, "expiry_behavior": "neutral_disarm"}})
        for i in range(1, 5):
            await call("POST", f"/v1/drones/mock-{i}/connect", {"address": f"mock-{i}"})
            await call("POST", f"/v1/slots/slot-{i}/assign", {"drone_id": f"mock-{i}",
                "expected_binding_epoch": snapshot["slots"][f"slot-{i}"]["binding_epoch"]})
        snapshot = await call("GET", "/v1/fleet")
        actions = {}
        for slot, info in snapshot["slots"].items():
            bind = {"expected_binding_epoch": info["binding_epoch"]}
            await call("POST", f"/v1/slots/{slot}/arm", bind)
            actions[slot] = (await call("POST", f"/v1/slots/{slot}/actions", dict(bind, duration_ms=10000)))["action_id"]
        operation_ids = []
        async def pump():
            for seq in range(30):
                for slot in ("slot-1", "slot-3", "slot-4"):
                    op = await call("POST", f"/v1/slots/{slot}/commands", {
                        "server_epoch": snapshot["server_epoch"],
                        "expected_binding_epoch": snapshot["slots"][slot]["binding_epoch"],
                        "action_id": actions[slot], "command_seq": seq, "surge_norm": .1, "yaw_rate_rad_s": .1})
                    operation_ids.append(op["operation_id"])
                await asyncio.sleep(.12)
        task = asyncio.create_task(pump())
        try:
            await call("POST", "/v1/mock/mock-2/battery", {"pack_voltage_v": 3.6})
            await asyncio.sleep(.35)
            before = await call("GET", "/v1/fleet")
            swap = await call("POST", "/v1/slots/slot-2/swaps",
                {"expected_binding_epoch": snapshot["slots"]["slot-2"]["binding_epoch"]},
                headers={"Idempotency-Key": "demo-swap"})
            await call("POST", f"/v1/swaps/{swap['swap_id']}/confirm-recovered", {"recovered": True})
            await call("POST", "/v1/drones/mock-5/connect", {"address": "mock-5"})
            await call("POST", f"/v1/swaps/{swap['swap_id']}/replacement", {"drone_id": "mock-5"})
            await task
            await asyncio.sleep(.15)
            after = await call("GET", "/v1/fleet")
            for i in (1, 3, 4):
                assert before["drones"][f"mock-{i}"]["connection_id"] == after["drones"][f"mock-{i}"]["connection_id"]
            assert after["slots"]["slot-2"]["drone_id"] == "mock-5"
            assert after["slots"]["slot-2"]["state"] == "READY_DISARMED"
            results = [await call("GET", f"/v1/operations/{op}") for op in operation_ids]
            assert all(r["status"] == "applied" for r in results)
            print(f"PASS: slot-2 replaced mock-2 with mock-5; {len(results)} commands applied on other three; their connection IDs unchanged.")
            events = await call("GET", "/v1/events/recent")
            assert any(e["type"] == "battery.warning" for e in events)
            print("PASS: battery warning event delivered; replacement remains disarmed.")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Leave all test devices disarmed when the demo exits.
            current = await call("GET", "/v1/fleet")
            for slot, info in current["slots"].items():
                if info["drone_id"]:
                    await call("POST", f"/v1/slots/{slot}/disarm", {"expected_binding_epoch": info["binding_epoch"]})


if __name__ == "__main__":
    asyncio.run(main())
