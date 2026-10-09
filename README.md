# UnityDroneAI

Unity 6000.0.56f1 project with LLM for Unity 3.0.3 command-generation benchmarks and a Python BLE bridge for up to four ESP32 blimps.

- [Python server setup and test demo](Server/RUNNING.md)
- [ESP32 BLE firmware contract](Server/FIRMWARE_CONTRACT.md)
- [Architecture](Server/README.md)
- [Measured Qwen CPU benchmark results](BenchmarkResults/RESULTS.ko.md)

The server supports interchangeable device IDs, four logical roles, battery warnings, isolated connections and replacement workflows. Its mock mode tests communications only; it does not simulate flight. ESP32 firmware and Unity network integration are not included yet. The current API listens on localhost; running Unity and Python on separate computers still requires a configurable network binding and deployment setup.

## Restore dependencies

Open the project in Unity 6000.0.56f1 and let Package Manager restore packages. LLMUnity downloads its LlamaLib runtime automatically. Runtime binaries, download caches, Python virtual environments, tokens and model weights are deliberately not committed.

For the saved benchmark scene, download [Qwen2.5-0.5B-Instruct Q4_K_M](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/blob/main/qwen2.5-0.5b-instruct-q4_k_m.gguf) into `Assets/StreamingAssets/Models/` with the original filename, then open `Assets/DroneAI/Scenes/CommandBenchmark.unity`. The model download is approximately 491 MB. LLMUnity runtime downloads require additional disk space.

Python setup, from `Server`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m blimp_bridge --mock
```

Real hardware arming is disabled by default. Battery thresholds and vehicle control limits must be configured against the actual hardware. No actual BLE flight test has been performed.

Third-party LLMUnity source retains its bundled license and notices under `Assets/LLMUnity`.
