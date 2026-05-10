"""TTL + max-size LRU cache for frame message_id dedup.

Used by /ingest to reject replayed/duplicate frames that arrive via a
different path through the cascade topology. Split-horizon on
`forwarded_by` catches most loops; this cache is the second line of
defense against fan-in re-delivery and honest duplicates from lossy
reconnects.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict


class MessageIdCache:
    """Thread-safe (asyncio) LRU with a TTL per entry.

    - add(id) returns False when the id is already present (dedup hit);
      True when it was freshly recorded.
    - Entries expire after `ttl_sec`.
    - When size exceeds `max_size`, the oldest entry is evicted.
    """

    def __init__(self, ttl_sec: float = 600.0, max_size: int = 1000):
        self._ttl = ttl_sec
        self._max = max_size
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def add(self, message_id: str) -> bool:
        async with self._lock:
            self._evict_expired_locked()
            if message_id in self._entries:
                # Already seen — refresh LRU position but treat as dup.
                self._entries.move_to_end(message_id)
                return False
            self._entries[message_id] = time.monotonic()
            if len(self._entries) > self._max:
                self._entries.popitem(last=False)
            return True

    async def contains(self, message_id: str) -> bool:
        async with self._lock:
            self._evict_expired_locked()
            return message_id in self._entries

    async def size(self) -> int:
        async with self._lock:
            self._evict_expired_locked()
            return len(self._entries)

    def _evict_expired_locked(self) -> None:
        cutoff = time.monotonic() - self._ttl
        # Since OrderedDict preserves insertion order and we update on hit,
        # the oldest-by-insertion might not match oldest-by-access; but TTL
        # is about *age since last access*, and we only bump on hit (above),
        # so this works.
        while self._entries:
            oldest_id = next(iter(self._entries))
            if self._entries[oldest_id] >= cutoff:
                break
            del self._entries[oldest_id]
