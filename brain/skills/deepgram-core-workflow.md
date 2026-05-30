# Deepgram Core Workflow: Real-time Streaming

## Overview
Implement real-time streaming transcription using Deepgram's WebSocket API for live audio processing.

## Prerequisites
- Completed `deepgram-install-auth` setup
- Understanding of WebSocket patterns
- Audio input source (microphone or stream)

## Instructions

### Step 1: Set Up WebSocket Connection
Initialize a live transcription connection with Deepgram.

### Step 2: Configure Stream Options
Set up interim results, endpointing, and language options.

### Step 3: Handle Events
Implement handlers for transcript events and connection lifecycle.

### Step 4: Stream Audio Data
Send audio chunks to the WebSocket connection.

## Output
- Live transcription WebSocket client
- Event handlers for real-time results
- Audio streaming pipeline
- Graceful connection management

## Error Handling
| Error | Cause | Solution |
|-------|-------|----------|
| Connection Closed | Network interruption | Implement auto-reconnect |
| Buffer Overflow | Too much audio data | Reduce sample rate or chunk size |
| No Transcripts | Silent audio | Check audio levels and format |
| High Latency | Network/processing delay | Use interim results |

## Examples

### TypeScript WebSocket Client
```typescript
// services/live-transcription.ts
import { createClient, LiveTranscriptionEvents } from '@deepgram/sdk';

export interface LiveTranscriptionOptions {
 model?: 'nova-2' | 'nova' | 'enhanced' | 'base';
 language?: string;
 punctuate?: boolean;
 interimResults?: boolean;
 endpointing?: number;
 vadEvents?: boolean;
}

export class LiveTranscriptionService {
 private client;
 private connection: any = null;

 constructor(apiKey: string) {
 this.client = createClient(apiKey);
 }

 async start(
 options: LiveTranscriptionOptions = {},
 handlers: {
 onTranscript?: (transcript: string, isFinal: boolean) => void;
 onError?: (error: Error) => void;
 onClose?: () => void;
 } = {}
 ): Promise<void> {
 this.connection = this.client.listen.live({
 model: options.model || 'nova-2',
 language: options.language || 'en',
 punctuate: options.punctuate ?? true,
 interim_results: options.interimResults ?? true,
 endpointing: options.endpointing ?? 300,
 vad_events: options.vadEvents ?? true,
 });

 this.connection.on(LiveTranscriptionEvents.Open, () => {
 console.log('Deepgram connection opened');
 });

 this.connection.on(LiveTranscriptionEvents.Transcript, (data: any) => {
 const transcript = data.channel.alternatives[0].transcript;
 const isFinal = data.is_final;

 if (transcript && handlers.onTranscript) {
 handlers.onTranscript(transcript, isFinal);
 }
 });

 this.connection.on(LiveTranscriptionEvents.Error, (error: Error) => {
 console.error('Deepgram error:', error);
 handlers.onError?.(error);
 });

 this.connection.on(LiveTranscriptionEvents.Close, () => {
 console.log('Deepgram connection closed');
 handlers.onClose?.();
 });
 }

 send(audioData: Buffer): void {
 if (this.connection) {
 this.connection.send(audioData);
 }
 }

 async stop(): Promise<void> {
 if (this.connection) {
 this.connection.finish();
 this.connection = null;
 }
 }
}
```
