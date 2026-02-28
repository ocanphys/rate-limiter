# Redis connection
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0

# Token bucket parameters
USER_MAX_TOKENS = 200  # max tokens per user bucket
MASTER_MAX_TOKENS = 1000  # total tokens available per refill cycle
# controls global throughput: max fully-refillable users = MASTER_MAX_TOKENS // USER_MAX_TOKENS

# Refill process
REFILL_INTERVAL = 5  # seconds between refill cycles

# User cap
MAX_USERS = 50  # maximum number of concurrent active users
