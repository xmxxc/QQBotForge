"""Compatibility shim for the removed scheduled updater.

Updates are initiated explicitly from the dashboard or the owner menu. The
class remains importable for older extensions, but ``start`` never creates a
timer and ``stop`` is a no-op.
"""


class AutoUpdateService:
    def __init__(self, base_dir=None, app=None):
        self._base_dir = base_dir
        self._app = app
        self._task = None

    def start(self):
        return None

    def stop(self):
        self._task = None
