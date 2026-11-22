from __future__ import annotations

import json
import platform
import re
import ssl
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib import error, request


@dataclass(frozen=True)
class LanguageServer:
    pid: int | None
    csrf_token: str
    workspace_id: str
    extension_port: int | None


def _option(command: str, name: str) -> str | None:
    match = re.search(
        rf"(?:^|\s)--{re.escape(name)}(?:=|\s+)(?:\"([^\"]*)\"|'([^']*)'|([^\s]+))",
        command,
        flags=re.IGNORECASE,
    )
    return next((part for part in match.groups() if part is not None), None) if match else None


def _parse_process(pid: int | None, name: str, command: str) -> LanguageServer | None:
    if "language_server" not in f"{name} {command}".lower():
        return None
    token = _option(command, "csrf_token")
    if not token:
        return None
    raw_port = _option(command, "extension_server_port")
    try:
        port = int(raw_port) if raw_port else None
    except ValueError:
        port = None
    return LanguageServer(pid, token, _option(command, "workspace_id") or "", port)


def discover_language_servers() -> list[LanguageServer]:
    if platform.system() == "Windows":
        script = (
            "$ErrorActionPreference='Stop'; $rows=@(Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match '(?i)language.?server' -or "
            "$_.CommandLine -match '(?i)language.?server' } | "
            "Select-Object ProcessId,Name,CommandLine); "
            "ConvertTo-Json -InputObject $rows -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Windows language-server discovery failed: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "PowerShell failed").strip()
            raise RuntimeError(f"Windows language-server discovery failed: {detail}")
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Windows process query returned invalid JSON") from exc
        if isinstance(rows, dict):
            rows = [rows]
        candidates = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        processes = []
        for row in candidates:
            command = str(row.get("CommandLine") or "")
            try:
                pid = int(row.get("ProcessId"))
            except (TypeError, ValueError):
                pid = None
            server = _parse_process(pid, str(row.get("Name") or ""), command)
            if server:
                processes.append(server)
        if not processes:
            if candidates:
                raise RuntimeError("Language server found, but its command line exposed no CSRF token")
            raise RuntimeError("No Antigravity language_server process found; open Antigravity and a workspace")
        return processes

    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Language-server process discovery failed: {exc}") from exc
    processes = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)", line)
        if match:
            server = _parse_process(int(match.group(1)), "", match.group(2))
            if server:
                processes.append(server)
    if not processes:
        raise RuntimeError("No Antigravity language_server process found; open Antigravity and a workspace")
    return processes


def listening_ports(server: LanguageServer) -> list[int]:
    ports: set[int] = set()
    if server.pid is not None and platform.system() == "Windows":
        script = (
            "$ErrorActionPreference='SilentlyContinue'; "
            f"Get-NetTCPConnection -State Listen -OwningProcess {server.pid} | "
            "Select-Object -ExpandProperty LocalPort -Unique | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            values = json.loads(result.stdout or "[]")
            values = values if isinstance(values, list) else [values]
            ports.update(int(value) for value in values if str(value).isdigit())
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
            pass
        if not ports:
            try:
                result = subprocess.run(
                    ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10
                )
                for line in result.stdout.splitlines():
                    columns = line.split()
                    if len(columns) >= 5 and columns[-2].upper() == "LISTENING" and columns[-1] == str(server.pid):
                        match = re.search(r":(\d+)$", columns[1])
                        if match:
                            ports.add(int(match.group(1)))
            except (OSError, subprocess.TimeoutExpired):
                pass
    elif server.pid is not None:
        for argv, pattern in (
            (["lsof", "-Pan", "-p", str(server.pid), "-iTCP", "-sTCP:LISTEN"], r":(\d+)\s+\(LISTEN\)"),
            (["ss", "-ltnp"], r":(\d+)\s"),
        ):
            try:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                continue
            for line in result.stdout.splitlines():
                if argv[0] == "ss" and not re.search(rf"\bpid={server.pid}(?:,|\))", line):
                    continue
                ports.update(int(match.group(1)) for match in re.finditer(pattern, line))
            if ports:
                break
    if server.extension_port:
        ports.add(server.extension_port)
    return sorted(ports)


_SCHEME_CACHE: dict[int, str] = {}


def rpc_call(port: int, csrf_token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    schemes = [_SCHEME_CACHE[port]] if port in _SCHEME_CACHE else []
    schemes.extend(scheme for scheme in ("http", "https") if scheme not in schemes)
    failures: list[str] = []
    for scheme in schemes:
        endpoint = (
            f"{scheme}://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method}"
        )
        req = request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json", "x-codeium-csrf-token": csrf_token},
            method="POST",
        )
        try:
            if scheme == "https":
                with request.urlopen(req, timeout=10, context=ssl._create_unverified_context()) as response:
                    raw = response.read().decode("utf-8")
            else:
                with request.urlopen(req, timeout=10) as response:
                    raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            failures.append(f"{scheme} HTTP {exc.code}")
            continue
        except (error.URLError, TimeoutError, OSError) as exc:
            failures.append(f"{scheme} {type(exc).__name__}")
            continue
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Antigravity RPC returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Antigravity RPC returned a non-object response")
        _SCHEME_CACHE[port] = scheme
        return result
    raise RuntimeError("Antigravity RPC failed: " + ", ".join(failures))


def fetch_trajectory(session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: list[str] = []
    for server in discover_language_servers():
        for port in listening_ports(server):
            try:
                trajectory_result = rpc_call(port, server.csrf_token, "GetCascadeTrajectory", {"cascadeId": session_id})
                trajectory = trajectory_result.get("trajectory", trajectory_result)
                if not isinstance(trajectory, dict) or not trajectory.get("trajectoryId"):
                    continue
                step_result = rpc_call(
                    port,
                    server.csrf_token,
                    "GetCascadeTrajectorySteps",
                    {"cascadeId": session_id, "verbosity": 1},
                )
                steps = step_result.get("steps", [])
                if not isinstance(steps, list):
                    raise RuntimeError("RPC returned invalid steps")
                return trajectory, [step for step in steps if isinstance(step, dict)]
            except (RuntimeError, ValueError) as exc:
                errors.append(f"port {port}: {exc}")
    raise RuntimeError("No live Antigravity trajectory found" + (f" ({'; '.join(errors[:3])})" if errors else ""))