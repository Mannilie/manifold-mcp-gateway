"""A fake Unraid GraphQL API. Answers by root field, checks the API key, and can be told
to 'rename' a field (returns the API's Cannot query field error) or forbid a root."""

from __future__ import annotations

import re

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

API_KEY = "unraid-key-123"

SCHEMA_FIELDS = {
    "Disk": [
        "id",
        "name",
        "device",
        "serialNum",
        "vendor",
        "size",
        "type",
        "interfaceType",
        "temperature",
        "smartStatus",
        "isSpinning",
    ],
    "ParityCheckMutations": ["start", "pause", "resume", "cancel"],
    "VmMutations": ["start", "stop", "pause", "resume"],
    "Mutation": ["archiveNotification", "parityCheck", "vm"],
}


def _disk(
    idx, name, device, dtype, size_kib, temp, status="DISK_OK", spinning=True, errors=0, fs=None
):
    return {
        "id": f"disk:{idx}",
        "idx": idx,
        "name": name,
        "device": device,
        "size": size_kib,
        "status": status,
        "type": dtype,
        "temp": temp,
        "isSpinning": spinning,
        "numErrors": errors,
        "numReads": 10,
        "numWrites": 5,
        "fsSize": fs and fs[0],
        "fsFree": fs and fs[1],
        "fsUsed": fs and (fs[0] - fs[1]),
        "fsType": fs and "xfs",
        "warning": 45,
        "critical": 55,
        "rotational": dtype != "CACHE",
        "color": "green-on",
    }


