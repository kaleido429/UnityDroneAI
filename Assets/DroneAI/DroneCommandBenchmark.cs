using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using LLMUnity;
using Newtonsoft.Json.Linq;
using UnityEngine;
using Debug = UnityEngine.Debug;

// Measures inference only. No networking, actuators, physics, or flight simulation.
public sealed class DroneCommandBenchmark : MonoBehaviour
{
    public LLMAgent[] agents;
    public float secondsPerMode = 60;
    public string outputDirectory = "BenchmarkResults";
    readonly List<JObject> samples = new List<JObject>();
    readonly Stopwatch clock = new Stopwatch();
    string runDirectory;
    public static bool Finished;
    public static bool Failed;
    const string SystemPrompt = "You are a game character issuing high-level velocity requests for ONE airship. " +
        "This is an offline benchmark. Output only JSON with vx,vy,vz,yaw. " +
        "Each value must be -0.2, 0, or 0.2. x is right, y is up, z is forward. " +
        "Move toward the supplied target on each axis; yaw must be 0. No explanation.";
    public static string Prompt => SystemPrompt;
    public static string Grammar => "root ::= \"{\\\"vx\\\":\" val \",\\\"vy\\\":\" val \",\\\"vz\\\":\" val \",\\\"yaw\\\":\" val \"}\"\nval ::= \"-0.2\" | \"0\" | \"0.2\"";

