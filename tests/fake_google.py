"""Fake Google token endpoint and a small in-memory Sheets API for tests.

Token endpoint accepts the service account jwt-bearer grant, verifies the RS256 signature
with the test key, and issues a short-lived token. Sheets API implements the subset the
toolset uses, on an in-memory grid, with knobs to force 403, 404, 429 and 500.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_PEM = (
    _KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)

SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "test-proj",
    "private_key_id": "kid1",
    "private_key": PRIVATE_PEM,
    "client_email": "bot@test-proj.iam.gserviceaccount.com",
}
ACCESS_TOKEN = "sa-access-token"

_COL = re.compile(r"^([A-Z]+)(\d*)$")


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


class FakeGoogle:
    def __init__(self) -> None:
        self.token_requests: list[dict] = []
        self.token_issued = 0
        self.fail_next: list[int] = []  # statuses to return before succeeding
        self.forbidden: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.batch_updates: list[dict] = []
        self.spreadsheets: dict[str, dict] = {}
        self.app = Starlette(
            routes=[
                Route("/token", self.token, methods=["POST"]),
                Route("/v4/spreadsheets/{sid}:batchUpdate", self.batch_update, methods=["POST"]),
                Route("/v4/spreadsheets/{sid}/values:batchGet", self.batch_get, methods=["GET"]),
                Route(
                    "/v4/spreadsheets/{sid}/values:batchUpdate",
                    self.values_batch_update,
                    methods=["POST"],
                ),
                Route(
                    "/v4/spreadsheets/{sid}/values/{rng:path}",
                    self.values,
                    methods=["GET", "PUT", "POST"],
                ),
                Route("/v4/spreadsheets/{sid}", self.get_spreadsheet, methods=["GET"]),
            ]
        )

    # -- fixtures -----------------------------------------------------------------------

    def add_spreadsheet(
        self, sid: str, title: str, sheets: dict[str, list[list[Any]]], locale="en_AU"
    ):
        self.spreadsheets[sid] = {
            "title": title,
            "locale": locale,
            "sheets": [
                {
                    "id": i,
                    "title": name,
                    "grid": [list(r) for r in rows],
                    "frozen_rows": 0,
                    "frozen_cols": 0,
                }
                for i, (name, rows) in enumerate(sheets.items())
            ],
        }

    def grid(self, sid: str, sheet: str) -> list[list[Any]]:
        return next(s for s in self.spreadsheets[sid]["sheets"] if s["title"] == sheet)["grid"]

    # -- token --------------------------------------------------------------------------

    async def token(self, request: Request):
        form = dict(await request.form())
        self.token_requests.append(form)
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return JSONResponse({"error": "unsupported_grant_type"}, 400)
        try:
            claims = jwt.decode(
                form["assertion"],
                PUBLIC_PEM,
                algorithms=["RS256"],
                audience=None,
                options={"verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            return JSONResponse({"error": "invalid_grant", "error_description": str(exc)}, 400)
        if claims.get("iss") != SERVICE_ACCOUNT["client_email"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "bad issuer"}, 400)
        self.token_issued += 1
        return JSONResponse(
            {
                "access_token": f"{ACCESS_TOKEN}-{self.token_issued}",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        )

    # -- helpers ------------------------------------------------------------------------

    def _guard(self, request: Request, sid: str):
        auth = request.headers.get("authorization", "")
        if not auth.startswith(f"Bearer {ACCESS_TOKEN}") and not auth.startswith("Bearer oauth-"):
            return JSONResponse(
                {
                    "error": {
                        "code": 401,
                        "message": "Request had invalid authentication credentials.",
                        "status": "UNAUTHENTICATED",
                    }
                },
                401,
            )
        if self.fail_next:
            status = self.fail_next.pop(0)
            return JSONResponse(
                {"error": {"code": status, "message": f"forced {status}", "status": "FORCED"}},
                status,
            )
        if sid in self.forbidden:
            return JSONResponse(
                {
                    "error": {
                        "code": 403,
                        "message": "The caller does not have permission",
                        "status": "PERMISSION_DENIED",
                    }
                },
                403,
            )
        if sid not in self.spreadsheets:
            return JSONResponse(
                {
                    "error": {
                        "code": 404,
                        "message": "Requested entity was not found.",
                        "status": "NOT_FOUND",
                    }
                },
                404,
            )
        return None

    def _parse_range(self, sid: str, rng: str) -> tuple[dict, int, int, int | None, int | None]:
        """Returns (sheet, r0, c0, r1, c1) zero-based inclusive, None for open-ended."""
        rng = unquote(rng)
        if "!" in rng:
            sheet_name, cells = rng.rsplit("!", 1)
            sheet_name = sheet_name.strip("'").replace("''", "'")
        else:
            sheet_name, cells = rng, ""
        sheet = next(
            (s for s in self.spreadsheets[sid]["sheets"] if s["title"] == sheet_name), None
        )
        if sheet is None:
            raise KeyError(sheet_name)
        if not cells:
            return sheet, 0, 0, None, None
        parts = cells.split(":")
        start = _COL.match(parts[0]) or re.match(r"^()(\d+)$", parts[0])
        end = (
            (_COL.match(parts[1]) or re.match(r"^()(\d+)$", parts[1])) if len(parts) > 1 else start
        )
        c0 = col_index(start.group(1)) if start.group(1) else 0
        r0 = int(start.group(2)) - 1 if start.group(2) else 0
        c1 = col_index(end.group(1)) if end.group(1) else None
        r1 = int(end.group(2)) - 1 if end.group(2) else None
        return sheet, r0, c0, r1, c1

    @staticmethod
    def _slice(grid, r0, c0, r1, c1):
        rows = grid[r0 : (r1 + 1 if r1 is not None else None)]
        out = []
        for row in rows:
            cells = row[c0 : (c1 + 1 if c1 is not None else None)]
            while cells and cells[-1] in ("", None):
                cells = cells[:-1]
            out.append(cells)
        while out and not out[-1]:
            out.pop()
        return out

    # -- endpoints ----------------------------------------------------------------------

    async def get_spreadsheet(self, request: Request):
        sid = request.path_params["sid"]
        self.calls.append(("GET", f"spreadsheets/{sid}"))
        if (err := self._guard(request, sid)) is not None:
            return err
        ss = self.spreadsheets[sid]
        return JSONResponse(
            {
                "spreadsheetId": sid,
                "properties": {
                    "title": ss["title"],
                    "locale": ss["locale"],
                    "timeZone": "Australia/Melbourne",
                },
                "sheets": [
                    {
                        "properties": {
                            "sheetId": s["id"],
                            "title": s["title"],
                            "index": i,
                            "gridProperties": {
                                "rowCount": max(len(s["grid"]), 1000),
                                "columnCount": 26,
                                "frozenRowCount": s["frozen_rows"],
                                "frozenColumnCount": s["frozen_cols"],
                            },
                        }
                    }
                    for i, s in enumerate(ss["sheets"])
                ],
            }
        )

    async def batch_get(self, request: Request):
        sid = request.path_params["sid"]
        self.calls.append(("GET", f"values:batchGet {request.query_params.getlist('ranges')}"))
        if (err := self._guard(request, sid)) is not None:
            return err
        out = []
        for rng in request.query_params.getlist("ranges"):
            sheet, r0, c0, r1, c1 = self._parse_range(sid, rng)
            out.append({"range": rng, "values": self._slice(sheet["grid"], r0, c0, r1, c1)})
        return JSONResponse({"valueRanges": out})

    async def values(self, request: Request):
        sid, rng = request.path_params["sid"], request.path_params["rng"]
        op = ""
        if ":" in rng and rng.rsplit(":", 1)[-1] in ("append", "clear"):
            rng, op = rng.rsplit(":", 1)
        self.calls.append(
            (request.method, f"values/{unquote(rng)}:{op or 'get'} {dict(request.query_params)}")
        )
        if (err := self._guard(request, sid)) is not None:
            return err
        try:
            sheet, r0, c0, r1, c1 = self._parse_range(sid, rng)
        except KeyError:
            return JSONResponse(
                {
                    "error": {
                        "code": 400,
                        "message": f"Unable to parse range: {unquote(rng)}",
                        "status": "INVALID_ARGUMENT",
                    }
                },
                400,
            )
        grid = sheet["grid"]
        if request.method == "GET":
            render = request.query_params.get("valueRenderOption", "FORMATTED_VALUE")
            values = self._slice(grid, r0, c0, r1, c1)
            if render == "FORMULA":
                values = [
                    [f"={v}" if isinstance(v, (int, float)) else v for v in row] for row in values
                ]
            elif render == "UNFORMATTED_VALUE":
                values = [[_unformat(v) for v in row] for row in values]
            return JSONResponse({"range": unquote(rng), "majorDimension": "ROWS", "values": values})
        body = await request.json() if request.method in ("PUT", "POST") and op != "clear" else {}
        if op == "clear":
            for r in range(r0, (r1 + 1) if r1 is not None else len(grid)):
                if r < len(grid):
                    for c in range(c0, (c1 + 1) if c1 is not None else len(grid[r])):
                        if c < len(grid[r]):
                            grid[r][c] = ""
            return JSONResponse({"clearedRange": unquote(rng)})
        if op == "append":
            start = len(grid)
            for row in body.get("values", []):
                grid.append(
                    [""] * c0
                    + [_enter(v, request.query_params.get("valueInputOption")) for v in row]
                )
            return JSONResponse(
                {
                    "updates": {
                        "updatedRange": f"{sheet['title']}!A{start + 1}",
                        "updatedRows": len(body.get("values", [])),
                    }
                }
            )
        # PUT update
        for i, row in enumerate(body.get("values", [])):
            r = r0 + i
            while len(grid) <= r:
                grid.append([])
            for j, v in enumerate(row):
                c = c0 + j
                while len(grid[r]) <= c:
                    grid[r].append("")
                grid[r][c] = _enter(v, request.query_params.get("valueInputOption"))
        return JSONResponse(
            {"updatedRange": unquote(rng), "updatedRows": len(body.get("values", []))}
        )

    async def values_batch_update(self, request: Request):
        sid = request.path_params["sid"]
        self.calls.append(("POST", "values:batchUpdate"))
        if (err := self._guard(request, sid)) is not None:
            return err
        body = await request.json()
        total = 0
        for item in body.get("data", []):
            sheet, r0, c0, _, _ = self._parse_range(sid, item["range"])
            grid = sheet["grid"]
            for i, row in enumerate(item.get("values", [])):
                r = r0 + i
                while len(grid) <= r:
                    grid.append([])
                for j, v in enumerate(row):
                    c = c0 + j
                    while len(grid[r]) <= c:
                        grid[r].append("")
                    grid[r][c] = _enter(v, body.get("valueInputOption"))
                total += 1
        return JSONResponse({"totalUpdatedRows": total})

    async def batch_update(self, request: Request):
        sid = request.path_params["sid"]
        self.calls.append(("POST", "batchUpdate"))
        if (err := self._guard(request, sid)) is not None:
            return err
        body = await request.json()
        self.batch_updates.append(body)
        ss = self.spreadsheets[sid]
        replies = []
        for req in body.get("requests", []):
            if "addSheet" in req:
                new_id = max([s["id"] for s in ss["sheets"]] + [-1]) + 1
                ss["sheets"].append(
                    {
                        "id": new_id,
                        "title": req["addSheet"]["properties"]["title"],
                        "grid": [],
                        "frozen_rows": 0,
                        "frozen_cols": 0,
                    }
                )
                replies.append(
                    {
                        "addSheet": {
                            "properties": {
                                "sheetId": new_id,
                                "title": req["addSheet"]["properties"]["title"],
                            }
                        }
                    }
                )
            elif "deleteSheet" in req:
                ss["sheets"] = [s for s in ss["sheets"] if s["id"] != req["deleteSheet"]["sheetId"]]
                replies.append({})
            elif "updateSheetProperties" in req:
                props = req["updateSheetProperties"]["properties"]
                sheet = next(s for s in ss["sheets"] if s["id"] == props["sheetId"])
                gp = props.get("gridProperties", {})
                sheet["frozen_rows"] = gp.get("frozenRowCount", sheet["frozen_rows"])
                sheet["frozen_cols"] = gp.get("frozenColumnCount", sheet["frozen_cols"])
                replies.append({})
            else:
                replies.append({})
        return JSONResponse({"spreadsheetId": sid, "replies": replies})


def _enter(value: Any, mode: str | None) -> Any:
    """USER_ENTERED turns numeric strings into numbers, like typing into a cell."""
    if mode == "USER_ENTERED" and isinstance(value, str):
        try:
            if re.fullmatch(r"-?\d+", value) and not (len(value) > 1 and value.startswith("0")):
                return int(value)
            if re.fullmatch(r"-?\d+\.\d+", value):
                return float(value)
        except ValueError:
            pass
    return value


def _unformat(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return float(value[1:].replace(",", ""))
    return value


def dump(obj: Any) -> str:
    return json.dumps(obj)
