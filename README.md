# Redis Rate Limiter Gateway

Token based rate limiter built with Python and FastAPI. Enforces per-IP connection token bucket limits. Tokens to the client buckets are only replenished by tokens coming from a master bucket that controls the total token generation rate globally. 

Each client gets a token bucket assigned and must consume a token for each HTTP request. Bucket states and a bucket registry keeping track of active buckets are stored on a Redis for fast access. In order to support multiple gateways working in parallel, we rely on atomic Redis operations via Lua scripts, avoiding get and set race conditions as the bucket states are being modified.

There is a refill process that runs periodically, first topping up the master bucket and then transferring the tokens in the master bucket to client buckets, replenishing the used tokens for existing connections. If a client bucket is full when the cycle runs, that bucket is evicted and tokens are returned to the master bucket for making resources available to others. In order the make the refill job failproof, it is designed as a daemon process that runs on every gateway server that is spun but only one of them can be active at a time relying on a distributed access lock that lives on the Redis. This lock has a TTL set as long as the cycle period, and whenever it expires, whicever gateway acquires the lock runs the refill cycle. If a gateway fails, another gateway will pick up the refill duty during the next cycle.

---

## Components

### `config.py` — Configuration

Central place for all tuneable parameters. No logic — just constants imported by every other module.

| Parameter | Default | Description |
|---|---|---|
| `USER_MAX_TOKENS` | 20 | Token capacity of each user bucket |
| `MASTER_MAX_TOKENS` | 1000 | Total token budget reset each refill cycle |
| `REFILL_INTERVAL` | 5s | Seconds between refill cycles |
| `MAX_USERS` | 50 | Maximum concurrent active users |

`MASTER_MAX_TOKENS / USER_MAX_TOKENS` controls how many users can be fully refilled each cycle (default: 50). Users beyond that are skipped and retried next cycle.

---

### `sharedstore.py` — RedisClient

Thin wrapper around `redis-py` that exposes the operations used by the rest of the system. On startup it attempts a real Redis connection; if that fails it falls back to `fakeredis` (in-memory, no server needed) and logs a warning.

Exposes: `set_value`, `get_value`, `delete_value`, `get_ttl`, `incr`, `decr`, `hset`, `hdel`, `hget`, `hgetall`, and `eval_script` for running Lua scripts atomically.

All token operations in the system go through `eval_script` — this is what makes concurrent reads and writes safe without application-level locking.

---

### `bucket.py` — Bucket

Represents a single token bucket stored as an integer key in Redis. Used in two roles:

- **Master bucket** (`bucket:master`) — shared pool that funds new user buckets and is reset to full at the start of each refill cycle.
- **User bucket** (`bucket:<ip>`) — one per active IP, consumed on each request and topped up by the refill cycle.

Both roles use the same two Lua scripts:

- `_CONSUME_SCRIPT` — atomically checks if enough tokens exist and decrements. Returns `1` if allowed, `0` if the bucket is empty.
- `_REFILL_SCRIPT` — atomically adds tokens up to `max_tokens`. Pass `tokens=-1` to reset to max without reading current value.

---

### `registry.py` — BucketRegistry

Tracks all active user buckets in a Redis Hash (`bucket_registry`), mapping `ip → bucket_key`. Lets the refill cycle iterate all live buckets with a single `HGETALL` instead of scanning the Redis keyspace.

User admission is handled by `_REGISTER_SCRIPT`, a single Lua script that performs all checks and writes atomically in one round-trip:

1. Reject if active user count is at `MAX_USERS`
2. Return `-1` (already registered) if the IP is known — gateway proceeds normally
3. Reject if master bucket can't fund a full user bucket
4. Deduct `USER_MAX_TOKENS` from master, add IP to registry hash, create user bucket at full capacity

This means a new user's first request is never double-charged — the registration and bucket creation happen together or not at all.

---

### `refill.py` — Refill Cycle

Runs as a daemon thread inside each gateway process. On every interval, it tries to acquire a Redis distributed lock (`refill:leader`). Only the instance that wins the lock runs the cycle — losers skip and try again next interval. If the leader crashes, the lock expires automatically (TTL = `REFILL_INTERVAL`) and another instance takes over.

The cycle has two phases:

**Phase 1** — Reset the master bucket to `MASTER_MAX_TOKENS`, giving the cycle a fresh budget.

**Phase 2** — Snapshot the registry, then process each user bucket:
- **Key missing** — bucket was evicted by Redis or deleted externally; remove from registry.
- **Tokens == max** — user made no requests since last cycle (idle); atomically delete the bucket and return the full token allocation to the master (`_EVICT_AND_RETURN_SCRIPT`), freeing capacity for new users immediately.
- **Tokens < max** — user was active; atomically recompute deficit, draw from master, reset bucket to max (`_TOPUP_SCRIPT`). If master is depleted, skip this user — they keep remaining tokens and are retried next cycle.

Both the eviction and top-up use Lua scripts to ensure the read-modify-write is atomic and cannot race with concurrent requests.

---

### `gateway.py` — HTTP Gateway

FastAPI app that wires all components together. On startup (`lifespan`), it initialises the Redis client, master bucket, registry, and kicks off the refill background thread.

Every request on `GET /` goes through two checks:

1. `registry.register(ip)` — admits or rejects the IP. New users are funded from the master; known users pass through immediately.
2. `bucket.consume(1)` — decrements the user's token count. Returns `429` if empty.

On success, the response includes a real-time snapshot of master and all user bucket states — useful for observing the system during testing.

---

## Setup

**1. Install and start Redis**

```bash
brew install redis
redis-server
```

Redis listens on `localhost:6379` by default. Leave this terminal running.

**2. Install dependencies**

```bash
uv sync
```

**3. Start the gateways**

Open a new terminal:

```bash
uvicorn gateway:app --port 8000
```

The gateway is now accepting requests at `http://localhost:8000`.

and can add more gateways:

```bash
uvicorn gateway:app --port 8001
```
and so on
