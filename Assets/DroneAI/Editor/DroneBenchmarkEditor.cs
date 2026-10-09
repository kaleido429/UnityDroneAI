using System;
using System.IO;
using LLMUnity;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

[InitializeOnLoad]
public static class DroneBenchmarkEditor
{
    const string AutoKey = "DroneBenchmark.Batch";
    static DroneBenchmarkEditor() { EditorApplication.update += Tick; }
    [MenuItem("Tools/Drone AI/Create Benchmark Scene")]
    public static void CreateScene()
    {
        if (!Application.isBatchMode && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;
        EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
        var root = new GameObject("Offline Drone Command Benchmark"); root.SetActive(false);
        var llm = root.AddComponent<LLM>();
        llm.model = "Models/qwen2.5-0.5b-instruct-q4_k_m.gguf";
        llm.numThreads = 8; llm.numGPULayers = 0; llm.parallelPrompts = 4; llm.contextSize = 4096;
        llm.dontDestroyOnLoad = false;
        var runner = root.AddComponent<DroneCommandBenchmark>(); runner.agents = new LLMAgent[4];
        for (int i = 0; i < 4; i++)
        {
            var child = new GameObject("Airship " + (i + 1)); child.transform.SetParent(root.transform);
            var agent = child.AddComponent<LLMAgent>(); agent.llm = llm; agent.slot = i;
            agent.systemPrompt = DroneCommandBenchmark.Prompt; agent.grammar = DroneCommandBenchmark.Grammar;
            agent.temperature = 0; agent.numPredict = 64; agent.cachePrompt = true; runner.agents[i] = agent;
        }
        root.SetActive(true);
        Directory.CreateDirectory("Assets/DroneAI/Scenes");
        EditorSceneManager.SaveScene(root.scene, "Assets/DroneAI/Scenes/CommandBenchmark.unity");
    }
    public static void RunBatch()
    {
        CreateScene(); SessionState.SetBool(AutoKey, true);
        SessionState.SetString("DroneBenchmark.Start", DateTime.UtcNow.ToString("O"));
        EditorApplication.isPlaying = true;
    }
    static void Tick()
    {
        if (!SessionState.GetBool(AutoKey, false)) return;
        if (DroneCommandBenchmark.Finished)
        { SessionState.SetBool(AutoKey, false); EditorApplication.Exit(DroneCommandBenchmark.Failed ? 1 : 0); }
        else if (DateTime.TryParse(SessionState.GetString("DroneBenchmark.Start", ""), out var start) && (DateTime.UtcNow - start.ToUniversalTime()).TotalMinutes > 10)
        { Debug.LogError("DRONE_BENCH_TIMEOUT"); SessionState.SetBool(AutoKey, false); EditorApplication.Exit(2); }
    }
}
