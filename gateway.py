import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bucket import Bucket
from config import MASTER_MAX_TOKENS, MAX_USERS, REDIS_DB, REDIS_HOST, REDIS_PORT, REFILL_INTERVAL, USER_MAX_TOKENS
from functions import RedisClient
from refill import start_background_thread
from registry import BucketRegistry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = RedisClient(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    master = Bucket(id='master', max_tokens=MASTER_MAX_TOKENS, client=client)
    registry = BucketRegistry(client, master_key=master._key)
    app.state.client = client
    app.state.registry = registry
    app.state.master = master

    start_background_thread(master, registry, client)

    logger.info("gateway started — master_max_tokens: %d | user_max_tokens: %d | refill_interval: %ds", MASTER_MAX_TOKENS, USER_MAX_TOKENS, REFILL_INTERVAL)
    yield
    logger.info("gateway shutting down")


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def handle(request: Request):
    ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
    bucket_key = f"bucket:{ip}"
    client: RedisClient = request.app.state.client
    registry: BucketRegistry = request.app.state.registry

    result = registry.register(ip, bucket_key)

    if result == 0:
        logger.info("%s rejected — user cap reached", ip)
        return JSONResponse(status_code=503, content={"detail": "server at capacity"})

    if result == -2:
        logger.info("%s rejected — master bucket depleted", ip)
        return JSONResponse(status_code=503, content={"detail": "server at capacity"})

    allowed = client.eval_script(Bucket._CONSUME_SCRIPT, [bucket_key], [1]) == 1

    if not allowed:
        logger.info("%s rate limited — bucket empty", ip)
        return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})

    master: Bucket = request.app.state.master
    all_buckets = registry.get_all()
    per_user = {
        user_id: USER_MAX_TOKENS - int(client.get_value(bkey) or 0)
        for user_id, bkey in all_buckets.items()
    }
    master_remaining = int(client.get_value(master._key) or 0)

    logger.info("%s allowed", ip)
    return JSONResponse(status_code=200, content={
        "message": "OK",
        "master": {
            "tokens_remaining": master_remaining,
            "tokens_used": MASTER_MAX_TOKENS - master_remaining,
            "max": MASTER_MAX_TOKENS,
        },
        "users": {
            "active": len(all_buckets),
            "max": MAX_USERS,
            "total_requests": sum(per_user.values()),
            "per_ip": {
                user_id: {"requests": used, "tokens_remaining": USER_MAX_TOKENS - used, "max": USER_MAX_TOKENS}
                for user_id, used in per_user.items()
            },
        },
    })


if __name__ == '__main__':
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    uvicorn.run(app, host='0.0.0.0', port=args.port)
