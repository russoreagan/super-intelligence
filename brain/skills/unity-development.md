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
