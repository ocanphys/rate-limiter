from sharedstore import RedisClient


class Bucket:
    """
    A token bucket backed by a single Redis key.

    Used in two roles:
      - Master bucket: a shared pool that limits total throughput across all users.
        Tokens are drawn from it when a new user is admitted (via BucketRegistry)
        and replenished at the start of each refill cycle.
      - User bucket: one per active IP. Tokens are consumed on each request and
        topped up from the master during the refill cycle.

    Both roles use the same consume/refill logic; the distinction is enforced by
    the caller (BucketRegistry and refill_cycle).

    All reads and writes go through Lua scripts executed atomically by Redis,
    so concurrent threads and processes cannot interleave operations on the same key.
    """

    # KEYS[1] = bucket key
    # ARGV[1] = number of tokens to consume
    # Returns 1 if the tokens were consumed, 0 if the bucket had insufficient tokens.
    _CONSUME_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]))
if current == nil or current < tonumber(ARGV[1]) then
    return 0  -- bucket empty or missing
end
redis.call('SET', KEYS[1], current - tonumber(ARGV[1]))
return 1  -- consumed successfully
"""

    # KEYS[1] = bucket key
    # ARGV[1] = tokens to add; pass -1 to refill to max without reading the current value
    # ARGV[2] = max_tokens (cap)
    # Returns the new token count.
    _REFILL_SCRIPT = """
local max_tokens = tonumber(ARGV[2])
local add = tonumber(ARGV[1])
if add < 0 then
    redis.call('SET', KEYS[1], max_tokens)  -- full refill: overwrite directly, no read needed
    return max_tokens
end
local current = tonumber(redis.call('GET', KEYS[1])) or 0
local new_val = math.min(current + add, max_tokens)  -- cap at max to prevent overflow
redis.call('SET', KEYS[1], new_val)
return new_val
"""

    def __init__(self, id: str, max_tokens: int, client: RedisClient):
        self.id = id
        self.max_tokens = max_tokens
        self._key = f"bucket:{id}"
        self._redis = client
        self._redis.set_value(self._key, str(max_tokens))

    def refill(self, tokens: int = None):
        """Atomically add tokens to the bucket, capped at max_tokens.

        Called with no argument to fully reset the bucket (master at cycle start).
        Called with a deficit value to top up a user bucket from the master.
        """
        add = tokens if tokens is not None else -1
        self._redis.eval_script(self._REFILL_SCRIPT, [self._key], [add, self.max_tokens])

    def use_tokens(self, n: int = 1) -> bool:
        """Atomically consume n tokens. Returns True if allowed, False if the bucket is empty."""
        result = self._redis.eval_script(self._CONSUME_SCRIPT, [self._key], [n])
        return result == 1


# --- Example usage ---
if __name__ == "__main__":
    client = RedisClient()
    bucket = Bucket(id="user:alice", max_tokens=5, client=client)

    # Use tokens
    for i in range(7):
        result = bucket.use_tokens()
        remaining = bucket._redis.get_value(bucket._key)
        print(
            f"Request {i + 1}: {'allowed' if result else 'blocked'} | tokens remaining: {remaining}"
        )

    print()

    # Partial refill
    bucket.refill(tokens=3)
    print(f"After partial refill (+3): {bucket._redis.get_value(bucket._key)}")

    # Full refill
    bucket.refill()
    print(f"After full refill: {bucket._redis.get_value(bucket._key)}")

    # Partial refill that would overflow
    bucket.refill(tokens=99)
    print(f"After overflow refill (+99): {bucket._redis.get_value(bucket._key)}")
