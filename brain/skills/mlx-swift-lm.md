# mlx-swift-lm Skill

## 1. Overview & Triggers

mlx-swift-lm is a Swift package for running Large Language Models (LLMs) and Vision-Language Models (VLMs) on Apple Silicon using MLX. It supports local inference, streaming generation, wired-memory coordination, tool calling, LoRA/DoRA fine-tuning, and embeddings.

### When to Use This Skill
- Running LLM/VLM inference on macOS/iOS with Apple Silicon
- Streaming text generation from local models
- Coordinating concurrent inference with wired-memory policies and tickets
- Tool calling / function calling with models
- LoRA adapter training and fine-tuning
- Text embeddings for RAG/semantic search
- Porting model architectures from Python MLX-LM to Swift

### Architecture Overview
```
MLXLMCommon     - Core infra (ModelContainer, ChatSession, Evaluate, KVCache, wired memory helpers)
MLXLLM          - Text-only LLM support (Llama, Qwen, Gemma, Phi, DeepSeek, etc.)
MLXVLM          - Vision-Language Models (Qwen-VL, PaliGemma, Gemma3, etc.)
MLXEmbedders    - Embedding models and pooling utilities
```

## 2. Key File Reference

| Purpose | File Path |
|---------|-----------|
| Thread-safe model wrapper | `Libraries/MLXLMCommon/ModelContainer.swift` |
| Simplified chat API | `Libraries/MLXLMCommon/ChatSession.swift` |
| Generation & streaming APIs | `Libraries/MLXLMCommon/Evaluate.swift` |
| KV cache types | `Libraries/MLXLMCommon/KVCache.swift` |
| Wired-memory policies | `Libraries/MLXLMCommon/WiredMemoryPolicies.swift` |
| Wired-memory measurement helpers | `Libraries/MLXLMCommon/WiredMemoryUtils.swift` |
| Model configuration | `Libraries/MLXLMCommon/ModelConfiguration.swift` |
| Chat message types | `Libraries/MLXLMCommon/Chat.swift` |
| Tool call processing | `Libraries/MLXLMCommon/Tool/ToolCallFormat.swift` |
| Concurrency utilities | `Libraries/MLXLMCommon/Utilities/SerialAccessContainer.swift` |
| LLM factory & registry | `Libraries/MLXLLM/LLMModelFactory.swift` |
| VLM factory & registry | `Libraries/MLXVLM/VLMModelFactory.swift` |
| LoRA configuration | `Libraries/MLXLMCommon/Adapters/LoRA/LoRAContainer.swift` |
| LoRA training | `Libraries/MLXLLM/LoraTrain.swift` |

## 3. Quick Start

### LLM Chat (Simplest API)

```swift
import MLXLLM
import MLXLMCommon
import MLXLMHuggingFace  // from swift-huggingface-mlx
import MLXLMTokenizers   // from swift-tokenizers-mlx

let modelContainer = try await LLMModelFactory.shared.loadContainer(
    from: HubClient.default,
    using: TokenizersLoader(),
    configuration: .init(id: "mlx-community/Qwen3-4B-4bit")
)

let session = ChatSession(modelContainer)

let response = try await session.respond(to: "What is Swift?")
print(response)

for try await chunk in session.streamResponse(to: "Explain structured concurrency") {
    print(chunk, terminator: "")
}
```

### VLM with Image

```swift
import MLXVLM
import MLXLMCommon
import MLXLMHuggingFace  // from swift-huggingface-mlx
import MLXLMTokenizers   // from swift-tokenizers-mlx

let modelContainer = try await VLMModelFactory.shared.loadContainer(
    from: HubClient.default,
    using: TokenizersLoader(),
    configuration: .init(id: "mlx-community/Qwen2-VL-2B-Instruct-4bit")
)
