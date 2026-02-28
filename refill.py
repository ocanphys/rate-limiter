import logging
import os
import socket
import threading
import time
from functions import RedisClient
from bucket import Bucket
from registry import BucketRegistry
from config import (
    REDIS_HOST,
    REDIS_PORT,
    REDIS_DB,
    USER_MAX_TOKENS,
    MASTER_MAX_TOKENS,
    REFILL_INTERVAL,
)

logger = logging.getLogger(__name__)


# KEYS[1] = user bucket key, KEYS[2] = master bucket key
# ARGV[1] = user max_tokens
# Atomically reads the current token count, recomputes the deficit, deducts it
# from the master, and tops the user bucket up to max — all in one round-trip.
# Recomputing deficit inside the script prevents a race where a request consumes
# tokens between the Python-side read and the SET.
# Returns 1 if topped up, 0 if the master had insufficient tokens.
_TOPUP_SCRIPT = """
local user_max = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1])) or 0
local deficit = user_max - current
if deficit <= 0 then return 0 end
local master = tonumber(redis.call('GET', KEYS[2])) or 0
if master < deficit then return 0 end
redis.call('SET', KEYS[2], master - deficit)
redis.call('SET', KEYS[1], user_max)
return 1
"""

# KEYS[1] = user bucket key, KEYS[2] = master bucket key
# ARGV[1] = tokens to refund, ARGV[2] = master max_tokens (cap)
# Atomically returns the user's token allocation to the master and deletes the
# idle bucket in a single round-trip, so concurrent registrations immediately
# benefit from the freed capacity.
# Returns the master's new token count.
_EVICT_AND_RETURN_SCRIPT = """
local refund = tonumber(ARGV[1])
local master_max = tonumber(ARGV[2])
local master_current = tonumber(redis.call('GET', KEYS[2])) or 0
local new_master = math.min(master_current + refund, master_max)  -- cap at max
redis.call('SET', KEYS[2], new_master)
redis.call('DEL', KEYS[1])  -- delete the idle user bucket
return new_master
"""


def refill_cycle(
    master: Bucket, registry: BucketRegistry, client: RedisClient, user_max_tokens: int
):
    """Run one refill cycle: reset the master, then top up or evict every active user bucket.

    Called periodically by _refill_loop. The cycle has two phases:

    Phase 1 — reset the master.
      The master is refilled to its maximum, giving the cycle a fresh token
      budget to distribute. Users are served first-come-first-served in
      registry insertion order until the master runs dry.

    Phase 2 — process each user bucket.
      For each registered user the current token count determines the outcome:
        - Key missing: the bucket was deleted externally; remove from registry.
        - tokens == max: user made no requests since the last cycle (idle);
          atomically refund their token allocation to the master and delete
          the bucket, so concurrent registrations can use the freed capacity.
        - tokens < max: user was active; draw the deficit from the master and
          top the bucket back up to max. If the master is already depleted,
          the user is skipped and keeps whatever tokens they have left.
    """
    # Phase 1: reset the master so this cycle has a full budget to distribute.
    master.refill()
    logger.info("refill cycle started — master reset to %d tokens", master.max_tokens)

    # Phase 2: snapshot the registry, then process each bucket in insertion order.
    all_buckets = registry.get_all()
    logger.info("%d active buckets found", len(all_buckets))

    for user_id, bucket_key in all_buckets.items():
        current_str = client.get_value(bucket_key)

        if current_str is None:
            # Bucket key was deleted externally (e.g. Redis eviction) — clean up registry.
            registry.unregister(user_id)
            logger.info("%s bucket key missing — unregistered", user_id)
            continue

        current = int(current_str)

        if current >= user_max_tokens:
            # Full bucket means the user made zero requests since the last cycle.
            # Atomically refund their allocation to the master and delete the bucket,
            # so any concurrent registrations can immediately use the freed capacity.
            new_master = client.eval_script(
                _EVICT_AND_RETURN_SCRIPT,
                [bucket_key, master._key],
                [user_max_tokens, master.max_tokens],
            )
            registry.unregister(user_id)
            logger.info(
                "%s idle — evicted, %d tokens returned to master (master now %s/%d)",
                user_id,
                user_max_tokens,
                new_master,
                master.max_tokens,
            )
        else:
            # User was active. Atomically recompute deficit, draw from master, and
            # restore the bucket to max in one Lua script to avoid races with
            # concurrent requests consuming tokens between the read and the SET.
            topped_up = client.eval_script(
                _TOPUP_SCRIPT,
                [bucket_key, master._key],
                [user_max_tokens],
            )
            if topped_up:
                logger.info(
                    "%s refilled (was %d, deficit drawn from master)",
                    user_id,
                    current,
                )
            else:
                # Master ran out of tokens earlier in this cycle; skip this user.
                # They keep their remaining tokens and will be retried next cycle.
                logger.info(
                    "%s skipped — master depleted before reaching this user (tokens=%d/%d)",
                    user_id,
                    current,
                    user_max_tokens,
                )


def _refill_loop(master: Bucket, registry: BucketRegistry, client: RedisClient):
    """Block for REFILL_INTERVAL seconds, then run a refill cycle. Repeat forever.

    Uses a Redis distributed lock so only one instance runs the refill at a time
    across all gateway replicas. An instance that loses the lock skips the cycle
    and tries again next interval — it does not queue up or block.

    The lock is held for the full REFILL_INTERVAL and never manually released.
    This prevents any other instance from acquiring it within the same window.
    The TTL also acts as a dead-man's switch — if the leader crashes, the lock
    expires automatically and another instance takes over next cycle.
    """
    # TTL = REFILL_INTERVAL: lock held for the full interval, never manually released,
    # so no other instance can run a duplicate cycle in this window.
    lock = client._client.lock("refill:leader", timeout=REFILL_INTERVAL)
    while True:
        time.sleep(REFILL_INTERVAL)
        if lock.acquire(blocking=False):
            logger.info(
                "refill lock acquired — host: %s pid: %d",
                socket.gethostname(),
                os.getpid(),
            )
            try:
                refill_cycle(master, registry, client, USER_MAX_TOKENS)
            except Exception:
                logger.exception("refill cycle failed")
        else:
            logger.info("refill skipped — lock held by another instance")


def start_background_thread(
    master: Bucket, registry: BucketRegistry, client: RedisClient
) -> threading.Thread:
    """Start the refill loop as a daemon thread and return it.

    Called once at startup. The thread runs for the lifetime
    of the process and does not need to be joined. Because it is a daemon
    thread, it is killed automatically when the main process exits.
    """
    thread = threading.Thread(
        target=_refill_loop, args=(master, registry, client), daemon=True
    )
    thread.start()
    return thread


# --- Example usage ---
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    _client = RedisClient(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    _master = Bucket(id="master", max_tokens=MASTER_MAX_TOKENS, client=_client)
    _registry = BucketRegistry(_client, master_key=_master._key)
    _thread = start_background_thread(_master, _registry, _client)
    _thread.join()  # block the main thread so the daemon thread stays alive
