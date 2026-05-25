---
name: unity-development
description: >
  Unity 6 LTS master skill — high-performance, cross-platform game development.
  Smart router: loads specialized sub-skills for ECS, rendering, audio, input, etc.
  Covers MCP Editor integration, C# scripting, scene management, prefab creation,
  and editor automation. Use for any Unity task; this skill routes to the right
  sub-skill and provides always-on best practices.
allowed-tools: Read, Grep, Write, Bash, Edit, Glob, WebFetch
---

# Unity Development — Unity 6 LTS Master Skill

## Sub-Skill Routing

Load the appropriate sub-skill **before** working on these areas. Each sub-skill
has concrete API patterns, code examples, and package-specific guidance.

| Task area | Sub-skill to load |
|-----------|-------------------|
| DOTS / ECS / Jobs / Burst | `unity-ecs` (workspace) + `unity-ecs-patterns` (global, has playbook) |
| URP — shaders, render features, post-processing | `unity-urp` |
| HDRP — ray tracing, volumetrics, high-fidelity | `unity-hdrp` |
| Shader Graph / custom HLSL shaders | `unity-shader-graph` |
| VFX Graph / GPU particles | `unity-vfx-graph` |
| Animator / blend trees / Timeline / Animation Rigging | `unity-animation` |
| Cinemachine — virtual cameras, dolly tracks | `unity-cinemachine` |
| New Input System — action maps, rebinding, local multiplayer | `unity-input-system` |
| Netcode for GameObjects / multiplayer | `unity-netcode` |
| Physics — rigid bodies, joints, raycasting | `unity-physics` |
| Addressables — remote catalogs, content updates | `unity-addressables` |
| UI Toolkit — UXML, USS, runtime UI | `unity-ui-toolkit` |
| Profiler / Frame Debugger / Memory Profiler | `unity-profiler` |
| Unity MCP Editor automation (MCP for Unity tools) | `unity-mcp-orchestrator` (global) |

---

## Behavioral Traits

- Prioritize **performance** from project start; profile on target hardware early
- Implement **scalable architecture** (MVC, state machines, service locator) for team projects
- Write clean, maintainable **C# 9+ code** with proper error handling and `[SerializeField]`
- Consider **target platform limitations** (mobile thermal, console TCR, WebGL threading) in every design decision
- Use **Unity Profiler** proactively — never assume where the bottleneck is
- Follow Unity coding conventions and naming standards
- Test on **all target platforms**, not just the editor
- Keep current with the **Unity 6 LTS roadmap** and package updates

---

## Unity 6 LTS — Key Capabilities

### Rendering
- Universal Render Pipeline (URP) and High Definition Render Pipeline (HDRP)
- Custom render features and renderer passes
- Shader Graph, HLSL shaders, compute shaders
- Real-time ray tracing and path tracing (HDRP)
- VFX Graph for GPU-accelerated particle effects
- HDR, tone mapping, post-processing stack

### Performance
- Job System + Burst Compiler for CPU-parallel work
- Data-Oriented Technology Stack (DOTS) / ECS for thousands of entities
- Async/await with UniTask (or careful Unity-context handling)
- LOD, occlusion culling, texture streaming
- Platform-specific profiling: mobile GPU thermal, console memory budgets

### Architecture
- ECS for data-oriented, large-scale systems
- ScriptableObjects for data-driven configuration (shared across scenes/prefabs)
- Addressable Assets for dynamic content loading and remote delivery
- Assembly Definitions (`.asmdef`) for fast incremental compilation
- Dependency injection via service locators or lightweight DI containers

### Multiplayer
- Unity Netcode for GameObjects (server-authoritative)
- Relay and lobby services
- State sync, lag compensation, bandwidth optimization

### Platform Targets
- **Mobile**: texture compression, draw call batching, thermal limits, IL2CPP
- **Console**: PlayStation / Xbox / Switch certification (TCR/TRC/LOT), memory budgets
- **PC**: Steam integration, Windows-specific optimizations
- **WebGL**: no threading, limited memory, shader restrictions — test early
- **VR/AR**: XR Toolkit, per-eye rendering budget, foveated rendering

---

## Always-On Best Practices

These apply to **every** Unity C# script. Check against them before submitting.

### Lifecycle
- `Awake` for self-init; `Start` for cross-component references
- Script execution order between components is not guaranteed — use Script Execution Order settings if needed
- `Awake` runs even when disabled; `Start` only runs when enabled

