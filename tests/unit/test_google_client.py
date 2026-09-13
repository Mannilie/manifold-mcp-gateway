"""Shared Google client: JWT exchange, caching, coalescing, backoff, error mapping."""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from manifold.gateway.manifest import Credentials
from manifold.google import auth, client, endpoints
from manifold.google.errors import GoogleError, map_error
from tests.fake_google import SERVICE_ACCOUNT, FakeGoogle

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@pytest.fixture
async def fake(monkeypatch):
    g = FakeGoogle()
    g.add_spreadsheet("ss1", "Budget", {"Sheet1": [["Name", "Qty"], ["Bolt", 12]]})
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=g.app), base_url="https://fake")
    monkeypatch.setattr(endpoints, "TOKEN_URL", "https://fake/token")
    monkeypatch.setattr(endpoints, "SHEETS_BASE", "https://fake/v4")
    monkeypatch.setattr(client, "RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr(client, "RETRY_JITTER_SECONDS", 0.0)
    auth.clear_cache()
    yield g, http
    await http.aclose()


def sa_credentials() -> Credentials:
    return Credentials(kind="service_account", values={"json": SERVICE_ACCOUNT})


async def test_service_account_jwt_exchange_and_cache(fake):
    g, http = fake
    c = client.client_for(sa_credentials(), SCOPES, http=http)
    assert c.identity == SERVICE_ACCOUNT["client_email"]
    body = await c.get("spreadsheets/ss1")
    assert body["properties"]["title"] == "Budget"
    await c.get("spreadsheets/ss1")
    assert g.token_issued == 1, "second call reused the cached token"
    assert g.token_requests[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"


async def test_service_account_token_is_shared_and_coalesced(fake):
    g, http = fake
    a = client.client_for(sa_credentials(), SCOPES, http=http)
    b = client.client_for(sa_credentials(), SCOPES, http=http)
    await asyncio.gather(*(c.get("spreadsheets/ss1") for c in (a, b, a, b)))
    assert g.token_issued == 1


async def test_bad_service_account_json_is_readable(fake):
    _, http = fake
    creds = Credentials(kind="service_account", values={"json": {"client_email": "x"}})
    with pytest.raises(GoogleError, match="private_key"):
        client.client_for(creds, SCOPES, http=http)


async def test_oauth_credentials_use_the_token_getter(fake):
    g, http = fake
    calls = []

    async def getter() -> str:
        calls.append(1)
        return "oauth-token"

    creds = Credentials(kind="oauth2", values={}).with_token_getter(getter)
    c = client.client_for(creds, SCOPES, http=http)
    assert c.identity is None
    assert (await c.get("spreadsheets/ss1"))["spreadsheetId"] == "ss1"
    assert calls == [1] and g.token_issued == 0


async def test_retries_on_429_and_5xx_then_succeeds(fake):
    g, http = fake
    g.fail_next = [429, 503, 500]
    c = client.client_for(sa_credentials(), SCOPES, http=http)
    assert (await c.get("spreadsheets/ss1"))["spreadsheetId"] == "ss1"


async def test_gives_up_after_three_retries_with_rate_limit_message(fake):
    g, http = fake
    g.fail_next = [429, 429, 429, 429]
    c = client.client_for(sa_credentials(), SCOPES, http=http)
    with pytest.raises(GoogleError, match="rate-limited") as info:
        await c.get("spreadsheets/ss1", context={"spreadsheet_id": "ss1"})
    assert info.value.reason == "rate_limited"


async def test_4xx_never_retries(fake):
    g, http = fake
    g.fail_next = [400]
    c = client.client_for(sa_credentials(), SCOPES, http=http)
    with pytest.raises(GoogleError, match="rejected"):
        await c.get("spreadsheets/ss1", context={"spreadsheet_id": "ss1"})
    assert g.fail_next == [] and len([x for x in g.calls if x[1].startswith("spreadsheets")]) == 1


async def test_403_names_the_service_account_to_share_with(fake):
    g, http = fake
    g.forbidden.add("ss1")
    c = client.client_for(sa_credentials(), SCOPES, http=http)
    with pytest.raises(
        GoogleError, match=r"Share it with bot@test-proj\.iam\.gserviceaccount\.com"
    ):
        await c.get("spreadsheets/ss1", context={"spreadsheet_id": "ss1"})


async def test_404_is_not_found(fake):
    _, http = fake
    c = client.client_for(sa_credentials(), SCOPES, http=http)
    with pytest.raises(GoogleError, match="spreadsheet or range not found: nope"):
        await c.get("spreadsheets/nope", context={"spreadsheet_id": "nope"})


def test_error_mapping_table():
    assert "Share it with bot@x" in str(
        map_error(403, {}, context={"spreadsheet_id": "s"}, identity="bot@x")
    )
    assert "does not have access to s: denied" in str(
        map_error(403, {"error": {"message": "denied"}}, context={"spreadsheet_id": "s"})
    )
    assert (
        str(map_error(404, {}, context={"spreadsheet_id": "s", "range": "A1"}))
        == "spreadsheet or range not found: s range A1"
    )
    assert "Unable to parse" in str(
        map_error(
            400, {"error": {"message": "Unable to parse range"}}, context={"spreadsheet_id": "s"}
        )
    )
    assert "Reconnect" in str(map_error(401, {}))
    assert map_error(429, {}).reason == "rate_limited"
    assert map_error(503, {"error": {"message": "boom"}}).reason == "upstream_error"
