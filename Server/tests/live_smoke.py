"""Launch real Uvicorn on loopback, run HTTP swap demo and WebSocket handshake."""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import httpx
from websockets.sync.client import connect

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="blimp-smoke-") as directory:
    data = Path(directory)
    with (data / "server.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "tests/smoke_server.py", directory], cwd=root, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError((data / "server.log").read_text())
                try:
                    if httpx.get("http://127.0.0.1:18765/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise TimeoutError("Server did not start")
            token = (data / "api-token.txt").read_text()
            with connect("ws://127.0.0.1:18765/v1/events", additional_headers={"Authorization": "Bearer " + token}) as ws:
                assert json.loads(ws.recv())["type"] == "snapshot"
            result = subprocess.run([sys.executable, "demo_swap.py", "--port", "18765", "--token-file", str(data / "api-token.txt")],
                                    cwd=root, capture_output=True, text=True, timeout=25)
            print(result.stdout)
            if result.returncode:
                raise RuntimeError(result.stderr)
            print("PASS: live HTTP server, authenticated WebSocket and swap demo.")
        finally:
            (data / "stop").touch()
            process.wait(timeout=10)
