# Offline LLM drone-command benchmark

This scene measures LLM for Unity 3.0.3 running Qwen2.5-0.5B-Instruct Q4_K_M on the local CPU. It has no drone connection, flight simulation, drone model, or actuator output.

Open `Assets/DroneAI/Scenes/CommandBenchmark.unity` and press Play. To recreate it, choose **Tools > Drone AI > Create Benchmark Scene**. The scene uses one LLM with four LLMAgent clients, four slots, CPU 8 threads, 4096 total context tokens, temperature 0, at most 64 output tokens, a constrained JSON grammar, and no growing conversation history. Each measured mode runs for at least 60 seconds plus completion of outstanding requests, after model load and four warmup requests.

Two workloads are measured: round-robin across four drones, and four independent concurrent request loops. Inputs contain changing synthetic position/target telemetry; no physics is simulated. Outputs contain vx, vy, vz and yaw, each restricted to -0.2, 0, or 0.2. This is a deliberately short command workload, not general character dialogue or natural-language mission planning.

Timestamped `BenchmarkResults` folders contain `summary.json` and every measured request/response. Commands per second for each drone are the count of valid completed JSON commands divided by the full wall-clock duration, including drain. Invalid replies do not count. Direction correctness is a separate simple task check. It is not flight-safety certification. Gap statistics use valid completion times and include the initial wait and final silence; p95 uses nearest rank. Warmup and loading are reported separately. No radio/network delay, rendering load, or real sensor processing is included.

The benchmark does not establish a safe flight-controller update rate. A vehicle-specific deterministic controller and stale-command handling must maintain the airship independently of LLM response timing. Do not connect these synthetic commands directly to motors.

Sources: https://github.com/undreamai/LLMUnity and https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF . The LlamaLib 2.0.5 archive was verified against its published SHA-256 before extraction. Model upstream SHA-256 verification was not completed.

To rerun without rendering (close the project in other Unity instances first):

```powershell
& 'C:\Program Files\Unity\Hub\Editor\6000.0.56f1\Editor\Unity.exe' -batchmode -nographics -projectPath 'F:\Unity\UnityDroneAI' -executeMethod DroneBenchmarkEditor.RunBatch -logFile 'F:\Unity\UnityDroneAI\BenchmarkResults-unity.log'
```

Do not add `-quit`: the editor runner exits when asynchronous measurement completes. The menu creates/saves a dedicated scene, without changing build settings. Model and native binaries are in StreamingAssets on F:. The downloaded multi-platform library archive is retained in BenchmarkDownloads for recovery.
