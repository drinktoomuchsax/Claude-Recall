"""Unit tests for the /ingest dedup cache."""

from __future__ import annotations

import asyncio

import pytest

from claude_recall.message_cache import MessageIdCache


async def test_add_new_returns_true():
    cache = MessageIdCache()
    assert await cache.add("abc") is True


async def test_add_duplicate_returns_false():
    cache = MessageIdCache()
    await cache.add("abc")
    assert await cache.add("abc") is False


async def test_contains_reflects_presence():
    cache = MessageIdCache()
    await cache.add("x")
    assert await cache.contains("x") is True
    assert await cache.contains("y") is False


async def test_max_size_evicts_oldest():
    cache = MessageIdCache(max_size=3)
    for i in range(5):
        await cache.add(f"id-{i}")
    # Only id-2, id-3, id-4 should remain (LRU eviction).
    assert await cache.contains("id-0") is False
    assert await cache.contains("id-1") is False
    assert await cache.contains("id-2") is True
    assert await cache.contains("id-3") is True
    assert await cache.contains("id-4") is True


async def test_ttl_expires_entries(monkeypatch):
    cache = MessageIdCache(ttl_sec=0.01)
    await cache.add("old")
    assert await cache.contains("old")
    await asyncio.sleep(0.02)
    assert await cache.contains("old") is False
    # Expired entry can be added again as "new".
    assert await cache.add("old") is True


async def test_refresh_on_duplicate_hit():
    """A duplicate hit should move the id to the MRU position so it
    survives subsequent evictions longer than entries added before it."""
    cache = MessageIdCache(max_size=3)
    await cache.add("a")
    await cache.add("b")
    await cache.add("c")
    # Refresh 'a' — now 'a' is MRU.
    await cache.add("a")
    # Push 'b' out by adding 'd'.
    await cache.add("d")
    assert await cache.contains("a") is True     # refreshed, safe
    assert await cache.contains("b") is False    # evicted
    assert await cache.contains("c") is True
    assert await cache.contains("d") is True


async def test_size_reports_live_count():
    cache = MessageIdCache(ttl_sec=10.0)
    await cache.add("a")
    await cache.add("b")
    assert await cache.size() == 2
    await cache.add("a")  # duplicate
    assert await cache.size() == 2
