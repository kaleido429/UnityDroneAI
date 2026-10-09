import argparse
import json
import os
import secrets
from pathlib import Path
import uvicorn
from .app import create_app
from .models import Settings


def main():
    parser = argparse.ArgumentParser(description="Unity/ESP32 Blimp BLE bridge")
    parser.add_argument("--mock", action="store_true", help="Protocol test doubles; never connects Bluetooth")
    parser.add_argument("--config", type=Path, help="Implementation settings JSON (not the earlier design example)")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    settings = Settings.model_validate(json.loads(args.config.read_text("utf-8"))) if args.config else Settings()
    data = args.data_dir or Path(__file__).resolve().parents[1] / "data" / ("mock" if args.mock else "ble")
    data.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("BLIMP_API_TOKEN") or secrets.token_urlsafe(32)
    (data / "api-token.txt").write_text(token, encoding="utf-8")
    print(f"Mode: {'MOCK (no radio)' if args.mock else 'BLE'}; token file: {data / 'api-token.txt'}")
    print("API: http://127.0.0.1:%d/docs ; one worker only" % args.port)
    uvicorn.run(create_app(settings, data, token, args.mock), host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