    async void Start()
    {
        Finished = false;
        Failed = false;
        runDirectory = Path.GetFullPath(Path.Combine(outputDirectory, DateTime.Now.ToString("yyyyMMdd-HHmmss")));
        Directory.CreateDirectory(runDirectory);
        try
        {
            var load = Stopwatch.StartNew();
            await Timed(agents[0].llm.WaitUntilReady(), 180000);
            for (int i = 0; i < 4; i++) await Timed(agents[i].Chat(Query(i, 0), addToHistory: false), 120000);
            var report = new JObject {
                ["model"] = agents[0].llm.model, ["unity"] = Application.unityVersion,
                ["cpu"] = SystemInfo.processorType, ["ramMB"] = SystemInfo.systemMemorySize,
                ["gpu"] = SystemInfo.graphicsDeviceName, ["gpuLayers"] = agents[0].llm.numGPULayers,
                ["threads"] = agents[0].llm.numThreads, ["slots"] = agents[0].llm.parallelPrompts,
                ["contextTotal"] = agents[0].llm.contextSize, ["maxOutputTokens"] = agents[0].numPredict,
                ["loadAndWarmupSeconds"] = load.Elapsed.TotalSeconds, ["secondsRequestedPerMode"] = secondsPerMode,
                ["method"] = "Completed, validated JSON commands; varying synthetic telemetry; no history growth; cached system prefix; no transport or game rendering load. Per-drone Hz = valid count / full measured wall time including drain. Gaps include mode start and trailing silence.",
                ["systemPrompt"] = SystemPrompt, ["grammar"] = Grammar
            };
            var modes = new JArray();
            foreach (string mode in new[] { "round_robin_4", "parallel_4" })
            {
                Debug.Log("DRONE_BENCH_START " + mode);
                samples.Clear(); clock.Restart();
                if (mode == "parallel_4") await Task.WhenAll(Enumerable.Range(0, 4).Select(i => Worker(i, mode)));
                else { int n = 0; while (clock.Elapsed.TotalSeconds < secondsPerMode) { await Request(n % 4, n / 4, mode); n++; } }
                clock.Stop();
                double elapsed = clock.Elapsed.TotalSeconds;
                var drones = new JArray();
                for (int i = 0; i < 4; i++)
                {
                    var rows = samples.Where(s => (int)s["drone"] == i).ToArray();
                    var valid = rows.Where(s => (bool)s["valid"]).ToArray();
                    var gaps = new List<double>(); double last = 0;
                    foreach (var row in valid) { double end = (double)row["endSeconds"]; gaps.Add(end - last); last = end; }
                    gaps.Add(elapsed - last);
                    double[] latency = rows.Select(s => (double)s["latencySeconds"]).ToArray();
                    drones.Add(new JObject { ["drone"] = i, ["requests"] = rows.Length, ["valid"] = valid.Length,
                        ["directionCorrect"] = valid.Count(s => (bool)s["directionCorrect"]),
                        ["commandsPerSecond"] = valid.Length / elapsed, ["latencyP50"] = Percentile(latency, .5),
                        ["latencyP95"] = Percentile(latency, .95), ["latencyMax"] = latency.DefaultIfEmpty(0).Max(),
                        ["gapP95"] = Percentile(gaps.ToArray(), .95), ["gapMax"] = gaps.Max() });
                }
                var summary = new JObject { ["mode"] = mode, ["wallSeconds"] = elapsed, ["drones"] = drones };
                modes.Add(summary);
                File.WriteAllText(Path.Combine(runDirectory, mode + "-samples.json"), new JArray(samples).ToString());
                report["modes"] = modes;
                File.WriteAllText(Path.Combine(runDirectory, "summary.json"), report.ToString());
                Debug.Log("DRONE_BENCH_RESULT " + summary.ToString(Newtonsoft.Json.Formatting.None));
            }
            Debug.Log("DRONE_BENCH_DONE " + runDirectory);
        }
        catch (Exception ex) { Failed = true; File.WriteAllText(Path.Combine(runDirectory, "error.txt"), ex.ToString()); Debug.LogException(ex); }
        finally { Finished = true; }
    }
    static async Task Timed(Task task, int ms)
    { if (await Task.WhenAny(task, Task.Delay(ms)) != task) throw new TimeoutException("LLM request timed out; aborting benchmark."); await task; }
    async Task Worker(int drone, string mode)
    { int n = 0; while (clock.Elapsed.TotalSeconds < secondsPerMode) await Request(drone, n++, mode); }
    static int[] Target(int drone, int sequence) => new[] { (sequence + drone) % 3 - 1, (sequence * 2 + drone) % 3 - 1, (sequence + drone * 2 + 1) % 3 - 1 };
    static string Query(int drone, int sequence)
    { var t = Target(drone, sequence); return $"Airship {drone + 1}; telemetry sequence {sequence}; position [0,0,0]; velocity [0,0,0]; target [{t[0]},{t[1]},{t[2]}]. Return the next velocity request."; }
    async Task Request(int drone, int sequence, string mode)
    {
        double start = clock.Elapsed.TotalSeconds;
        var task = agents[drone].Chat(Query(drone, sequence), addToHistory: false);
        await Timed(task, 120000);
        string response = await task;
        double end = clock.Elapsed.TotalSeconds;
        bool valid = false, correct = false;
        try
        {
            var obj = JObject.Parse(response); string[] keys = { "vx", "vy", "vz", "yaw" };
            valid = obj.Count == 4 && keys.All(k => obj[k] != null && (obj[k].Type == JTokenType.Integer || obj[k].Type == JTokenType.Float) && new[] { -.2, 0, .2 }.Any(v => Math.Abs((double)obj[k] - v) < .00001));
            var target = Target(drone, sequence);
            correct = valid && Enumerable.Range(0, 3).All(i => Math.Abs((double)obj[keys[i]] - target[i] * .2) < .00001) && (double)obj["yaw"] == 0;
        }
        catch { }
        samples.Add(new JObject { ["mode"] = mode, ["drone"] = drone, ["sequence"] = sequence, ["startSeconds"] = start,
            ["endSeconds"] = end, ["latencySeconds"] = end - start, ["valid"] = valid, ["directionCorrect"] = correct, ["response"] = response });
    }
    static double Percentile(double[] values, double p)
    { if (values.Length == 0) return 0; Array.Sort(values); return values[Math.Min(values.Length - 1, (int)Math.Ceiling(values.Length * p) - 1)]; }
}
