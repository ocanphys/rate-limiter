import time
import redis
import fakeredis


class RedisClient:
    def __init__(self, host: str = 'localhost', port: int = 6379, db: int = 0):
        try:
            client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
            client.ping()  # raises ConnectionError if server is unreachable
            self._client = client
            print(f"Connected to Redis at {host}:{port}")
        except (redis.ConnectionError, redis.TimeoutError):
            print(f"WARNING: Could not connect to Redis at {host}:{port}. Falling back to fakeredis.")
            self._client = fakeredis.FakeRedis(decode_responses=True)

    def set_value(self, key: str, value: str, ttl: int = None):
        """Add a key-value pair. Optionally set TTL in seconds."""
        self._client.set(key, value, ex=ttl)

    def get_value(self, key: str):
        """Retrieve a value by key. Returns None if not found."""
        return self._client.get(key)

    def delete_value(self, key: str):
        """Remove a key-value pair."""
        self._client.delete(key)

    def get_ttl(self, key: str):
        """Returns remaining TTL in seconds. -1 = no expiry, -2 = key doesn't exist."""
        return self._client.ttl(key)

    def incr(self, key: str) -> int:
        """Atomically increment a key by 1. Returns new value."""
        return self._client.incr(key)

    def decr(self, key: str) -> int:
        """Atomically decrement a key by 1. Returns new value."""
        return self._client.decr(key)

    def hset(self, hash_key: str, field: str, value: str):
        """Set a field in a Redis Hash."""
        self._client.hset(hash_key, field, value)

    def hdel(self, hash_key: str, field: str):
        """Delete a field from a Redis Hash."""
        self._client.hdel(hash_key, field)

    def hget(self, hash_key: str, field: str):
        """Get a field value from a Redis Hash. Returns None if not found."""
        return self._client.hget(hash_key, field)

    def hgetall(self, hash_key: str) -> dict:
        """Get all field-value pairs from a Redis Hash as a dict."""
        return self._client.hgetall(hash_key)

    def eval_script(self, script: str, keys: list, args: list):
        """Execute a Lua script atomically. keys and args are passed as KEYS and ARGV."""
        return self._client.eval(script, len(keys), *keys, *args)


# --- Example usage ---
if __name__ == '__main__':
    r = RedisClient(host='localhost', port=6379)

    r.set_value('username', 'alice')
    print(r.get_value('username'))   # alice

    r.delete_value('username')
    print(r.get_value('username'))   # None

    # --- TTL expiry test ---
    r.set_value('temp', 'expires_soon', ttl=2)
    print(r.get_value('temp'))       # expires_soon
    time.sleep(3)
    print(r.get_value('temp'))       # None (expired)

    # --- TTL remaining test ---
    r.set_value('countdown', 'tick', ttl=10)
    print(r.get_value('countdown'))
    print(r.get_ttl('countdown'))    # ~10 (seconds remaining)
    time.sleep(3)
    print(r.get_value('countdown'))
    print(r.get_ttl('countdown'))    # ~7 (seconds remaining)
    time.sleep(8)
    print(r.get_value('countdown'))
    print(r.get_ttl('countdown'))    # -2 (expired, key gone)
