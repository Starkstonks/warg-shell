"""End-to-end tests of the click commands against the fake servers."""

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from warg_shell import _cli
from warg_shell._cli import (
    DumperChecker,
    detect_direct_cli,
    detect_module_cli,
    detect_uvx_cli,
    main,
    validate_domain,
)


@pytest.fixture
def runner() -> CliRunner:
    """
    Click test runner.

    Returns
    -------
    CliRunner
        Runner capturing stdout and stderr together.
    """
    return CliRunner()


@pytest.mark.parametrize(
    "domain",
    [
        "warg.example.com",
        "https://warg.example.com",
        "http://localhost:8000",
        "127.0.0.1:8000",
        "http://10.0.0.1/some/path",
    ],
)
def test_validate_domain_accepts(domain):
    """Hostnames, IPs, localhost, with or without scheme/port/path pass."""
    assert validate_domain(None, None, domain) == domain


@pytest.mark.parametrize("domain", ["", "not a domain", "ftp://x.com", "foo"])
def test_validate_domain_rejects(domain):
    """Garbage and unsupported schemes are refused with a click error."""
    with pytest.raises(click.BadParameter):
        validate_domain(None, None, domain)


def test_detect_uvx_cli():
    """The ``uvx [--from pkg] warg-shell`` prefix is recovered from uv's argv."""
    assert detect_uvx_cli(
        ["/usr/bin/uv", "tool", "uvx", "warg-shell", "shell", "x", "y", "z", "w"]
    ) == ["uvx", "warg-shell"]
    assert detect_uvx_cli(
        ["uv", "tool", "uvx", "--from", "warg-shell", "warg-shell", "shell"]
    ) == ["uvx", "--from", "warg-shell", "warg-shell"]
    assert detect_uvx_cli(["uv", "tool", "run", "warg-shell"]) == []
    assert detect_uvx_cli(["bash"]) == []


def test_detect_module_cli():
    """Running ``python -m warg_shell`` is recognised by the ``__main__`` path."""
    main_py = str(Path(_cli.__file__).parent / "__main__.py")
    assert detect_module_cli([main_py, "shell"]) == ["python", "-m", "warg_shell"]
    assert detect_module_cli(["/somewhere/else.py"]) == []


def test_detect_direct_cli():
    """A bare or absolute ``warg-shell`` executable is recognised."""
    assert detect_direct_cli(["warg-shell", "shell"]) == ["warg-shell"]
    assert detect_direct_cli(["/home/me/.local/bin/warg-shell"]) == ["warg-shell"]
    assert detect_direct_cli(["/usr/bin/something"]) == []


def test_dumper_checker():
    """Bytes are written through and the tail is matched across chunks."""
    out = io.BytesIO()
    dc = DumperChecker(expected=b"END\n", output=out)
    dc.dump(b"some ")
    dc.dump(b"data EN")
    assert not dc.check()
    dc.dump(b"D\n")
    assert dc.check()
    assert out.getvalue() == b"some data END\n"


def test_auth_stores_token_in_keyring(runner, fake_jon, memory_keyring):
    """``auth`` exchanges the token and stores the result under the domain."""
    result = runner.invoke(main, ["auth", fake_jon.domain, fake_jon.req_token])

    assert result.exit_code == 0, result.output
    assert "Auth successful" in result.output
    stored = memory_keyring.get_password("warg-shell", fake_jon.domain)
    assert json.loads(stored) == json.loads(fake_jon.stored_auth)


def test_auth_failure_stores_nothing(runner, fake_jon, memory_keyring):
    """A 403 is reported as a failure, not a crash, and nothing is stored."""
    result = runner.invoke(main, ["auth", fake_jon.domain, "wrong-token"])

    assert result.exit_code == 0, result.output
    assert "Auth failed" in result.output
    assert memory_keyring.storage == {}


