"""The ``app_based_mcp`` kind. The application is faked at ``AppResolver.probe``; everything else is real."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_cli import mcp_catalog
from hermes_cli.mcp_catalog import CatalogError, _parse_manifest
from hermes_platform.resolver.availability import Availability
from hermes_platform.resolver.base import Probe
from hermes_platform.resolver.core import CheckState, Observation
from tools.connectors import app_based_mcp as kind
from tools.connectors import contract as c
from tools.connectors import operation as op

MANIFEST = textwrap.dedent("""
    manifest_version: 1
    name: thing-mcp
    description: Fronts the Thing desktop app.
    transport: { type: http, url: "http://127.0.0.1:47985/mcp" }
    auth: { type: none }
    suggest: { keywords: [thing], applications: [Thing] }
    app:
      win32:
        presence: executable
        location: "C:/Program Files/Thing/Thing.exe"
        version: { kind: uninstall_registry, display_name_prefix: Thing }
        liveness: { kind: server_json, path: "%LOCALAPPDATA%/Thing/server.json" }
    requires: { app: true, min_version: "2.0" }
""")


def _entry(tmp_path: Path, extra: str = ""):
    p = tmp_path / "thing-mcp" / "manifest.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(MANIFEST + textwrap.dedent(extra), encoding="utf-8")
    return _parse_manifest(p)


# ---- contract and payload ------------------------------------------------------------------


def test_app_based_transition_rows_name_their_actors():
    assert "app_based_mcp" in c.KINDS
    assert c.TargetState.unavailable in c.RESOLVED_STATES
    assert c.allowed("app_based_mcp", c.TargetState.pending, c.TargetState.initiated) == c.Actor.user
    assert c.allowed("app_based_mcp", c.TargetState.pending, c.TargetState.unavailable) == c.Actor.backend_watcher
    assert c.allowed("app_based_mcp", c.TargetState.initiated, c.TargetState.connected) == c.Actor.backend_watcher
    assert c.allowed("app_based_mcp", c.TargetState.initiated, c.TargetState.expired) == c.Actor.clock
    assert c.allowed("app_based_mcp", c.TargetState.pending, c.TargetState.connected) is None


def test_payload_is_kind_checked_and_flattened_into_the_snapshot():
    target = op.Target("thing-mcp", "app_based_mcp", "connect")
    operation = op.ConnectionOperation([target])
    payload = op.AppBasedMcpPayload(kind="app_based_mcp", app="Thing", availability="available",
                                    app_version="2.1", open_supported=True)
    operation.transition("thing-mcp", c.TargetState.initiated, c.Actor.user, payload=payload)
    row = operation.request_payload()["targets"][0]
    assert row["app"] == "Thing" and row["availability"] == "available" and row["open_supported"] is True
    assert "launched_at" not in row and "failure_reason" not in row and "kind" in row
    with pytest.raises(op.IllegalTransition):
        operation.transition("thing-mcp", c.TargetState.connected, c.Actor.backend_watcher,
                             payload=op.McpPayload(kind="mcp"))


def test_mcp_payload_keeps_the_tools_and_discovery_error_keys_the_desktop_reads():
    target = op.Target("linear", "mcp", "install")
    operation = op.ConnectionOperation([target])
    operation.transition("linear", c.TargetState.initiated, c.Actor.backend_watcher)
    operation.transition("linear", c.TargetState.connected, c.Actor.backend_watcher,
                         payload=target.with_payload(tools=["a", "b"], discovery_error="slow"))
    row = operation.request_payload()["targets"][0]
    assert row["tools"] == ["a", "b"] and row["discovery_error"] == "slow"


# ---- the composer ---------------------------------------------------------------------------


@pytest.mark.parametrize("state,journey,expect", [
    ("unsupported_os", None, "only available on Windows"),
    ("missing_app", None, "not installed"),
    ("version_too_old", None, "version 2.0 or newer"),
    ("available", op.AppBasedMcpPayload("app_based_mcp", "Thing", "available", failure_reason="startup_timeout"), "did not answer within"),
    ("available", op.AppBasedMcpPayload("app_based_mcp", "Thing", "available", failure_reason="launch_failed"), "could not be started"),
    ("available", op.AppBasedMcpPayload("app_based_mcp", "Thing", "available", endpoint="http://127.0.0.1:1/mcp"), "is answering"),
    ("available", op.AppBasedMcpPayload("app_based_mcp", "Thing", "available", open_supported=True), "Open it to connect"),
    ("available", op.AppBasedMcpPayload("app_based_mcp", "Thing", "available", open_supported=False), "Start it, then connect"),
])
def test_describe_covers_each_availability_and_journey_state(tmp_path, state, journey, expect):
    entry = _entry(tmp_path)
    avail = Availability(state, version="1.0" if state == "version_too_old" else None, min_version="2.0")
    text = kind.describe(avail, journey, entry)
    assert expect in text and text.startswith("Thing")


# ---- the journey ----------------------------------------------------------------------------


@dataclass
class _FakeApp:
    running: bool = False
    answering: bool = False
    url: str = "http://127.0.0.1:13508/mcp"

    def probe(self, res, *, effort, deadline_s):
        present = Observation(CheckState.PRESENT, True)
        absent = Observation(CheckState.ABSENT, False)
        endpoint = Observation(CheckState.PRESENT, self.url) if self.running else Observation(CheckState.ABSENT)
        return Probe(running=present if self.running else absent,
                     answering=present if self.answering else absent, endpoint=endpoint)

    def locate(self, ctx=None):
        return object()


def _j(target: op.Target) -> op.AppBasedMcpPayload:
    assert isinstance(target.payload, op.AppBasedMcpPayload)
    return target.payload


def _runner(monkeypatch, tmp_path, app: _FakeApp, availability: Availability, extra=""):
    entry = _entry(tmp_path, extra)
    monkeypatch.setattr(kind, "_entry", lambda name: entry)
    monkeypatch.setattr("hermes_platform.host.facts.os_family", lambda: "win32")
    monkeypatch.setattr("hermes_platform.resolver.availability.availability", lambda e, **kw: availability)
    monkeypatch.setattr("hermes_platform.resolver.app.AppResolver", lambda definition: app)
    monkeypatch.setattr(kind, "_register", lambda name: (["thing_tool"], ""))
    runner = kind.Runner()
    target = op.Target("thing-mcp", "app_based_mcp", "connect")
    operation = op.ConnectionOperation([target])
    return runner, operation, target


def test_unsupported_os_settles_unavailable_before_any_card(monkeypatch, tmp_path):
    runner, operation, target = _runner(monkeypatch, tmp_path, _FakeApp(), Availability("unsupported_os"))
    runner.prepare(operation)
    assert target.state is c.TargetState.unavailable and operation.all_resolved
    assert "only available on Windows" in target.detail


def test_an_answering_server_connects_in_prepare_without_a_card(monkeypatch, tmp_path):
    app = _FakeApp(running=True, answering=True)
    runner, operation, target = _runner(monkeypatch, tmp_path, app, Availability("available", version="2.1"))
    runner.prepare(operation)
    assert target.state is c.TargetState.connected
    row = operation.request_payload()["targets"][0]
    assert row["tools"] == ["thing_tool"] and row["endpoint"] == app.url


def test_open_is_recorded_on_the_rpc_thread_and_launched_by_the_tick(monkeypatch, tmp_path):
    app = _FakeApp(running=False)
    runner, operation, target = _runner(monkeypatch, tmp_path, app, Availability("available", version="2.1"),
                                        'launch: { win32: { command: "C:/Program Files/Thing/Thing.exe" } }')
    spawned = []
    monkeypatch.setattr(kind.subprocess, "Popen", lambda args, **kw: spawned.append(args))
    runner.prepare(operation)
    assert target.state is c.TargetState.pending and _j(target).open_supported is True
    assert runner.request_open(operation, target) is None
    assert target.state is c.TargetState.initiated and spawned == []  # the RPC thread launches nothing
    runner.observe(operation)
    assert spawned == [["C:/Program Files/Thing/Thing.exe"]] and _j(target).launched_at is not None
    runner.observe(operation)  # a second tick with the launch already made spawns again never
    assert len(spawned) == 1
    app.running = app.answering = True
    runner.observe(operation)
    assert target.state is c.TargetState.connected


def test_open_without_a_launch_block_is_refused_and_stays_pending(monkeypatch, tmp_path):
    runner, operation, target = _runner(monkeypatch, tmp_path, _FakeApp(), Availability("available", version="2.1"))
    runner.prepare(operation)
    assert _j(target).open_supported is False
    assert "no Open action" in (runner.request_open(operation, target) or "")
    assert target.state is c.TargetState.pending


def test_startup_budget_distinguishes_a_stopped_app_from_an_unreachable_endpoint(monkeypatch, tmp_path):
    app = _FakeApp(running=False)
    runner, operation, target = _runner(monkeypatch, tmp_path, app, Availability("available", version="2.1"),
                                        'launch: { win32: { command: "C:/Program Files/Thing/Thing.exe" } }')
    monkeypatch.setattr(kind.subprocess, "Popen", lambda args, **kw: None)
    runner.prepare(operation)
    runner.request_open(operation, target)
    runner.observe(operation)
    runner.rows["thing-mcp"].launched_at = 0.0  # long past the budget
    runner.observe(operation)
    assert target.state is c.TargetState.failed and _j(target).failure_reason == "startup_timeout"
    app.running = True
    runner.request_open(operation, target)
    runner.rows["thing-mcp"].launched_at = None
    runner.observe(operation)
    runner.rows["thing-mcp"].launched_at = 0.0
    runner.observe(operation)
    assert _j(target).failure_reason == "endpoint_unreachable"


def test_a_failed_launch_reports_launch_failed(monkeypatch, tmp_path):
    runner, operation, target = _runner(monkeypatch, tmp_path, _FakeApp(), Availability("available", version="2.1"),
                                        'launch: { win32: { command: "C:/Program Files/Thing/Thing.exe" } }')

    def boom(args, **kw):
        raise OSError("denied")

    monkeypatch.setattr(kind.subprocess, "Popen", boom)
    runner.prepare(operation)
    runner.request_open(operation, target)
    runner.observe(operation)
    assert target.state is c.TargetState.failed and _j(target).failure_reason == "launch_failed"
    assert "denied" not in target.detail  # the sentence is composed, never the exception text


def test_run_operation_emits_no_card_when_prepare_resolved_every_target(monkeypatch, tmp_path):
    from tools.connectors import live
    from tools.connectors.run import Kind, run_operation

    live.reset_for_tests()
    runner, operation, target = _runner(monkeypatch, tmp_path, _FakeApp(), Availability("unsupported_os"))
    cards = []
    result = run_operation([op.Target("thing-mcp", "app_based_mcp", "connect")],
                           Kind(prepare=runner.prepare, observe=runner.observe, note="n"),
                           session_key="s", tool_call_id=None, connection_callback=cards.append,
                           with_urls_in_result=False)
    assert cards == [] and '"unavailable"' in result and '"settled_by": "all_resolved"' in result
    live.reset_for_tests()


# ---- manifest keys -----------------------------------------------------------------------


def test_launch_needs_a_matching_app_os_and_an_absolute_command(tmp_path):
    with pytest.raises(CatalogError, match="launch.darwin has no matching app.darwin"):
        _entry(tmp_path, 'launch: { darwin: { command: /Applications/Thing.app } }')
    with pytest.raises(CatalogError, match="launch.win32.command must be absolute"):
        _entry(tmp_path, 'launch: { win32: { command: Thing.exe } }')
    entry = _entry(tmp_path, 'launch: { win32: { command: "%ProgramFiles%/Thing/Thing.exe" } }')
    assert entry.launch_for("win32") == "%ProgramFiles%/Thing/Thing.exe" and entry.launch_for("darwin") is None


def test_visibility_defaults_public_and_rejects_other_values(tmp_path):
    assert _entry(tmp_path).visibility == "public"
    assert _entry(tmp_path, "visibility: guest_onboarding").visibility == "guest_onboarding"
    with pytest.raises(CatalogError, match="visibility must be one of"):
        _entry(tmp_path, "visibility: staff")


def test_list_catalog_hides_guest_onboarding_entries_unless_the_gate_is_on(tmp_path, monkeypatch):
    root = tmp_path / "catalog"
    (root / "public-mcp").mkdir(parents=True)
    (root / "gated-mcp").mkdir(parents=True)
    (root / "public-mcp" / "manifest.yaml").write_text(MANIFEST.replace("thing-mcp", "public-mcp"), encoding="utf-8")
    (root / "gated-mcp" / "manifest.yaml").write_text(
        MANIFEST.replace("thing-mcp", "gated-mcp") + "visibility: guest_onboarding\n", encoding="utf-8")
    monkeypatch.setattr(mcp_catalog, "_catalog_root", lambda: root)
    mcp_catalog._CATALOG_CACHE = None
    monkeypatch.setattr("hermes_cli.anon_auth.guest_enabled", lambda: False)
    assert [e.name for e in mcp_catalog.list_catalog()] == ["public-mcp"]
    monkeypatch.setattr("hermes_cli.anon_auth.guest_enabled", lambda: True)
    assert [e.name for e in mcp_catalog.list_catalog()] == ["gated-mcp", "public-mcp"]
    mcp_catalog._CATALOG_CACHE = None


def test_an_app_based_install_records_its_catalog_name(tmp_path):
    entry = _entry(tmp_path)
    cfg = mcp_catalog._build_server_config(entry, None)
    assert cfg["url"] == "http://127.0.0.1:47985/mcp" and cfg["catalog_name"] == "thing-mcp"
    plain = _parse_manifest(_write_plain(tmp_path))
    assert "catalog_name" not in mcp_catalog._build_server_config(plain, None)


def _write_plain(tmp_path: Path) -> Path:
    p = tmp_path / "plain-mcp" / "manifest.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent("""
        manifest_version: 1
        name: plain-mcp
        description: A plain http server.
        transport: { type: http, url: "http://127.0.0.1:9/mcp" }
        auth: { type: none }
    """), encoding="utf-8")
    return p


# ---- the transport reads the endpoint per attempt -------------------------------------------


def test_live_endpoint_comes_from_the_runtime_file_and_is_never_persisted(tmp_path, monkeypatch):
    from tools import mcp_tool_transport as transport

    runtime = tmp_path / "server.json"
    runtime.write_text('{"pid": 1, "http": "http://127.0.0.1:13508/mcp", "token": "s3cret"}', encoding="utf-8")
    p = tmp_path / "thing-mcp" / "manifest.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(MANIFEST.replace("%LOCALAPPDATA%/Thing/server.json", runtime.as_posix()), encoding="utf-8")
    entry = _parse_manifest(p)
    monkeypatch.setattr("hermes_cli.mcp_catalog.get_entry", lambda name, **kw: entry if name == "thing-mcp" else None)
    monkeypatch.setattr("hermes_platform.host.facts.os_family", lambda: "win32")
    config = {"url": "http://127.0.0.1:47985/mcp", "catalog_name": "thing-mcp"}
    url, headers = transport._live_endpoint("thing-mcp", config)
    assert url == "http://127.0.0.1:13508/mcp" and headers == {"Authorization": "Bearer s3cret"}
    assert config == {"url": "http://127.0.0.1:47985/mcp", "catalog_name": "thing-mcp"}
    runtime.write_text('{"pid": 1, "http": "http://127.0.0.1:2222/mcp"}', encoding="utf-8")
    assert transport._live_endpoint("thing-mcp", config) == ("http://127.0.0.1:2222/mcp", {})
    assert transport._live_endpoint("other", {"url": "http://127.0.0.1:1/mcp"}) is None
    # The configured url is never a fallback for a dynamic server: an unusable runtime file is an error.
    runtime.unlink()
    with pytest.raises(transport.LiveEndpointUnavailable):
        transport._live_endpoint("thing-mcp", config)


def test_the_runtime_token_is_registered_for_redaction(tmp_path, monkeypatch):
    from agent import redact
    from tools import mcp_tool_transport as transport

    runtime = tmp_path / "server.json"
    runtime.write_text('{"pid": 1, "http": "http://127.0.0.1:13508/mcp", "token": "tok-9f8e7d"}', encoding="utf-8")
    p = tmp_path / "thing-mcp" / "manifest.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(MANIFEST.replace("%LOCALAPPDATA%/Thing/server.json", runtime.as_posix()), encoding="utf-8")
    entry = _parse_manifest(p)
    monkeypatch.setattr("hermes_cli.mcp_catalog.get_entry", lambda name, **kw: entry)
    monkeypatch.setattr("hermes_platform.host.facts.os_family", lambda: "win32")
    transport._live_endpoint("thing-mcp", {"url": "http://127.0.0.1:1/mcp", "catalog_name": "thing-mcp"})
    assert "tok-9f8e7d" not in redact.redact_sensitive_text("HTTP 401 from POST ... body: Bearer tok-9f8e7d", force=True)
    redact.clear_vault_redaction_values()


def test_a_gated_catalog_name_fails_closed_in_the_registry_gate(monkeypatch, tmp_path):
    from hermes_cli import mcp_config
    from tools import mcp_tool_handlers as handlers

    entry = _entry(tmp_path, "visibility: guest_onboarding")
    servers = {"thing-mcp": {"url": "http://127.0.0.1:47985/mcp", "catalog_name": "thing-mcp"},
               "mine": {"url": "http://127.0.0.1:47985/mcp"}}
    monkeypatch.setattr(mcp_config, "_get_mcp_servers", lambda config=None: servers)

    def get_entry(name, include_hidden=False):
        return entry if include_hidden else None  # the gate is off: the entry is hidden

    monkeypatch.setattr("hermes_cli.mcp_catalog.get_entry", get_entry)
    assert handlers._catalog_app_offerable("thing-mcp") is False
    assert handlers._catalog_app_offerable("mine") is True  # no catalog_name and no visible manifest: the user's own server
