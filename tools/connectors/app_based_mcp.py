"""The ``app_based_mcp`` connection kind: an MCP server hosted by a desktop application."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tools.connectors.contract import Actor, TargetState
from tools.connectors.operation import AppBasedMcpPayload, ConnectionOperation, IllegalTransition, Target

logger = logging.getLogger(__name__)

# Seconds after Open before an unanswering row fails.
STARTUP_BUDGET_SECONDS = 60.0
PROBE_DEADLINE_SECONDS = 3.0

NOTE = (
    "A connected target's server is registered and its tools are callable now. An unavailable target "
    "cannot be offered on this machine; tell the user why from its detail and do not retry. A failed "
    "target with failure_reason startup_timeout or endpoint_unreachable may work after the user starts "
    "the application themselves; do not ask them to reinstall."
)


def describe(availability: Any, journey: Optional[AppBasedMcpPayload], entry: Any) -> str:
    app = _display_name(entry)
    state = availability.state
    if state == "unsupported_os":
        supported = ", ".join(_OS_LABELS.get(o, o) for o in sorted(entry.app.per_os)) if entry.app else "another operating system"
        return f"{app} is only available on {supported}."
    if state == "missing_app":
        return f"{app} is not installed on this machine."
    if state == "version_too_old":
        return f"{app} {availability.version} is installed; version {availability.min_version} or newer is required."
    if journey is None:
        return f"{app} {availability.version} is installed." if availability.version else f"{app} is installed."
    if journey.failure_reason == "launch_failed":
        return f"{app} could not be started."
    if journey.failure_reason == "startup_timeout":
        return f"{app} was started but its server did not answer within {int(STARTUP_BUDGET_SECONDS)} seconds."
    if journey.failure_reason == "endpoint_unreachable":
        return f"{app} is running but its server did not answer."
    if journey.endpoint:
        return f"{app} is running and its server is answering."
    if journey.launched_at is not None:
        return f"{app} is starting."
    if journey.open_supported:
        return f"{app} is installed but not running. Open it to connect."
    return f"{app} is installed but not running. Start it, then connect."


_OS_LABELS = {"win32": "Windows", "darwin": "macOS", "linux": "Linux"}


def _display_name(entry: Any) -> str:
    labels = getattr(getattr(entry, "suggest", None), "applications", None) or []
    return labels[0] if labels else entry.name


def _journey(target: Target, **changes: Any) -> AppBasedMcpPayload:
    payload = target.with_payload(**changes)
    assert isinstance(payload, AppBasedMcpPayload)
    return payload


@dataclass
class _Row:
    entry: Any
    resolver: Any
    resolution: Any
    availability: Any
    launch_command: Optional[str]
    launched_at: Optional[float] = None
    launch_requested: bool = False
    spawn_error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


class Runner:
    def __init__(self) -> None:
        self.rows: Dict[str, _Row] = {}
        self.operation: Optional[ConnectionOperation] = None

    def prepare(self, operation: ConnectionOperation) -> None:
        from hermes_platform.host import facts
        from hermes_platform.resolver.app import AppResolver
        from hermes_platform.resolver.availability import availability

        self.operation = operation
        osf = facts.os_family()
        for target in operation.targets:
            if target.kind != "app_based_mcp":
                continue
            entry = _entry(target.name)
            avail = availability(entry)
            definition = entry.app_for(osf)
            resolver = AppResolver(definition) if definition is not None else None
            resolution = resolver.locate() if resolver is not None else None
            row = _Row(entry, resolver, resolution, avail, entry.launch_for(osf))
            self.rows[target.name] = row
            payload = AppBasedMcpPayload(
                kind="app_based_mcp", app=_display_name(entry), availability=avail.state,
                app_version=avail.version, min_version=avail.min_version,
                open_supported=row.launch_command is not None,
            )
            if avail.state == "unsupported_os":
                _move(operation, target, TargetState.unavailable, Actor.backend_watcher,
                      detail=describe(avail, None, entry), payload=payload)
                continue
            if avail.state in ("missing_app", "version_too_old"):
                _move(operation, target, TargetState.failed, Actor.backend_watcher,
                      detail=describe(avail, None, entry), payload=payload)
                continue
            target.payload = payload
            target.detail = describe(avail, payload, entry)
            self._observe_one(operation, target, row)

    def observe(self, operation: ConnectionOperation) -> None:
        for target in operation.targets:
            row = self.rows.get(target.name)
            if row is None or operation.settled or target.state not in (TargetState.pending, TargetState.initiated):
                continue
            self._observe_one(operation, target, row)

    def _observe_one(self, operation: ConnectionOperation, target: Target, row: _Row) -> None:
        from hermes_platform.resolver.base import Effort
        from hermes_platform.resolver.core import CheckState

        if row.resolver is None or row.resolution is None:
            return
        probe = row.resolver.probe(row.resolution, effort=Effort.NETWORK, deadline_s=PROBE_DEADLINE_SECONDS)
        running = probe.running.value is True
        answering = probe.answering.state is CheckState.PRESENT and probe.answering.value is True
        endpoint = probe.endpoint.value if probe.endpoint.state is CheckState.PRESENT else None

        if answering:
            if target.state is TargetState.pending:
                _move(operation, target, TargetState.initiated, Actor.user, payload=_journey(target, endpoint=endpoint))
            tools, discovery_error = _register(target.name)
            payload = _journey(target, endpoint=endpoint, tools=tools, failure_reason=None)
            detail = describe(row.availability, payload, row.entry)
            if discovery_error:
                detail = f"{detail} Tool discovery failed: {discovery_error}"
            _move(operation, target, TargetState.connected, Actor.backend_watcher, detail=detail, payload=payload)
            return

        with row.lock:
            wants_launch = row.launch_requested and row.launched_at is None and not running
            row.launch_requested = False
        if wants_launch and target.state is TargetState.initiated:
            self._launch(operation, target, row)
            return

        if target.state is TargetState.initiated and row.launched_at is not None:
            if time.time() - row.launched_at > STARTUP_BUDGET_SECONDS:
                reason = "endpoint_unreachable" if running else "startup_timeout"
                payload = _journey(target, failure_reason=reason, endpoint=endpoint)
                _move(operation, target, TargetState.failed, Actor.backend_watcher,
                      detail=describe(row.availability, payload, row.entry), payload=payload)
            return
        payload = _journey(target, endpoint=endpoint)
        if payload != target.payload:
            target.payload = payload
            operation.refresh(target.name, connect_url=None, detail=describe(row.availability, payload, row.entry),
                              actor=Actor.backend_watcher)

    def _launch(self, operation: ConnectionOperation, target: Target, row: _Row) -> None:
        command = _expand(row.launch_command or "")
        try:
            kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([command], cwd=os.path.dirname(command) or None, **kwargs)  # noqa: S603 - manifest-declared absolute path
        except Exception as exc:
            logger.info("app_based_mcp %s: launch failed: %s", target.name, exc.__class__.__name__)
            payload = _journey(target, failure_reason="launch_failed", launch_requested=False)
            _move(operation, target, TargetState.failed, Actor.backend_watcher,
                  detail=describe(row.availability, payload, row.entry), payload=payload)
            return
        with row.lock:
            row.launched_at = time.time()
        payload = _journey(target, launched_at=row.launched_at, launch_requested=False)
        target.payload = payload
        operation.refresh(target.name, connect_url=None, detail=describe(row.availability, payload, row.entry),
                          actor=Actor.backend_watcher)

    # Runs on the RPC thread; the launch happens on the tool thread's next tick, which is the thread /stop covers.
    def request_open(self, operation: ConnectionOperation, target: Target) -> Optional[str]:
        row = self.rows.get(target.name)
        if row is None or row.launch_command is None:
            return "this application has no Open action; start it yourself, then connect"
        if target.state not in (TargetState.pending, TargetState.failed, TargetState.expired):
            return None
        with row.lock:
            row.launch_requested = True
            row.launched_at = None
        payload = _journey(target, launch_requested=True, launched_at=None, failure_reason=None)
        _move(operation, target, TargetState.initiated, Actor.user, payload=payload,
              detail=describe(row.availability, payload, row.entry))
        return None

    def request_connect(self, operation: ConnectionOperation, target: Target) -> None:
        row = self.rows.get(target.name)
        if row is None or target.state not in (TargetState.pending, TargetState.failed, TargetState.expired):
            return
        _move(operation, target, TargetState.initiated, Actor.user, payload=_journey(target, failure_reason=None))


def _entry(name: str) -> Any:
    from hermes_cli.mcp_catalog import get_entry

    entry = get_entry(name)
    if entry is None or not entry.app_based:
        raise IllegalTransition(f"{name!r} is not an application-hosted catalog entry")
    return entry


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def _move(operation: ConnectionOperation, target: Target, to: TargetState, actor: Actor, **fields: Any) -> bool:
    try:
        operation.transition(target.name, to, actor, **fields)
        return True
    except IllegalTransition:
        if not operation.settled:
            raise
        return False


def _register(name: str) -> tuple[List[str], str]:
    """Register the server in the current profile scope; the transport resolves its live endpoint."""
    from agent.redact import redact_sensitive_text
    from tools.connectors.mcp import _registered_tool_names

    try:
        from tools.mcp_tool_config import _load_mcp_config
        from tools.mcp_tool_discovery import register_mcp_servers

        config = _load_mcp_config().get(name)
        if not isinstance(config, dict):
            raise RuntimeError(f"no committed MCP configuration for '{name}'")
        register_mcp_servers({name: config})
        return _registered_tool_names(name), ""
    except Exception as exc:
        return [], redact_sensitive_text(str(exc) or exc.__class__.__name__, force=True) or "error"
