"""A cache failure is never a request failure."""

from __future__ import annotations

import asyncio

import pytest

from inceptools.platform import cache


class BrokenCache:
    async def get(self, key):
        raise OSError("redis down")

    async def set(self, key, value, ttl=0):
        raise OSError("redis down")

    async def delete(self, *keys):
        raise OSError("redis down")

    async def delete_prefix(self, prefix):
        raise OSError("redis down")

    async def ping(self):
        raise OSError("redis down")


def test_keys_are_namespaced():
    assert cache.key("catalog", "product", "7") == "catalog:product:7"
    assert cache.key("catalog", "", "7") == "catalog:7"


async def test_get_or_set_loads_once():
    store = cache.Memory()
    calls: list[int] = []

    async def load() -> dict:
        calls.append(1)
        return {"a": 1}

    assert await cache.get_or_set(store, "k", 60, load) == {"a": 1}
    assert await cache.get_or_set(store, "k", 60, load) == {"a": 1}
    assert len(calls) == 1
    await store.aclose()


async def test_a_load_failure_is_not_cached():
    store = cache.Memory()

    async def failing() -> dict:
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        await cache.get_or_set(store, "k", 60, failing)
    assert await store.get("k") is None
    await store.aclose()


async def test_an_undecodable_value_is_a_miss_and_self_heals():
    """A shape written by an older deploy must not fail forever."""
    store = cache.Memory()
    await store.set("k", b"not json at all")
    assert await cache.get(store, "k", "MISS") == "MISS"
    assert await store.get("k") is None
    await store.aclose()


async def test_a_ttl_expires():
    store = cache.Memory()
    await store.set("k", b"1", ttl=0.01)
    assert await store.get("k") == b"1"
    await asyncio.sleep(0.02)
    assert await store.get("k") is None
    await store.aclose()


async def test_a_non_positive_ttl_never_expires():
    store = cache.Memory()
    await store.set("forever", b"1", ttl=0)
    await asyncio.sleep(0.02)
    assert await store.get("forever") == b"1"
    await store.aclose()


async def test_the_noop_cache_stores_nothing():
    store = cache.Noop()
    await cache.set(store, "k", {"a": 1})
    assert await cache.get(store, "k") is None


async def test_a_nil_cache_is_safe():
    assert await cache.get(None, "k", "default") == "default"
    await cache.set(None, "k", 1)


async def test_an_unencodable_value_is_dropped_not_raised():
    store = cache.Memory()
    # A tuple key is not expressible in JSON, and `default=` cannot rescue it.
    await cache.set(store, "k", {(1, 2): "a"})
    assert await store.get("k") is None
    await store.aclose()


async def test_delete_prefix_only_removes_its_namespace():
    store = cache.Memory()
    await store.set("catalog:a", b"1")
    await store.set("billing:a", b"1")
    await store.delete_prefix("catalog:")
    assert await store.get("catalog:a") is None
    assert await store.get("billing:a") == b"1"
    await store.aclose()
