"""Redis client singleton."""

import redis


_client = None


def get_redis(config: dict = None) -> redis.Redis:
    """Get or create Redis client singleton."""
    global _client
    if _client is None:
        cfg = config or {"host": "localhost", "port": 6379, "db": 0}
        _client = redis.Redis(
            host=cfg.get("host", "localhost"),
            port=cfg.get("port", 6379),
            db=cfg.get("db", 0),
            decode_responses=True,
            socket_keepalive=True,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
    return _client
