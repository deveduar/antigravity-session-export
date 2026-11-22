from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import time
from pathlib import Path

from live import discover_language_servers


def _server_pids() -> set[int]:
    try:
        return {server.pid for server in discover_language_servers() if server.pid is not None}
    except RuntimeError:
        return set()


def _launcher() -> str:
    override = os.environ.get("ANTIGRAVITY_LAUNCHER")
    if override:
        return override
    if platform.system() == "Windows":
        roots = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Antigravity",
            Path(os.environ.get("ProgramFiles", "")) / "Antigravity",
            Path("D:/Antigravity"),
        ]
        for root in roots:
            candidate = root / "Antigravity.exe"
            if candidate.is_file():
                return str(candidate)
        for name in ("Antigravity.exe", "antigravity.exe"):
            found = shutil.which(name)
            if found:
                return found
    else:
        for name in ("wrapped-antigravity", "antigravity"):
            found = shutil.which(name)
            if found:
                return found
        for candidate in (
            Path("/mnt/d/Antigravity/Antigravity.exe"),
            Path("/mnt/c/Program Files/Antigravity/Antigravity.exe"),
        ):
            if candidate.is_file():
                return str(candidate)
    raise RuntimeError("Antigravity launcher not found; set ANTIGRAVITY_LAUNCHER")


def _terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


class ManagedLaunch:
    def __init__(self, enabled: bool, timeout: float = 90.0) -> None:
        self.enabled = enabled
        self.timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._pids: set[int] = set()
        self._before: set[int] = set()

    def ensure(self) -> None:
        if not self.enabled or self._process is not None:
            return
        self._before = _server_pids()
        executable = _launcher()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [executable],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        self._pids.add(self._process.pid)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("Antigravity exited before its language server became ready")
            new = _server_pids() - self._before
            if new:
                self._pids.update(new)
                return
            time.sleep(1)
        raise RuntimeError("Antigravity language server did not become ready")

    def close(self) -> None:
        for pid in sorted(self._pids):
            if self._process is not None and pid == self._process.pid:
                if self._process.poll() is None:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
            else:
                _terminate(pid)
        self._pids.clear()
        self._before.clear()
        self._process = None
