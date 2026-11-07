"""Non-blocking, cached hardware status for the launcher's status bar.

NetworkManager and bluetoothctl are subprocesses with multi-second timeouts.
They must never be called by a renderer: one slow adapter would otherwise make
the entire shell feel frozen.  This monitor owns one daemon worker and exposes
only small, lock-protected snapshots to the UI thread.
"""

import threading
from typing import Any, Callable, Dict, Optional


Status = Optional[Dict[str, Any]]


class SystemStatusMonitor:
    """Periodically refreshes WiFi and Bluetooth state off the UI thread."""

    def __init__(self, wifi_provider: Callable[[], Status],
                 bluetooth_provider: Callable[[], Status],
                 interval: float = 5.0):
        self.wifi_provider = wifi_provider
        self.bluetooth_provider = bluetooth_provider
        self.interval = max(1.0, float(interval))
        self._snapshot: Dict[str, Status] = {"wifi": None, "bluetooth": None}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _copy(value: Status) -> Status:
        return dict(value) if isinstance(value, dict) else None

    def snapshot(self) -> Dict[str, Status]:
        """Returns a consistent, disconnected copy; never runs a subprocess."""
        with self._lock:
            return {name: self._copy(value)
                    for name, value in self._snapshot.items()}

    def refresh(self) -> None:
        """Refreshes both providers. Safe to call directly in deterministic tests."""
        values: Dict[str, Status] = {}
        for name, provider in (("wifi", self.wifi_provider),
                               ("bluetooth", self.bluetooth_provider)):
            try:
                values[name] = self._copy(provider())
            except Exception:
                # An unavailable adapter is deliberately indistinguishable from
                # a disconnected one in the status bar: both draw no icon.
                values[name] = None
        with self._lock:
            self._snapshot = values

    def request_refresh(self) -> None:
        """Coalesces refresh requests without blocking the caller."""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="gba-system-status")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            # Clear before doing work: a request arriving during a slow probe
            # remains queued and causes one immediate follow-up, not a storm.
            self._wake.clear()
            self.refresh()
            self._wake.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
