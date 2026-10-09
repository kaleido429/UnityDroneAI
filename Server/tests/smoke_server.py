import asyncio
import secrets
import sys
from pathlib import Path
import uvicorn
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blimp_bridge.app import create_app
from blimp_bridge.models import Settings


async def main():
    data = Path(sys.argv[1])
    token = secrets.token_urlsafe(32)
    (data / "api-token.txt").write_text(token)
    app = create_app(Settings(), data, token, mock=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18765, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        while not (data / "stop").exists() and not task.done():
            await asyncio.sleep(.1)
    finally:
        server.should_exit = True
        await task


asyncio.run(main())
