# Redis Rate Limiter

A modular Redis-backed rate limiter designed to sit in front of web application components as a gateway layer. Built with Python and FastAPI (gateway planned).

## Architecture

```
HTTP Request
     ↓
Gateway (planned)        ← FastAPI, proxies requests to target app
     ↓
Rate Limiter Layer       ← checks token bucket per IP
     ↓
Redis                    ← shared state (buckets + registry)
     ↓
Target App
```

The refill cycle runs as a **separate process**, making the system horizontally scalable. Multiple gateway workers share state through Redis.

---

## Project Structure

| File | Description |
|---|---|
| `functions.py` | `RedisClient` — wrapper around redis-py with automatic fallback to fakeredis |
| `bucket.py` | `Bucket` — token bucket per user/IP stored in Redis |
| `registry.py` | `BucketRegistry` — Redis Hash tracking all active buckets by user ID |

---

## Modules

### `functions.py` — RedisClient

Wraps `redis-py`. On initialization, attempts to connect to a real Redis server. If the connection fails, falls back to `fakeredis` and prints a warning — no code changes needed to switch between environments.

```python
from functions import RedisClient

r = RedisClient(host='localhost', port=6379)
r.set_value('key', 'value', ttl=60)
r.get_value('key')       # 'value'
r.delete_value('key')
r.get_ttl('key')         # seconds remaining, -1 = no expiry, -2 = gone
r.incr('counter')        # atomic increment
r.decr('counter')        # atomic decrement
r.hset('myhash', 'field', 'value')
r.hget('myhash', 'field')
r.hgetall('myhash')      # returns dict
r.hdel('myhash', 'field')
```

---

### `bucket.py` — Bucket

Token bucket stored in Redis. Each bucket has a unique ID (typically a user ID or IP address) and a maximum token capacity. Every request consumes one token. When the bucket is empty, requests are rejected.

```python
from bucket import Bucket
from functions import RedisClient

client = RedisClient()
bucket = Bucket(id='192.168.1.1', max_tokens=100, client=client)

bucket.use_token()          # True if allowed, False if empty
bucket.refill()             # refill to max
bucket.refill(tokens=10)    # add 10 tokens, capped at max
```

**Note:** `refill` is not atomic — it is a `GET` + `SET` operation. This is safe for single-process use. For multi-process deployments, a Lua script should be used to make it atomic.

---

### `registry.py` — BucketRegistry

Tracks all active buckets in a Redis Hash (`bucket_registry`), mapping `user_id → bucket_key`. Lives entirely in Redis so it is visible to all processes without scanning the keyspace.

Used by the refill process to iterate active buckets efficiently — `HGETALL` is O(N) over active users only, not the entire Redis keyspace.

```python
from registry import BucketRegistry
from functions import RedisClient

client = RedisClient()
registry = BucketRegistry(client)

registry.register('192.168.1.1', 'bucket:192.168.1.1')
registry.is_registered('192.168.1.1')   # True
registry.get('192.168.1.1')             # 'bucket:192.168.1.1'
registry.get_all()                      # {'192.168.1.1': 'bucket:192.168.1.1', ...}
registry.unregister('192.168.1.1')      # called by refill process, not gateway
```

---

## Rate Limiting Strategy

**Token bucket** per IP address:
- Each IP starts with `max_tokens` tokens
- Each request consumes 1 token (`use_token`)
- A background refill process runs every N seconds and refills all active buckets
- If a bucket is full at refill time (no requests since last cycle), it is deleted and the user is unregistered — idle cleanup

**Bucket lifecycle:**
```
First request from IP  →  create Bucket, register in BucketRegistry
Each request           →  use_token() → True (allowed) or False (rejected 429)
Every N seconds        →  refill process: refill active buckets, delete idle ones
```

---

## Setup

```bash
# Install dependencies
uv add redis fakeredis

# Run with real Redis (Docker)
docker run -d -p 6379:6379 redis

# Or use fakeredis (no server needed) — automatic fallback if Redis is unreachable
```

---

## Planned

- `gateway.py` — FastAPI gateway that proxies HTTP requests and enforces rate limits
- `refill.py` — separate process for scheduled bucket refill and idle cleanup
- Concurrent connection limiting (separate counter per IP alongside token bucket) - protects against slowloris / sending slow HTTP requests to keep many connections open without completing the request.
- Atomic refill via Lua script for multi-process safety
