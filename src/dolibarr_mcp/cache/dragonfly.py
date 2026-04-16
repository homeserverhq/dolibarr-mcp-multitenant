"""DragonflyDB/Redis async cache client for Dolibarr MCP.

DragonflyDB is Redis-compatible but 25x faster and more memory efficient.
This module provides async caching with automatic serialization.
"""

import json
import logging
import hashlib
from typing import Any, Dict, List, Optional, Union
from datetime import datetime

logger = logging.getLogger(__name__)

# Try to import redis async client
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None


class DragonflyCache:
    """Async cache client compatible with DragonflyDB and Redis.

    Features:
    - Automatic JSON serialization/deserialization
    - Key prefixing for namespace isolation
    - TTL support per key
    - Graceful degradation when cache unavailable
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        prefix: str = "dolibarr:",
        default_ttl: int = 300,
        enabled: bool = True,
    ):
        """Initialize cache client.

        Args:
            host: DragonflyDB/Redis host
            port: DragonflyDB/Redis port
            db: Database number
            password: Optional password
            prefix: Key prefix for namespace isolation
            default_ttl: Default TTL in seconds
            enabled: Whether cache is enabled
        """
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.prefix = prefix
        self.default_ttl = default_ttl
        self.enabled = enabled and REDIS_AVAILABLE
        self._client: Optional[redis.Redis] = None
        self._connected = False

        # Stats
        self._hits = 0
        self._misses = 0
        self._errors = 0

    async def connect(self) -> bool:
        """Connect to DragonflyDB/Redis.

        Returns:
            True if connected successfully
        """
        if not self.enabled:
            logger.debug("Cache disabled, skipping connection")
            return False

        if not REDIS_AVAILABLE:
            logger.warning("redis package not installed, cache disabled")
            return False

        try:
            self._client = redis.Redis(
                host=self.host,
                port=self.port,
                db=self.db,
                password=self.password,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            # Test connection
            await self._client.ping()
            self._connected = True
            logger.info(f"Connected to cache at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.warning(f"Cache connection failed: {e}")
            self._connected = False
            self._client = None
            return False

    async def disconnect(self) -> None:
        """Disconnect from cache."""
        if self._client:
            await self._client.close()
            self._client = None
            self._connected = False

    async def __aenter__(self) -> "DragonflyCache":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()

    def _make_key(self, key: str) -> str:
        """Create prefixed cache key."""
        return f"{self.prefix}{key}"

    def _hash_args(self, args: Dict[str, Any]) -> str:
        """Create hash from arguments for cache key."""
        # Sort keys for consistent hashing
        sorted_args = json.dumps(args, sort_keys=True, default=str)
        return hashlib.md5(sorted_args.encode()).hexdigest()[:12]

    def make_tool_key(
        self,
        tool_name: str,
        args: Dict[str, Any],
        auth_context: Optional[str] = None
    ) -> str:
        """Create cache key for a tool call.

        For multi-tenancy support, cache keys can include auth context
        to ensure different users get isolated cached results.

        Args:
            tool_name: Name of the tool
            args: Tool arguments
            auth_context: Optional auth token/tenant ID for isolation

        Returns:
            Cache key string
        """
        args_hash = self._hash_args(args)
        if auth_context:
            auth_hash = hashlib.md5(auth_context.encode()).hexdigest()[:8]
            return f"tool:{tool_name}:{auth_hash}:{args_hash}"
        return f"tool:{tool_name}:{args_hash}"

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache.

        Args:
            key: Cache key (without prefix)

        Returns:
            Cached value or None if not found
        """
        if not self._connected:
            return None

        try:
            full_key = self._make_key(key)
            value = await self._client.get(full_key)

            if value is not None:
                self._hits += 1
                return json.loads(value)
            else:
                self._misses += 1
                return None

        except Exception as e:
            self._errors += 1
            logger.debug(f"Cache get error: {e}")
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None
    ) -> bool:
        """Set value in cache.

        Args:
            key: Cache key (without prefix)
            value: Value to cache (will be JSON serialized)
            ttl: TTL in seconds (uses default if None)

        Returns:
            True if set successfully
        """
        if not self._connected:
            return False

        try:
            full_key = self._make_key(key)
            serialized = json.dumps(value, default=str)
            await self._client.setex(
                full_key,
                ttl or self.default_ttl,
                serialized
            )
            return True
        except Exception as e:
            self._errors += 1
            logger.debug(f"Cache set error: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Delete key from cache.

        Args:
            key: Cache key (without prefix)

        Returns:
            True if deleted
        """
        if not self._connected:
            return False

        try:
            full_key = self._make_key(key)
            await self._client.delete(full_key)
            return True
        except Exception as e:
            self._errors += 1
            logger.debug(f"Cache delete error: {e}")
            return False

    async def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all keys matching pattern.

        Args:
            pattern: Pattern to match (e.g., "tool:get_customers:*")

        Returns:
            Number of keys deleted
        """
        if not self._connected:
            return 0

        try:
            full_pattern = self._make_key(pattern)
            keys = []
            async for key in self._client.scan_iter(match=full_pattern):
                keys.append(key)

            if keys:
                await self._client.delete(*keys)
            return len(keys)
        except Exception as e:
            self._errors += 1
            logger.debug(f"Cache invalidate error: {e}")
            return 0

    async def invalidate_entity(self, entity_type: str) -> int:
        """Invalidate all cached data for an entity type.

        Args:
            entity_type: Entity type (e.g., "customers", "products")

        Returns:
            Number of keys deleted
        """
        # Invalidate list and search operations
        patterns = [
            f"tool:get_{entity_type}:*",
            f"tool:search_{entity_type}:*",
            f"tool:get_{entity_type[:-1]}_by_id:*",  # singular form
        ]

        total = 0
        for pattern in patterns:
            total += await self.invalidate_pattern(pattern)
        return total

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dict with hits, misses, errors, and hit rate
        """
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0

        return {
            "connected": self._connected,
            "hits": self._hits,
            "misses": self._misses,
            "errors": self._errors,
            "hit_rate": f"{hit_rate:.1f}%",
            "total_requests": total,
        }


# Global cache instance
_cache_instance: Optional[DragonflyCache] = None


async def get_cache(
    host: Optional[str] = None,
    port: Optional[int] = None,
    enabled: bool = True,
) -> DragonflyCache:
    """Get or create global cache instance.

    Args:
        host: Override default host
        port: Override default port
        enabled: Whether cache should be enabled

    Returns:
        DragonflyCache instance
    """
    global _cache_instance

    if _cache_instance is None:
        import os
        _cache_instance = DragonflyCache(
            host=host or os.getenv("DRAGONFLY_HOST", "localhost"),
            port=port or int(os.getenv("DRAGONFLY_PORT", "6379")),
            password=os.getenv("DRAGONFLY_PASSWORD"),
            enabled=enabled and os.getenv("CACHE_ENABLED", "true").lower() == "true",
        )
        await _cache_instance.connect()

    return _cache_instance
