import logging
from sharedstore import RedisClient
from bucket import Bucket
from config import MAX_USERS, USER_MAX_TOKENS, MASTER_MAX_TOKENS

logger = logging.getLogger(__name__)


class BucketRegistry:
    """
    Tracks active connection token buckets in a Redis Hash (key: 'bucket_registry').

    Maps user_id -> bucket_key for every currently active user. This lets
    the refill cycle iterate all live buckets without scanning the full
    Redis keyspace.

    Lifecycle of a user bucket:
      1. First request from an IP: register() admits the user, deducts
         USER_MAX_TOKENS from the master, and creates the user's bucket.
      2. Subsequent requests: register() returns -1 (already known);
         the gateway proceeds directly to token consumption.
      3. Refill cycle: idle buckets (tokens == max) are deleted and
         unregistered; active ones are topped up from the master.

    The registry hash lives entirely in Redis, so it is visible to all
    gateway replicas. The refill job, however, must run in exactly one
    process — running it in multiple places would cause duplicate refills.
    """

    REGISTRY_KEY = "bucket_registry"

    # Atomically admits a new user in a single Redis round-trip.
    # Redis executes Lua scripts without interleaving other commands,
    # so all checks and writes below are free of TOCTOU races.
    #
    # KEYS[1] = registry hash key
    # KEYS[2] = master bucket key
    # ARGV[1] = user_id, ARGV[2] = bucket_key, ARGV[3] = cap, ARGV[4] = max_tokens
    #
    # Returns:
    #   1  — user admitted (new registration)
    #   0  — rejected: active user count is at the cap
    #  -1  — user already registered (idempotent; caller lets the request through)
    #  -2  — rejected: master bucket cannot fund a full user bucket
    _REGISTER_SCRIPT = """
local cap = tonumber(ARGV[3])
local max_tokens = tonumber(ARGV[4])
if redis.call('HLEN', KEYS[1]) >= cap then
    return 0  -- too many concurrent users
end
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then
    return -1  -- already registered; nothing to do
end
local master = tonumber(redis.call('GET', KEYS[2]))
if master == nil or master < max_tokens then
    return -2  -- master can't fund a full bucket for this user
end
redis.call('SET', KEYS[2], master - max_tokens)  -- deduct from master
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])    -- add user_id -> bucket_key to registry
redis.call('SET', ARGV[2], max_tokens)            -- create user bucket at full capacity
return 1
"""

    def __init__(self, client: RedisClient, master_key: str):
        self._redis = client
        self._master_key = master_key
        master_tokens = self._redis.get_value(self._master_key)
        logger.debug("registry initialised — master bucket: %s tokens=%s", self._master_key, master_tokens)

    def _master_tokens(self) -> str:
        """Return the current master token count as a string (for debug logging)."""
        return self._redis.get_value(self._master_key) or '0'

    def register(self, user_id: str, bucket_key: str) -> int:
        """Attempt to admit a user, executing all admission checks atomically.

        On first contact (result 1): deducts USER_MAX_TOKENS from the master
        and creates the user's bucket at full capacity.
        On repeat contact (result -1): no-op; the caller proceeds normally.

        Returns: 1 = admitted, 0 = cap reached, -1 = already registered, -2 = master depleted.
        """
        result = self._redis.eval_script(
            self._REGISTER_SCRIPT,
            [self.REGISTRY_KEY, self._master_key],
            [user_id, bucket_key, MAX_USERS, USER_MAX_TOKENS],
        )
        if logger.isEnabledFor(logging.DEBUG):
            master_remaining = self._master_tokens()
            if result == 1:
                logger.debug("%s admitted — %d tokens drawn from master, master remaining: %s", user_id, USER_MAX_TOKENS, master_remaining)
            elif result == -1:
                logger.debug("%s already registered — passing through, master tokens: %s", user_id, master_remaining)
            elif result == 0:
                logger.debug("%s rejected — user cap of %d reached, master tokens: %s", user_id, MAX_USERS, master_remaining)
            elif result == -2:
                logger.debug("%s rejected — master bucket depleted (needs %d, master tokens: %s)", user_id, USER_MAX_TOKENS, master_remaining)
        return result

    def unregister(self, user_id: str):
        """Remove a user from the registry. Called by the refill cycle when a bucket goes idle."""
        self._redis.hdel(self.REGISTRY_KEY, user_id)
        logger.debug("%s unregistered — bucket removed from registry", user_id)

    def get(self, user_id: str) -> str | None:
        """Return the bucket key for a user, or None if not registered."""
        return self._redis.hget(self.REGISTRY_KEY, user_id)

    def get_all(self) -> dict:
        """Return all {user_id: bucket_key} pairs. Called by the refill cycle to iterate active users."""
        return self._redis.hgetall(self.REGISTRY_KEY)

    def is_registered(self, user_id: str) -> bool:
        """Return True if the user has an active bucket."""
        return self.get(user_id) is not None


# --- Example usage ---
if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(levelname)s %(name)s: %(message)s')
    client = RedisClient()
    master = Bucket(id='master', max_tokens=MASTER_MAX_TOKENS, client=client)
    registry = BucketRegistry(client, master_key=master._key)

    # New users — master has enough tokens, cap not reached
    print(registry.register('192.168.1.1', 'bucket:192.168.1.1'))  # 1 (admitted)
    print(registry.register('192.168.1.2', 'bucket:192.168.1.2'))  # 1 (admitted)

    # Same IP again — idempotent, gateway lets the request through
    print(registry.register('192.168.1.1', 'bucket:192.168.1.1'))  # -1 (already registered)

    print("All buckets:", registry.get_all())
    print("Lookup 192.168.1.2:", registry.get('192.168.1.2'))
    print("Is 192.168.1.1 registered?", registry.is_registered('192.168.1.1'))

    registry.unregister('192.168.1.2')
    print("After removing 192.168.1.2:", registry.get_all())
    print("Is 192.168.1.2 registered?", registry.is_registered('192.168.1.2'))

    # Master depleted — small master only has enough for one user
    small_master = Bucket(id='small_master', max_tokens=USER_MAX_TOKENS, client=client)
    small_registry = BucketRegistry(client, master_key=small_master._key)
    print(small_registry.register('10.0.0.1', 'bucket:10.0.0.1'))  # 1 (admitted)
    print(small_registry.register('10.0.0.2', 'bucket:10.0.0.2'))  # -2 (master depleted)
    print("All buckets:", small_registry.get_all())
    # Cap reached — registry with MAX_USERS=2 would return 0 on the third registration