def test_shell_requires_auth(runner, fake_jon, memory_keyring):
    """Without stored credentials, ``shell`` prints the ``auth`` command to run."""
    result = runner.invoke(
        main, ["shell", fake_jon.domain, "product", "env", "component"]
    )

    assert result.exit_code == 1
    assert "Not authenticated" in result.output
    assert "auth" in result.output
    assert fake_jon.requests == []


def test_shell_rejects_expired_token(runner, fake_jon, memory_keyring):
    """An expired stored token is refused before calling the server."""
    memory_keyring.set_password(
        "warg-shell",
        fake_jon.domain,
        json.dumps(
            {
                "token": fake_jon.auth_token,
                "valid_until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            }
        ),
    )

    result = runner.invoke(
        main, ["shell", fake_jon.domain, "product", "env", "component"]
    )

    assert result.exit_code == 1
    assert "expired" in result.output
    assert fake_jon.requests == []


def test_shell_reports_unknown_component(runner, fake_jon, memory_keyring):
    """The server's 404 detail is shown to the user."""
    memory_keyring.set_password("warg-shell", fake_jon.domain, fake_jon.stored_auth)

    result = runner.invoke(main, ["shell", fake_jon.domain, "product", "env", "nope"])

    assert result.exit_code == 0, result.output
    assert "Component not found" in result.output


def test_shell_connects_to_returned_url(runner, fake_jon, memory_keyring, monkeypatch):
    """The websocket URL returned by Jon is what gets connected to."""
    memory_keyring.set_password("warg-shell", fake_jon.domain, fake_jon.stored_auth)
    connected = []

    class FakeWargShell:
        """Record the URL instead of opening a websocket."""

        def __init__(self, url):
            connected.append(url)

        async def connect_tty(self):
            """Pretend the session ran."""

    monkeypatch.setattr(_cli, "WargShell", FakeWargShell)

    result = runner.invoke(
        main, ["shell", fake_jon.domain, "product", "env", "component"]
    )

    assert result.exit_code == 0, result.output
    assert connected == [fake_jon.shell_url]


def test_pg_dump_to_file(runner, fake_jon, memory_keyring, tmp_path):
    """A complete dump is streamed to the output file and reported as such."""
    memory_keyring.set_password("warg-shell", fake_jon.domain, fake_jon.stored_auth)
    out = tmp_path / "dump.sql"

    result = runner.invoke(
        main, ["pg-dump", fake_jon.domain, "product", "env", "db", "-o", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert "Dump downloaded successfully" in result.output
    assert out.read_bytes() == b"".join(fake_jon.pg_dump_chunks)


def test_pg_dump_detects_truncated_dump(runner, fake_jon, memory_keyring, tmp_path):
    """A dump missing the PostgreSQL footer exits with code 2."""
    memory_keyring.set_password("warg-shell", fake_jon.domain, fake_jon.stored_auth)
    fake_jon.pg_dump_chunks = [b"-- header\n", b"CREATE TABLE foo();"]
    out = tmp_path / "dump.sql"

    result = runner.invoke(
        main, ["pg-dump", fake_jon.domain, "product", "env", "db", "-o", str(out)]
    )

    assert result.exit_code == 2
    assert "incomplete" in result.output
    assert out.read_bytes() == b"-- header\nCREATE TABLE foo();"


def test_pg_dump_reports_server_error(runner, fake_jon, memory_keyring):
    """A server-side error is shown and exits with code 1."""
    memory_keyring.set_password("warg-shell", fake_jon.domain, fake_jon.stored_auth)

    result = runner.invoke(main, ["pg-dump", fake_jon.domain, "product", "env", "nope"])

    assert result.exit_code == 1
    assert "Database not found" in result.output


def test_pg_dump_requires_auth(runner, fake_jon, memory_keyring):
    """``pg-dump`` needs stored credentials like ``shell`` does."""
    result = runner.invoke(main, ["pg-dump", fake_jon.domain, "product", "env", "db"])

    assert result.exit_code == 1
    assert "Not authenticated" in result.output
