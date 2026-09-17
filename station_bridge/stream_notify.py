"""Coalesced thread -> asyncio notifications for the experimental PCM path.

An event is only a wake-up hint; the sequence-addressed ring is the source of
truth. Clear the event BEFORE checking the ring, then wait when no data exists.
No thread-pool task or asyncio polling timer is allocated per audio packet.
"""
from __future__ import annotations
import asyncio
import threading
from .live_stream import FrameRing


class RingSubscription:
    def __init__(self, ring: 'NotifiedFrameRing'):
        self.ring = ring
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()
        self._lock = threading.Lock()
        self._scheduled = False
        self._closed = False

    def notify(self) -> None:
        with self._lock:
            if self._closed or self._scheduled:
                return
            self._scheduled = True
        try:
            self.loop.call_soon_threadsafe(self._deliver)
        except RuntimeError:  # loop closed during shutdown
            with self._lock:
                self._closed = True
                self._scheduled = False

    def _deliver(self) -> None:
        with self._lock:
            self._scheduled = False
            closed = self._closed
        if not closed:
            self.event.set()

    def clear(self) -> None:
        self.event.clear()

    async def wait(self, timeout: float = 1.0) -> None:
        async with asyncio.timeout(timeout):
            await self.event.wait()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.ring.unsubscribe(self)


class NotifiedFrameRing(FrameRing):
    """The normal ring's framing and bounds, with optional coalesced wake-ups."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._listeners: set[RingSubscription] = set()
        self._listeners_lock = threading.Lock()

    def subscribe(self) -> RingSubscription:
        subscription = RingSubscription(self)
        with self._listeners_lock:
            self._listeners.add(subscription)
        return subscription

    def unsubscribe(self, subscription: RingSubscription) -> None:
        with self._listeners_lock:
            self._listeners.discard(subscription)

    def append(self, frame: bytes) -> None:
        super().append(frame)
        self.wake()

    def append_packet(self, data: bytes) -> None:
        super().append_packet(data)
        self.wake()

    def wake(self) -> None:
        with self._listeners_lock:
            listeners = tuple(self._listeners)
        for subscription in listeners:
            subscription.notify()

    @property
    def subscriber_count(self) -> int:
        with self._listeners_lock:
            return len(self._listeners)
