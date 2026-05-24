---
name: debugging-websocket-issues
description: Use when seeing WebSocket errors like "Invalid frame header", "RSV1 must be clear", or "WS_ERR_UNEXPECTED_RSV_1" - covers multiple WebSocketServer conflicts, compression issues, and raw frame debugging techniques
tags: websocket, debugging, ws, node
disable-model-invocation: true

---
# Debugging WebSocket Issues

## Overview

WebSocket "invalid frame header" errors often stem from raw HTTP being written to an upgraded socket, not actual frame corruption. The most common cause is multiple `WebSocketServer` instances conflicting on the same HTTP server.

## When to Use

- Error: `Invalid WebSocket frame: RSV1 must be clear`
- Error: `WS_ERR_UNEXPECTED_RSV_1`
- Error: `Invalid frame header`
- WebSocket connects then immediately disconnects with code 1006
- Server logs success but client receives garbage data

## Quick Reference

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| RSV1 must be clear | Multiple WSS on same server OR compression mismatch | Use `noServer: true` mode |
| Hex starts with `48545450` | Raw HTTP on WebSocket (0x48='H') | Check for conflicting upgrade handlers |
| Code 1006, no reason | Abnormal closure, often server-side abort | Check `abortHandshake` calls |
| Works isolated, fails in app | Something else writing to socket | Audit all upgrade listeners |

## The Multiple WebSocketServer Bug

### Problem

When attaching multiple `WebSocketServer` instances to the same HTTP server using the `server` option:

```typescript
// ❌ BAD - Both servers add upgrade listeners, causing conflicts
const wss1 = new WebSocketServer({ server, path: '/ws' });
const wss2 = new WebSocketServer({ server, path: '/ws/other' });
```

**What happens:**
1. Client connects to `/ws`
2. BOTH upgrade handlers fire (Node.js EventEmitter calls all listeners)
3. `wss1` matches path, handles upgrade successfully
4. `wss2` doesn't match, calls `abortHandshake(socket, 400)`
5. Raw `HTTP/1.1 400 Bad Request` written to the now-WebSocket socket
6. Client receives HTTP text as WebSocket frame data
7. First byte `0x48` ('H') interpreted as: RSV1=1, opcode=8 → invalid frame

### Solution

Use `noServer: true` and manually route upgrades:

```typescript
// ✅ GOOD - Single upgrade handler routes to correct server
const wss1 = new WebSocketServer({ noServer: true, perMessageDeflate: false });
const wss2 = new WebSocketServer({ noServer: true, perMessageDeflate: false });

server.on('upgrade', (request, socket, head) => {
  const pathname = new URL(request.url || '', `http://${request.headers.host}`).pathname;

  if (pathname === '/ws') {
    wss1.handleUpgrade(request, socket, head, (ws) => {
      wss1.emit('connection', ws, request);
    });
  } else if (pathname === '/ws/other') {
    wss2.handleUpgrade(request, socket, head, (ws) => {
      wss2.emit('connection', ws, request);
    });
  } else {
    socket.destroy();
  }
});
```

## Debugging Techniques

### Raw Frame Inspection

Hook into the socket to see actual bytes received:

```typescript
ws.on('open', () => {
  const socket = ws._socket;
  const originalPush = socket.push.bind(socket);

  socket.push = function(chunk, encoding) {
    if (chunk) {
      console.log('First 20 bytes (hex):', chunk.slice(0, 20).toString('hex'));
      const byte0 = chunk[0];
      console.log(`FIN: ${!!(byte0 & 0x80)}, RSV1: ${!!(byte0 & 0x40)}, Opcode: ${byte0 & 0x0f}`);

      // Check if it's actually HTTP text
      if (chunk.slice(0, 4).toString() === 'HTTP') {
        console.log('*** RECEIVED RAW HTTP ON WEBSOCKET ***');
      }
    }
    return originalPush(chunk, encoding);
  };
});
```

### Key Hex Patterns

- `81` = FIN + text frame (normal)
- `82` = FIN + binary frame (normal)
- `88` = FIN + close frame (normal)
- `48545450` = "HTTP" - raw HTTP on WebSocket (bug!)
- `c1` or similar with bit 6 set = compressed frame (RSV1=1)

## Common Mistakes

| Mistake | Result | Fix |
|---------|--------|-----|
| Multiple WSS with `server` option | HTTP 400 written to socket | Use `noServer: true` |
| `perMessageDeflate: true` (default in older ws) | RSV1 set on frames | Explicitly set `perMessageDeflate: false` |
| Not checking upgrade headers | Miss compression negotiation | Log `sec-websocket-extensions` header |
| Assuming RSV1 error = compression | Could be raw HTTP | Check if bytes decode as ASCII "HTTP" |

## ESM/CommonJS and Socket.IO Initialization

### Problem

Next.js production uses ESM. CommonJS globals (`module`, `require`, `__dirname`, `__filename`) **do not exist** in ESM and throw `ReferenceError: module is not defined` at runtime.

If your Socket.IO init (e.g. `setSocketServer(io)`) references these globals—even indirectly, e.g. in debug logging—the init throws, Socket.IO never initializes, and **all** `emitToSession` (or equivalent) calls return false. The server may log "Degraded mode" or "Socket.IO not initialized."

### Symptoms

- `emitToSession` / broadcast always returns false
- Server shows "Socket.IO not initialized" or "Degraded mode"
- Real-time features (maps, scene images, game state updates) never reach clients
- Logs show `ReferenceError: module is not defined` at startup

### Fix

- **Avoid CommonJS globals** in code that runs in Next.js production: no `module.id`, `module.filename`, `require`, `__dirname`, `__filename` unless using explicit ESM-compatible polyfills.
- **Validate init** – If emit fails, check whether init threw. Add stack-trace logging in the null case to trace callers; the real bug is often in init, not in the emit path.

### Quick Reference

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `ReferenceError: module is not defined` at init | CommonJS global in ESM build | Remove `module`, `require`, `__dirname`, `__filename` |
| All emits return false; init "succeeds" | Init threw before `setSocketServer` | Check init stack; fix the throw |
| Degraded mode, no Socket.IO | Init failed; catch swallowed error | Log init errors; avoid referencing CJS globals |

### Module duplication (Next.js server + API routes)

- **Problem:** server.js and Next.js API routes run from different bundles. setSocketServer(io) is called from server.js on its copy of socket-service; API routes use a separate copy from .next/server/ where socketServer stays null.
- **Symptoms:** Init logs "Socket.IO initialized"; emits from API routes / map job still get socketServer null.
- **Fix:** Store socket server on `globalThis` (e.g. `(globalThis as any)[GLOBAL_KEY]`) so all bundle instances share the same reference. Both setSocketServer and getSocketServerRef read/write from globalThis.

## Verification Checklist

After fixing, verify:
- [ ] `RSV1: false` in frame inspection
- [ ] `Extensions header: NONE` in upgrade response
- [ ] No `HTTP/1.1` in raw frame data
- [ ] Messages received match sent payload size
- [ ] Multiple broadcasts work (test interval sends)
- [ ] No `module`, `require`, `__dirname`, `__filename` in Socket.IO init path (for ESM builds)
