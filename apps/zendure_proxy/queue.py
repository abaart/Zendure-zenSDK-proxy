"""
RequestQueue: coalesces concurrent GET requests and deduplicates POST requests.

GET coalescing
  Every GET that arrives while the worker is processing a previous batch
  receives the *same* response from a single device query, instead of
  triggering N separate upstream fetches.

POST deduplication
  If multiple POSTs with identical property key-sets are waiting, only the
  most recently received one is sent to the devices.  Earlier duplicates
  receive an immediate synthetic {"ack":"pong"} so HA is never blocked.
"""

from __future__ import annotations

import asyncio


# Type alias for the processed POST list returned by drain()
# Each item: (payload, future_to_resolve, list_of_skipped_futures)
PostGroup = tuple[dict, asyncio.Future, list[asyncio.Future]]


class RequestQueue:
    def __init__(self):
        self._pending_gets: list[asyncio.Future] = []
        self._pending_posts: list[tuple[dict, asyncio.Future]] = []
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()

    async def enqueue_get(self) -> asyncio.Future:
        """Add a GET request; return the Future the caller should await."""
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        async with self._lock:
            self._pending_gets.append(fut)
            self._event.set()
        return fut

    async def enqueue_post(self, payload: dict) -> asyncio.Future:
        """Add a POST request; return the Future the caller should await."""
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        async with self._lock:
            self._pending_posts.append((payload, fut))
            self._event.set()
        return fut

    async def drain(self) -> tuple[list[asyncio.Future], list[PostGroup]]:
        """
        Block until at least one request is pending, then atomically drain
        both queues.

        Returns:
          gets  – futures waiting for a GET response (resolve all with one result)
          posts – deduplicated POST groups; for each group the caller should
                  immediately resolve the skipped futures with {"ack":"pong"}
                  and execute only the latest payload.
        """
        await self._event.wait()
        async with self._lock:
            self._event.clear()
            gets = list(self._pending_gets)
            self._pending_gets.clear()
            posts_raw = list(self._pending_posts)
            self._pending_posts.clear()

        return gets, _dedup_posts(posts_raw)


def _dedup_posts(posts: list[tuple[dict, asyncio.Future]]) -> list[PostGroup]:
    """
    Group POSTs by their property key-set.  Within each group keep only the
    last (most recently received) entry; the rest are "skipped".
    """
    groups: dict[frozenset, list[tuple[dict, asyncio.Future]]] = {}
    for payload, fut in posts:
        keys = frozenset((payload.get("properties") or {}).keys())
        groups.setdefault(keys, []).append((payload, fut))

    result: list[PostGroup] = []
    for group in groups.values():
        skipped = [fut for _, fut in group[:-1]]
        latest_payload, latest_fut = group[-1]
        result.append((latest_payload, latest_fut, skipped))
    return result
