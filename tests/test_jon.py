"""Tests for the Jon HTTP client, against the real local fake server."""

import httpx
import pytest

from warg_shell._jon import Jon, PgDumpResponse


def test_base_url_adds_https_when_no_scheme():
    """A bare hostname is assumed to be served over HTTPS."""
    assert str(Jon("warg.example.com").base_url) == (
        "https://warg.example.com/back/api/warg/"
    )


def test_base_url_keeps_explicit_scheme_and_port():
    """An explicit scheme and port are kept as-is (local dev servers)."""
    assert str(Jon("http://localhost:8000").base_url) == (
        "http://localhost:8000/back/api/warg/"
    )


async def test_get_auth_token(fake_jon):
    """The request token is exchanged for an auth token and its validity."""
    token = await Jon(fake_jon.domain).get_auth_token(fake_jon.req_token)

    assert token["token"] == fake_jon.auth_token
    assert token["valid_until"] == fake_jon.valid_until.isoformat()
    assert fake_jon.requests == [
        ("/back/api/warg/token/", {"token": fake_jon.req_token})
    ]


async def test_get_auth_token_rejected(fake_jon):
    """A 403 is surfaced as an HTTP error for the CLI to handle."""
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await Jon(fake_jon.domain).get_auth_token("wrong")

    assert exc_info.value.response.status_code == 403


async def test_get_shell_url(fake_jon):
    """A known component yields the websocket URL to connect to."""
    resp = await Jon(fake_jon.domain).get_shell_url(
        fake_jon.auth_token, "product", "env", "component"
    )

    assert resp.success
    assert resp.url == fake_jon.shell_url
    assert fake_jon.requests[-1][1] == {
        "token": fake_jon.auth_token,
        "product": "product",
        "env": "env",
        "component": "component",
    }


async def test_get_shell_url_unknown_component(fake_jon):
    """A 404 is turned into a failed response carrying the server's detail."""
    resp = await Jon(fake_jon.domain).get_shell_url(
        fake_jon.auth_token, "product", "env", "nope"
    )

    assert not resp.success
    assert resp.error == "Component not found"


async def test_get_shell_url_unexpected_status_raises(fake_jon):
    """Statuses other than 200/404 are not swallowed."""
    with pytest.raises(httpx.HTTPStatusError):
        await Jon(fake_jon.domain).get_shell_url("bad", "product", "env", "component")


async def test_get_pg_dump_streams_chunks(fake_jon):
    """The dump is streamed as raw bytes without buffering the whole body."""
    chunks = [
        c
        async for c in Jon(fake_jon.domain).get_pg_dump(
            fake_jon.auth_token, "product", "env", "db"
        )
    ]

    assert all(isinstance(c, bytes) for c in chunks)
    assert b"".join(chunks) == b"".join(fake_jon.pg_dump_chunks)


async def test_get_pg_dump_error(fake_jon):
    """A 404 yields a single error response instead of bytes."""
    chunks = [
        c
        async for c in Jon(fake_jon.domain).get_pg_dump(
            fake_jon.auth_token, "product", "env", "nope"
        )
    ]

    assert chunks == [PgDumpResponse(success=False, error="Database not found")]
