"""Unraid toolset: the gaps beside Homarr (DECISIONS.md, Phase 5 gate 1). Array and pools,
disk health, UPS, parity check, VMs, shares, notifications, mover status. No Docker, no
command execution. Mutations do nothing unless confirm is true."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from manifold.gateway.manifest import Credentials, HealthResult, ToolsetConfig, ToolsetManifest
from manifold.toolsets.unraid import queries as q
from manifold.unraid import UnraidClient, UnraidError, UnraidSchemaError, client_for

log = logging.getLogger(__name__)

MANIFEST = ToolsetManifest(
    key="unraid",
    display_name="Unraid",
    description="Array, pools, disk health, UPS, parity, VMs, shares, notifications and mover "
    "status on the NAS, through the Unraid 7.2 API.",
    kind="native",
    supported_auth=["api_key"],
    settings_schema={
        "type": "object",
        "properties": {
            "server_url": {
                "type": "string",
                "title": "Server URL",
                "description": "Where the Unraid web GUI answers, without a path.",
                "minLength": 8,
                "x-manifold": {"placeholder": "http://192.168.1.10"},
            },
            "verify_tls": {
                "type": "boolean",
                "title": "Verify TLS certificate",
                "description": "Turn off for a self-signed https URL.",
                "default": True,
            },
        },
        "required": ["server_url"],
        "additionalProperties": False,
    },
    example_settings={"server_url": "http://unraid.example.test", "verify_tls": True},
    version="0.1.0",
)

GB = 1024**3


def _gb(value: Any, unit: str = "bytes") -> float | None:
    if value in (None, ""):
        return None
    n = float(value)
    if unit == "kib":
        n *= 1024
    return round(n / GB, 1)


def _disk(d: dict) -> dict:
    return {
        "name": d.get("name"),
        "device": d.get("device"),
        "type": d.get("type"),
        "status": d.get("status"),
        "size_gb": _gb(d.get("size"), "kib"),
        "fs_type": d.get("fsType"),
        "fs_used_gb": _gb(d.get("fsUsed"), "kib"),
        "fs_free_gb": _gb(d.get("fsFree"), "kib"),
        "temp_c": d.get("temp"),
        "spinning": d.get("isSpinning"),
        "errors": d.get("numErrors"),
        "warning_temp_c": d.get("warning"),
        "critical_temp_c": d.get("critical"),
        "colour": d.get("color"),
    }


def _short(exc: Exception) -> str:
    """The provider's own words, without our 'Unraid refused X:' prefix."""
    text = str(exc)
    return text.split(": ", 1)[1] if ": " in text else text


def _confirm(confirm: bool, what: str) -> None:
    if not confirm:
        raise ToolError(
            f"not done: {what} needs confirm=true. Ask the user, then call again with confirm=true."
        )


class _Unraid:
    def __init__(self, config: ToolsetConfig, credentials: Credentials) -> None:
        self.server_url = str(config.settings.get("server_url") or "")
        self.verify_tls = bool(config.settings.get("verify_tls", True))
        self.credentials = credentials
        self._client: UnraidClient | None = None

    @property
    def client(self) -> UnraidClient:
        if self._client is None:
            self._client = client_for(self.credentials, self.server_url, self.verify_tls)
        return self._client

    async def query(self, document: str, variables: dict | None = None, *, context: str) -> dict:
        try:
            return await self.client.query(document, variables, context=context)
        except UnraidError as exc:
            raise ToolError(str(exc)) from None


