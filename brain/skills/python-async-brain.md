---
name: python-async-brain
description: Key Python asyncio patterns for writing correct async tool steps. Covers gather, tasks, queues, semaphores, and common pitfalls.
disable-model-invocation: true

---
# Python Async Patterns — Brain Reference

## Running things concurrently
```python
# Run multiple coroutines in parallel, wait for all
results = await asyncio.gather(coro1(), coro2(), coro3())

# Fire and forget (don't lose the task reference)
task = asyncio.create_task(my_coro())
await task  # or collect tasks and await later
```

## Async context managers
```python
async with aiofiles.open("file.txt") as f:
    content = await f.read()

async with aiohttp.ClientSession() as session:
    async with session.get(url) as resp:
        data = await resp.json()
```

## Queues (producer/consumer)
```python
q = asyncio.Queue()
await q.put(item)
item = await q.get()
q.task_done()
await q.join()  # block until all items processed
```

## Limiting concurrency
```python
sem = asyncio.Semaphore(5)
async with sem:
    await do_work()
```

## Running sync code without blocking
```python
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(None, blocking_function, arg)
```

## Common pitfalls
- `await` is required to actually run a coroutine — bare `coro()` creates it but doesn't run it
- `asyncio.sleep(0)` yields control to let other tasks run
- Don't call blocking I/O (open, requests, time.sleep) inside async functions — use async equivalents
- `asyncio.run()` creates a new event loop — only call at the top level, not inside async code
- Tasks are cancelled on `gather` error unless `return_exceptions=True`
