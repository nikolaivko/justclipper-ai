"""One process-wide reservation for source acquisition and pipeline work."""

import threading


class ProcessingBusy(Exception):
    pass


class HeavyGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None

    def claim(self, owner: str) -> None:
        with self._lock:
            if self._owner is not None and self._owner != owner:
                raise ProcessingBusy("PROCESSING_BUSY")
            self._owner = owner

    def release(self, owner: str) -> None:
        with self._lock:
            if self._owner == owner:
                self._owner = None

    def occupied_by_other(self, owner: str) -> bool:
        with self._lock:
            return self._owner is not None and self._owner != owner


heavy_gate = HeavyGate()