def build(config: ToolsetConfig, credentials: Credentials) -> MCPServer:
    nas = _Unraid(config, credentials)
    server = MCPServer(
        name="unraid",
        instructions=(
            "The Unraid NAS through its API. Reads are safe. parity_check, vm_control and "
            "archive_notification change things and refuse unless confirm=true: ask the user "
            "before passing it. Docker containers are not here; they are on the Homarr connector."
        ),
    )

    @server.tool()
    async def system_overview() -> dict:
        """One-call summary of the NAS: hostname, uptime, Unraid version, CPU and memory
        load, array state and capacity, and whether the mover or a parity sync is running.

        Returns those as plain fields with sizes in GB. Use it first. For per-disk detail
        use array_status or disk_health.
        """
        data = await nas.query(q.SYSTEM_OVERVIEW, context="read system overview")
        info, metrics, arr, vars_ = data["info"], data["metrics"], data["array"], data["vars"]
        kb = arr["capacity"]["kilobytes"]
        return {
            "hostname": info["os"].get("hostname"),
            "uptime": info["os"].get("uptime"),
            "unraid_version": info["versions"]["core"].get("unraid"),
            "kernel": info["os"].get("kernel"),
            "cpu": {
                "model": info["cpu"].get("brand"),
                "cores": info["cpu"].get("cores"),
                "threads": info["cpu"].get("threads"),
                "load_percent": round(metrics["cpu"]["percentTotal"], 1),
            },
            "memory": {
                "total_gb": _gb(metrics["memory"]["total"]),
                "used_gb": _gb(metrics["memory"]["used"]),
                "available_gb": _gb(metrics["memory"]["available"]),
                "used_percent": round(metrics["memory"]["percentTotal"], 1),
            },
            "array": {
                "state": arr["state"],
                "total_gb": _gb(kb["total"], "kib"),
                "used_gb": _gb(kb["used"], "kib"),
                "free_gb": _gb(kb["free"], "kib"),
            },
            "mover_running": bool(vars_.get("shareMoverActive")),
            "parity_sync_running": bool(vars_.get("mdResync")),
            "md_state": vars_.get("mdState"),
        }

    @server.tool()
    async def array_status() -> dict:
        """The parity array and every pool: state, capacity, parity check status, and each
        disk's role, status, temperature, spin state, errors and filesystem usage.

        Returns parity, data, pools (cache and other pools, including NVMe mirrors) and
        boot as lists of disks, plus parity_check with progress when one is running. Uses
        cached values, so it never spins up a sleeping disk. For SMART detail use
        disk_health.
        """
        data = await nas.query(q.ARRAY_STATUS, context="read array status")
        arr = data["array"]
        kb = arr["capacity"]["kilobytes"]
        pc = arr.get("parityCheckStatus") or {}
        return {
            "state": arr["state"],
            "capacity": {
                "total_gb": _gb(kb["total"], "kib"),
                "used_gb": _gb(kb["used"], "kib"),
                "free_gb": _gb(kb["free"], "kib"),
                "slots": arr["capacity"].get("disks"),
            },
            "parity_check": {
                "status": pc.get("status"),
                "running": pc.get("running"),
                "paused": pc.get("paused"),
                "correcting": pc.get("correcting"),
                "progress_percent": pc.get("progress"),
                "speed": pc.get("speed"),
                "errors": pc.get("errors"),
                "last_run": pc.get("date"),
                "last_duration_s": pc.get("duration"),
            },
            "parity": [_disk(d) for d in arr.get("parities") or []],
            "data": [_disk(d) for d in arr.get("disks") or []],
            "pools": [_disk(d) for d in arr.get("caches") or []],
            "boot": _disk(arr["boot"]) if arr.get("boot") else None,
        }

    @server.tool()
    async def disk_health(spin_up: bool = False) -> dict:
        """Health of every physical disk: temperature, status, spin state, error counts.

        By default returns Unraid's cached values, which never wake a sleeping disk. Pass
        spin_up=true to read fresh SMART data from every disk; that spins up sleeping disks
        and takes several seconds, so only do it when the user asks for a real SMART check.
        Returns disks with role, temp_c, status, errors and, with spin_up, smart_status.
        """
        if not spin_up:
            data = await nas.query(q.DISK_HEALTH_CACHED, context="read cached disk health")
            arr = data["array"]
            disks = [
                {**_disk(d), "role": role}
                for role, key in (("parity", "parities"), ("data", "disks"), ("pool", "caches"))
                for d in arr.get(key) or []
            ]
            return {
                "source": "cached",
                "disks": disks,
                "warnings": [
                    d["name"]
                    for d in disks
                    if d.get("errors")
                    or (
                        d.get("temp_c")
                        and d.get("warning_temp_c")
                        and d["temp_c"] >= d["warning_temp_c"]
                    )
                ],
            }
        data = await nas.query(q.DISK_HEALTH_SMART, context="read SMART disk health")
        disks = [
            {
                "name": d.get("name"),
                "device": d.get("device"),
                "serial": d.get("serialNum"),
                "vendor": d.get("vendor"),
                "size_gb": _gb(d.get("size")),
                "type": d.get("type"),
                "interface": d.get("interfaceType"),
                "temp_c": d.get("temperature"),
                "smart_status": d.get("smartStatus"),
                "spinning": d.get("isSpinning"),
            }
            for d in data.get("disks") or []
        ]
        return {
            "source": "smart",
            "disks": disks,
            "warnings": [d["name"] for d in disks if d.get("smart_status") not in ("OK", None)],
        }

    @server.tool()
    async def ups_status() -> dict:
        """UPS state: charge level, estimated runtime, load and voltages. Read only.

        Returns a list of UPS devices, or an empty list with a note when none is configured.
        """
        try:
            data = await nas.query(q.UPS_STATUS, context="read UPS status")
        except ToolError as exc:
            if "not allowed" in str(exc) or "unreachable" in str(exc):
                raise
            return {"devices": [], "note": f"UPS monitoring is not available: {_short(exc)}"}
        devices = data.get("upsDevices") or []
        return {
            "devices": [
                {
                    "name": u.get("name"),
                    "model": u.get("model"),
                    "status": u.get("status"),
                    "charge_percent": u["battery"].get("chargeLevel"),
                    "runtime_minutes": u["battery"].get("estimatedRuntime"),
                    "battery_health": u["battery"].get("health"),
                    "load_percent": u["power"].get("loadPercentage"),
                    "input_voltage": u["power"].get("inputVoltage"),
                    "output_voltage": u["power"].get("outputVoltage"),
                    "power_w": u["power"].get("currentPower"),
                }
                for u in devices
            ],
            "note": None if devices else "no UPS configured on this server",
        }

    @server.tool()
    async def parity_check(action: str, correct: bool = False, confirm: bool = False) -> dict:
        """Start, pause, resume or cancel a parity check. Does nothing unless confirm is true.

        action: start, pause, resume or cancel. correct=true makes a start write corrections
        to parity, which is the slower and riskier mode; leave it false for a plain check.
        A check runs for hours and slows the array. Ask the user, then call with
        confirm=true. Returns the action taken; check progress with array_status.
        """
        if action not in q.PARITY_CHECK:
            raise ToolError("action must be start, pause, resume or cancel")
        _confirm(confirm, f"parity check {action}")
        variables = {"correct": bool(correct)} if action == "start" else None
        await nas.query(q.PARITY_CHECK[action], variables, context=f"{action} parity check")
        return {
            "action": action,
            "correcting": bool(correct) if action == "start" else None,
            "done": True,
        }

    @server.tool()
    async def list_vms() -> dict:
        """Virtual machines with their state (RUNNING, SHUTOFF, PAUSED and so on). Read only.

        Returns vms as a list of name and state. Use vm_control to change one.
        """
        try:
            data = await nas.query(q.LIST_VMS, context="list VMs")
        except ToolError as exc:
            if "not allowed" in str(exc) or "unreachable" in str(exc):
                raise
            return {"vms": [], "note": f"the VM service is not available: {_short(exc)}"}
        domains = (data.get("vms") or {}).get("domains") or []
        return {"vms": [{"name": d.get("name"), "state": d.get("state")} for d in domains]}

    @server.tool()
    async def vm_control(name: str, action: str, confirm: bool = False) -> dict:
        """Start, stop, pause or resume a VM by name. Does nothing unless confirm is true.

        stop is a shutdown request to the guest. Ask the user, then call with confirm=true.
        Returns the VM name and action. Check the result with list_vms.
        """
        if action not in q.VM_CONTROL:
            raise ToolError("action must be start, stop, pause or resume")
        _confirm(confirm, f"{action} VM {name!r}")
        data = await nas.query(q.LIST_VMS, context="list VMs")
        domains = (data.get("vms") or {}).get("domains") or []
        match = [d for d in domains if (d.get("name") or "").lower() == name.lower()]
        if not match:
            raise ToolError(
                f"no VM named {name!r}. VMs: {', '.join(d.get('name') or '?' for d in domains)}"
            )
        await nas.query(q.VM_CONTROL[action], {"id": match[0]["id"]}, context=f"{action} VM {name}")
        return {"vm": match[0].get("name"), "action": action, "done": True}

    @server.tool()
    async def list_shares() -> dict:
        """User shares with usage, cache setting and disk include or exclude lists. Read only.

        Returns shares with used_gb, free_gb, size_gb, cache, allocator and the include and
        exclude disk lists. Sizes are for the share's content, not the disks.
        """
        data = await nas.query(q.LIST_SHARES, context="list shares")
        return {
            "shares": [
                {
                    "name": s.get("name"),
                    "comment": s.get("comment"),
                    "used_gb": _gb(s.get("used"), "kib"),
                    "free_gb": _gb(s.get("free"), "kib"),
                    "size_gb": _gb(s.get("size"), "kib"),
                    "cache": s.get("cache"),
                    "allocator": s.get("allocator"),
                    "split_level": s.get("splitLevel"),
                    "include": s.get("include") or [],
                    "exclude": s.get("exclude") or [],
                    "encrypted": s.get("luksStatus"),
                }
                for s in data.get("shares") or []
            ]
        }

    @server.tool()
    async def list_notifications(
        type: str = "unread", importance: str | None = None, limit: int = 20
    ) -> dict:
        """Unraid notifications with counts by importance. Read only.

        type: unread (default) or archive. importance: alert, warning or info to filter.
        limit: how many to return, newest first, at most 100. Returns counts for both
        unread and archive, and notifications with id, title, subject, description,
        importance and timestamp. Use archive_notification with an id to clear one.
        """
        if type not in ("unread", "archive"):
            raise ToolError("type must be unread or archive")
        if importance is not None and importance not in ("alert", "warning", "info"):
            raise ToolError("importance must be alert, warning or info")
        limit = max(1, min(int(limit), 100))
        filt: dict[str, Any] = {"type": type.upper(), "offset": 0, "limit": limit}
        if importance:
            filt["importance"] = importance.upper()
        data = await nas.query(q.NOTIFICATIONS, {"filter": filt}, context="list notifications")
        n = data["notifications"]
        return {
            "counts": {"unread": n["overview"]["unread"], "archive": n["overview"]["archive"]},
            "notifications": [
                {
                    "id": x.get("id"),
                    "title": x.get("title"),
                    "subject": x.get("subject"),
                    "description": x.get("description"),
                    "importance": x.get("importance"),
                    "timestamp": x.get("timestamp"),
                }
                for x in n.get("list") or []
            ],
        }

    @server.tool()
    async def archive_notification(id: str, confirm: bool = False) -> dict:
        """Archive one notification by id. Does nothing unless confirm is true.

        Clears it from the unread list. Take the id from list_notifications. Returns the
        id archived.
        """
        _confirm(confirm, f"archive notification {id}")
        data = await nas.query(q.ARCHIVE_NOTIFICATION, {"id": id}, context="archive notification")
        return {"archived": (data.get("archiveNotification") or {}).get("id", id), "done": True}

    @server.tool()
    async def mover_status() -> dict:
        """Whether the mover is running, its schedule, and the array state. Read only.

        The Unraid API offers no way to start or stop the mover, so there is no mover
        control tool; use the web GUI for that. Returns running, schedule (cron), logging
        and array_state.
        """
        data = await nas.query(q.MOVER_STATUS, context="read mover status")
        v = data["vars"]
        return {
            "running": bool(v.get("shareMoverActive")),
            "schedule": v.get("shareMoverSchedule"),
            "logging": bool(v.get("shareMoverLogging")),
            "array_state": data["array"]["state"],
            "md_state": v.get("mdState"),
        }

    return server


