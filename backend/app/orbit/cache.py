import time
from typing import Any, Optional


class TTLCache:
    """Lightweight in-memory cache with time-to-live expiration per key."""

    def __init__(self, default_ttl_seconds: int = 7200):
        self.default_ttl = default_ttl_seconds
        # shortcut: in-memory cache is lost on process restart, migrate to SQLite table when persistent cache is required
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        """Retrieve value if key exists and has not expired."""
        if key in self._store:
            expires_at, val = self._store[key]
            if time.time() < expires_at:
                return val
            # Lazily remove expired entry
            del self._store[key]
        return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store key-value pair with a TTL."""
        ttl_seconds = ttl if ttl is not None else self.default_ttl
        self._store[key] = (time.time() + ttl_seconds, value)

    def contains(self, key: str) -> bool:
        """Check if key exists and is unexpired."""
        return self.get(key) is not None

    def delete(self, key: str) -> None:
        """Remove a key from cache."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Clear all cached entries."""
        self._store.clear()

    def __len__(self) -> int:
        now = time.time()
        expired = [k for k, (exp, _) in self._store.items() if now >= exp]
        for k in expired:
            del self._store[k]
        return len(self._store)