class FakeUnraid:
    def __init__(self) -> None:
        self.renamed: set[str] = set()
        self.forbidden: set[str] = set()
        self.unavailable: set[str] = set()
        self.mutations: list[tuple[str, dict]] = []
        self.calls = 0
        self.introspection = True
        self.notifications = [
            {
                "id": "n:1",
                "title": "Array",
                "subject": "Parity check finished",
                "description": "0 errors",
                "importance": "INFO",
                "timestamp": "2026-09-13T10:00:00Z",
                "type": "UNREAD",
            },
            {
                "id": "n:2",
                "title": "Disk",
                "subject": "Disk 2 hot",
                "description": "48C",
                "importance": "WARNING",
                "timestamp": "2026-09-13T11:00:00Z",
                "type": "UNREAD",
            },
        ]
        self.vms = [
            {"id": "vm:1", "name": "HomeAssistant", "state": "RUNNING"},
            {"id": "vm:2", "name": "Win11", "state": "SHUTOFF"},
        ]
        self.app = Starlette(routes=[Route("/graphql", self.graphql, methods=["POST"])])

    async def graphql(self, request: Request):
        self.calls += 1
        if request.headers.get("x-api-key") != API_KEY:
            return JSONResponse(
                {
                    "errors": [
                        {"message": "Unauthorized", "extensions": {"code": "UNAUTHENTICATED"}}
                    ]
                },
                401,
            )
        body = await request.json()
        query, variables = body.get("query", ""), body.get("variables") or {}
        for field in self.renamed:
            if re.search(rf"\b{re.escape(field)}\b", query):
                return JSONResponse(
                    {"errors": [{"message": f'Cannot query field "{field}" on type "ArrayDisk".'}]}
                )
        if "__type" in query:
            if not self.introspection:
                return JSONResponse(
                    {"errors": [{"message": "GraphQL introspection is not allowed"}]}
                )
            name = variables.get("name")
            fields = SCHEMA_FIELDS.get(name)
            return JSONResponse(
                {
                    "data": {
                        "__type": None
                        if fields is None
                        else {
                            "name": name,
                            "fields": [{"name": f} for f in fields if f not in self.renamed],
                        }
                    }
                }
            )
        if query.lstrip().startswith("mutation"):
            return self._mutation(query, variables)
        data: dict = {}
        for root in (
            "info",
            "metrics",
            "array",
            "vars",
            "vms",
            "shares",
            "notifications",
            "upsDevices",
            "disks",
        ):
            if re.search(rf"\b{root}\b", query):
                if root in self.forbidden:
                    return JSONResponse(
                        {
                            "errors": [
                                {
                                    "message": f"Forbidden: missing READ_ANY on {root.upper()}",
                                    "extensions": {"code": "FORBIDDEN"},
                                }
                            ]
                        }
                    )
                if root in self.unavailable:
                    return JSONResponse(
                        {"errors": [{"message": f"{root} service is not enabled on this server"}]}
                    )
                data[root] = getattr(self, f"_{root}")(variables)
        return JSONResponse({"data": data})

    def _mutation(self, query, variables):
        for root in ("parityCheck", "vm", "archiveNotification"):
            if root in query:
                if root in self.forbidden:
                    return JSONResponse(
                        {
                            "errors": [
                                {
                                    "message": f"Forbidden: missing UPDATE_ANY on {root.upper()}",
                                    "extensions": {"code": "FORBIDDEN"},
                                }
                            ]
                        }
                    )
                action = re.search(r"\{\s*(start|stop|pause|resume|cancel)\b", query)
                self.mutations.append((f"{root}.{action.group(1)}" if action else root, variables))
                if root == "archiveNotification":
                    return JSONResponse(
                        {
                            "data": {
                                "archiveNotification": {"id": variables["id"], "type": "ARCHIVE"}
                            }
                        }
                    )
                return JSONResponse({"data": {root: {action.group(1): True}}})
        return JSONResponse({"errors": [{"message": "unknown mutation"}]})

    def _info(self, _):
        return {
            "os": {
                "hostname": "manny-nas",
                "uptime": "3 days",
                "kernel": "6.12.0",
                "release": "7.2.0",
            },
            "cpu": {"brand": "AMD Ryzen 7", "cores": 8, "threads": 16},
            "versions": {"core": {"unraid": "7.2.0", "api": "4.20.0", "kernel": "6.12.0"}},
        }

    def _metrics(self, _):
        return {
            "cpu": {"percentTotal": 12.34},
            "memory": {
                "total": 64 * 1024**3,
                "used": 20 * 1024**3,
                "free": 30 * 1024**3,
                "available": 40 * 1024**3,
                "percentTotal": 31.25,
            },
        }

    def _array(self, _):
        return {
            "state": "STARTED",
            "capacity": {
                "kilobytes": {"free": "20000000000", "used": "10000000000", "total": "30000000000"},
                "disks": {"free": "2", "used": "4", "total": "6"},
            },
            "parityCheckStatus": {
                "status": "COMPLETED",
                "progress": 100,
                "speed": "180 MB/s",
                "errors": 0,
                "date": "2026-09-01T02:00:00Z",
                "duration": 36000,
                "running": False,
                "paused": False,
                "correcting": False,
            },
            "parities": [_disk(0, "parity", "sda", "PARITY", 16_000_000_000, 34)],
            "disks": [
                _disk(
                    1,
                    "disk1",
                    "sdb",
                    "DATA",
                    16_000_000_000,
                    36,
                    fs=(15_000_000_000, 5_000_000_000),
                ),
                _disk(
                    2,
                    "disk2",
                    "sdc",
                    "DATA",
                    16_000_000_000,
                    48,
                    spinning=True,
                    errors=3,
                    fs=(15_000_000_000, 1_000_000_000),
                ),
            ],
            "caches": [
                _disk(
                    30,
                    "cache",
                    "nvme0n1",
                    "CACHE",
                    2_000_000_000,
                    41,
                    fs=(1_900_000_000, 900_000_000),
                ),
                _disk(
                    31,
                    "cache2",
                    "nvme1n1",
                    "CACHE",
                    2_000_000_000,
                    40,
                    fs=(1_900_000_000, 900_000_000),
                ),
            ],
            "boot": _disk(99, "flash", "sdz", "FLASH", 32_000_000, None, spinning=None),
        }

    def _vars(self, _):
        return {
            "shareMoverActive": False,
            "shareMoverSchedule": "0 3 * * *",
            "shareMoverLogging": True,
            "mdResync": 0,
            "mdResyncPos": 0,
            "mdResyncSize": 0,
            "mdState": "STARTED",
        }

    def _vms(self, _):
        return {"domains": self.vms}

    def _shares(self, _):
        return [
            {
                "id": "share:1",
                "name": "media",
                "comment": "Films",
                "free": "9000000000",
                "used": "1000000000",
                "size": "10000000000",
                "cache": True,
                "include": ["disk1"],
                "exclude": [],
                "allocator": "highwater",
                "splitLevel": "1",
                "floor": "0",
                "luksStatus": "Unencrypted",
            }
        ]

    def _notifications(self, variables):
        filt = variables.get("filter") or {}
        items = [n for n in self.notifications if n["type"] == filt.get("type", "UNREAD")]
        if filt.get("importance"):
            items = [n for n in items if n["importance"] == filt["importance"]]
        overview = {
            "unread": {"info": 1, "warning": 1, "alert": 0, "total": 2},
            "archive": {"info": 0, "warning": 0, "alert": 0, "total": 0},
        }
        return {"overview": overview, "list": items[: filt.get("limit", 20)]}

    def _upsDevices(self, _):
        return [
            {
                "id": "ups:1",
                "name": "Eaton",
                "model": "5E 1500",
                "status": "ONLINE",
                "battery": {"chargeLevel": 100, "estimatedRuntime": 42, "health": "GOOD"},
                "power": {
                    "inputVoltage": 240.0,
                    "outputVoltage": 240.0,
                    "loadPercentage": 22,
                    "nominalPower": 900,
                    "currentPower": 198.0,
                },
            }
        ]

    def _disks(self, _):
        return [
            {
                "id": "disk:1",
                "name": "disk1",
                "device": "sdb",
                "serialNum": "WD1",
                "vendor": "WDC",
                "size": 16 * 1024**4,
                "type": "HD",
                "interfaceType": "SATA",
                "temperature": 36.0,
                "smartStatus": "OK",
                "isSpinning": True,
            },
            {
                "id": "disk:2",
                "name": "disk2",
                "device": "sdc",
                "serialNum": "WD2",
                "vendor": "WDC",
                "size": 16 * 1024**4,
                "type": "HD",
                "interfaceType": "SATA",
                "temperature": 48.0,
                "smartStatus": "UNKNOWN",
                "isSpinning": True,
            },
        ]
