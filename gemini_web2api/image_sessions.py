"""In-memory continuation state for explicitly grouped image generations."""

import threading
import time
from collections import OrderedDict
from contextlib import contextmanager


class ImageConversationSessions:
    def __init__(self, ttl_seconds=3600, max_sessions=256):
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._entries = OrderedDict()
        self._guard = threading.RLock()

    def _prune(self, now):
        expired = [
            key
            for key, entry in self._entries.items()
            if entry["active"] == 0 and now - entry["updated"] >= self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) > self.max_sessions:
            removable = next(
                (key for key, entry in self._entries.items() if entry["active"] == 0),
                None,
            )
            if removable is None:
                break
            self._entries.pop(removable, None)

    @contextmanager
    def use(self, key):
        now = time.monotonic()
        with self._guard:
            self._prune(now)
            entry = self._entries.get(key)
            if entry is None:
                entry = {"context": None, "updated": now, "active": 0, "lock": threading.Lock()}
                self._entries[key] = entry
            entry["active"] += 1
            self._entries.move_to_end(key)

        entry["lock"].acquire()
        try:
            yield entry
        finally:
            entry["lock"].release()
            with self._guard:
                entry["active"] -= 1
                entry["updated"] = time.monotonic()
                self._prune(entry["updated"])


IMAGE_CONVERSATIONS = ImageConversationSessions()