### GetComponent
- Never call `GetComponent` in `Update` — cache in `Awake` or `Start`
- Prefer `TryGetComponent` for null-safe lookups
- `GetComponentInChildren` is expensive on deep hierarchies — cache it

### Physics
- Physics logic in `FixedUpdate`, not `Update`
- Use `Rigidbody.MovePosition`/`MoveRotation` — `transform.position` bypasses physics
- Use `Time.fixedDeltaTime` in `FixedUpdate`, `Time.deltaTime` in `Update`
- Non-alloc physics queries: `RaycastNonAlloc`, `OverlapSphereNonAlloc`

### Unity's Fake Null
- Destroyed objects pass `!= null` — they're not truly null
- Null-conditional `?.` operator does **not** work correctly on Unity objects
- `Destroy` is deferred; object persists until end of frame

### Coroutines
- `StartCoroutine` requires an active, enabled MonoBehaviour — disabled/destroyed = stopped
- `StopCoroutine` needs the same IEnumerator reference or stored `Coroutine` — string overload is unreliable

### Object Pooling
- Never `Instantiate`/`Destroy` in tight loops — pool instead
- `SetActive(false)` to return to pool; parent inactive objects under a dedicated pool root

### Serialization
- Prefer `[SerializeField] private` over `public` for inspector-exposed fields
- `[HideInInspector]` hides but still serializes; `[System.NonSerialized]` skips serialization entirely
- Inspector values override code defaults after first serialization

### Common Pitfalls
- `Find` methods every frame — always cache
- `tag == "Enemy"` string comparison — use `CompareTag("Enemy")`
- `async/await` without UnityContext — use UniTask or handle exceptions explicitly

---

## MCP Editor Integration

When interacting with Unity Editor via MCP for Unity:

1. **Resource-first**: check `mcpforunity://editor/state` before acting; ensure `ready_for_tools` is true
2. **batch_execute**: 10–100× faster than sequential calls; max 25 per batch
3. **After script changes**: call `refresh_unity(mode="force", wait_for_ready=True)` then `read_console(types=["error"])`
4. **Finding objects**: `find_gameobjects` → read resource → then modify

Common resource URIs:

| Resource | URI |
|----------|-----|
| Editor state | `mcpforunity://editor/state` |
| Project info | `mcpforunity://project/info` |
| Scene hierarchy | `mcpforunity://scene/gameobject-api` |
| Tags / Layers | `mcpforunity://project/tags`, `mcpforunity://project/layers` |
| Active instances | `mcpforunity://instances` |

For full tool reference, load **`unity-mcp-orchestrator`**.

---

## Core Code Patterns

### MonoBehaviour (standard template)

```csharp
using UnityEngine;

public class PlayerController : MonoBehaviour
{
    [Header("Movement")]
    [SerializeField] private float moveSpeed = 5f;
    [SerializeField] private float jumpForce = 10f;

    [Header("Ground Check")]
    [SerializeField] private Transform groundCheck;
    [SerializeField] private float groundRadius = 0.2f;
    [SerializeField] private LayerMask groundLayer;

    private Rigidbody2D _rb;
    private bool _isGrounded;

    private void Awake()
    {
        _rb = GetComponent<Rigidbody2D>();
    }

    private void FixedUpdate()
    {
        _isGrounded = Physics2D.OverlapCircle(groundCheck.position, groundRadius, groundLayer);
        float horizontal = Input.GetAxisRaw("Horizontal");
        _rb.velocity = new Vector2(horizontal * moveSpeed, _rb.velocity.y);
    }
}
```

### ScriptableObject (data container)

```csharp
using UnityEngine;

[CreateAssetMenu(fileName = "NewEnemyData", menuName = "Game/Enemy Data")]
public class EnemyData : ScriptableObject
{
    public string enemyName;
    public int maxHealth = 100;
    public float moveSpeed = 3f;
    public int damage = 10;
    public float attackRange = 2f;
}
```

### Custom Profiler Marker

```csharp
using Unity.Profiling;

public class MySystem : MonoBehaviour
{
    static readonly ProfilerMarker s_Marker = new ProfilerMarker("MySystem.Update");

    void Update()
    {
        using (s_Marker.Auto())
        {
            ProcessEntities();
        }
    }
}
```

---

## References

- [Unity 6 LTS Documentation](https://docs.unity3d.com/)
- [Unity Learn](https://learn.unity.com/)
- [Unity Best Practices](https://unity.com/how-to)
- [Unity Package Manager](https://docs.unity3d.com/Manual/PackageManager.html)