async def _forbidden_roots(nas: _Unraid) -> list[str]:
    refused: list[str] = []
    for root, document in q.ROOT_PROBES.items():
        try:
            await nas.client.query(document, context=f"read {root}")
        except UnraidError as exc:
            if exc.reason == "forbidden":
                refused.append(root)
            elif root not in q.OPTIONAL_QUERIES:
                refused.append(f"{root} ({exc})")
    return refused


async def healthcheck(config: ToolsetConfig, credentials: Credentials) -> HealthResult:
    """Touch every read field the tools use in one query, and confirm the write and SMART
    fields exist by introspection, so an Unraid upgrade that renames a field shows here
    with the field name rather than mid-conversation."""
    if credentials.kind != "api_key":
        return HealthResult(status="down", detail="unraid needs an api_key credential")
    nas = _Unraid(config, credentials)
    if not nas.server_url:
        return HealthResult(status="down", detail="server_url setting is empty")
    try:
        await nas.client.query(q.HEALTH_QUERY, context="health query")
    except UnraidSchemaError as exc:
        return HealthResult(status="degraded", detail=str(exc))
    except UnraidError as exc:
        if exc.reason != "forbidden":
            return HealthResult(status="down", detail=str(exc))
        refused = await _forbidden_roots(nas)
        if refused:
            return HealthResult(
                status="degraded",
                detail="the API key cannot read: "
                + ", ".join(refused)
                + ". Grant READ_ANY on the matching resources under Settings, Management "
                "Access, API Keys. Every other root answered.",
            )
        return HealthResult(status="degraded", detail=str(exc))
    notes: list[str] = []
    for root, document in q.OPTIONAL_QUERIES.items():
        try:
            await nas.client.query(document, context=f"read {root}")
        except UnraidSchemaError as exc:
            return HealthResult(status="degraded", detail=str(exc))
        except UnraidError as exc:
            if exc.reason == "forbidden":
                return HealthResult(status="degraded", detail=str(exc))
            notes.append(f"{root} unavailable: {_short(exc)}")
    missing: list[str] = []
    for type_name, fields in q.FIELDS_USED.items():
        try:
            data = await nas.client.query(
                q.INTROSPECT_TYPE, {"name": type_name}, context="introspection"
            )
        except UnraidError:
            return HealthResult(
                status="ok",
                detail=(
                    "read fields verified; introspection unavailable so write and SMART "
                    "fields were not"
                ),
            )
        present = {f["name"] for f in ((data.get("__type") or {}).get("fields") or [])}
        if not present:
            missing.append(f"type {type_name}")
            continue
        missing.extend(f"{type_name}.{f}" for f in fields if f not in present)
    if missing:
        return HealthResult(
            status="degraded",
            detail="the Unraid API is missing "
            + ", ".join(missing)
            + "; this toolset needs updating for this Unraid version",
        )
    return HealthResult(status="ok", detail="; ".join(notes))
