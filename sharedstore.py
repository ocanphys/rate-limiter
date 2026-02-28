import logging
import redis
import fakeredis

logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self, host: str = 'localhost', port: int = 6379, db: int = 0):
        try:
            client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
            client.ping()  # raises ConnectionError if server is unreachable
            self._client = client
            logger.info("connected to Redis at %s:%d", host, port)
        except (redis.ConnectionError, redis.TimeoutError):
            logger.warning("could not connect to Redis at %s:%d — falling back to fakeredis", host, port)
            self._client = fakeredis.FakeRedis(decode_responses=True)

    def set_value(self, key: str, value: str, ttl: int = None):
        """Add a key-value pair. Optionally set TTL in seconds."""
        self._client.set(key, value, ex=ttl)

    def get_value(self, key: str):
        """Retrieve a value by key. Returns None if not found."""
        return self._client.get(key)

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
