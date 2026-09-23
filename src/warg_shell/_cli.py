import asyncio
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path, PurePosixPath, PureWindowsPath
from sys import argv
from typing import BinaryIO

import httpx
import keyring
import keyring.errors
import psutil
import rich_click as click
from rich.console import Console
from rich.syntax import Syntax
from rich.traceback import install as install_traceback

from ._jon import Jon, PgDumpResponse
from ._warg import WargShell

console = Console(stderr=True)


def validate_domain(ctx, param, value):
    domain_pattern = (
        r"^(https?:\/\/)?(localhost|(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
        r"|([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,6})"
        r"(:(\d{1,5}))?(\/.*)?$"
    )
    if not re.match(domain_pattern, value):
        msg = (
            "Invalid domain. Please provide a valid hostname, IP address, or "
            '"localhost", optionally prefixed with http:// or https://, and '
            "optionally followed by a port number."
        )
        raise click.BadParameter(msg)
    return value


def _is_program(arg: str, name: str) -> bool:
    """Whether ``arg`` is ``name`` itself or a path to it, on any platform."""
    base = PureWindowsPath(arg).name if "\\" in arg else PurePosixPath(arg).name
    return base.lower() in {name, f"{name}.exe"}


def detect_uvx_cli(a: list[str]) -> list[str]:
    if len(a) < 3:
        return []

    if not _is_program(a[0], "uv"):
        return []

    if a[1] != "tool" or a[2] != "uvx":
        return []

    cmd = -1

    for i in range(3, len(a)):
        if a[i] == "warg-shell" and a[i - 1] != "--from":
            cmd = i
            break

    if cmd >= 0:
        return a[2 : cmd + 1]

    return []


def detect_module_cli(a: list[str]) -> list[str]:
    if len(a) < 1:
        return []

    if Path(a[0]).absolute() == (Path(__file__).parent / "__main__.py").absolute():
        return ["python", "-m", "warg_shell"]

    return []


def detect_direct_cli(a: list[str]) -> list[str]:
    if len(a) < 1:
        return []

    if _is_program(a[0], "warg-shell"):
        return ["warg-shell"]

    return []


def detect_cli(domain: str):
    p = psutil.Process(os.getpid())
    parent = p.parent()

    a1 = argv
    a2 = parent.cmdline()

    if prefix := detect_uvx_cli(a2):
        pass
    elif prefix := detect_module_cli(a1):
        pass
    elif prefix := detect_direct_cli(a1):
        pass
    else:
        prefix = a1

    args = [*prefix, "auth", domain, "<your-token>"]

    return " ".join([shlex.quote(x) for x in args])


def arun(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper


KEYRING_SERVICE = "warg-shell"


def _no_keyring_help() -> str:
    """
    Explain what to do when ``keyring`` found nowhere to store the token.

    Returns
    -------
    str
        Rich-formatted, platform-specific advice.
    """
    from ._keyring import find_powershell, is_wsl

    if is_wsl() and find_powershell() is None:
        return (
            "Running under WSL but [bold]powershell.exe[/] is not reachable, so "
            "the token cannot be stored in the Windows Credential Manager.\n"
            "Enable Windows interop in [bold]/etc/wsl.conf[/] "
            "([dim][interop] enabled=true[/]) or make sure the Windows drive is "
            "mounted under [bold]/mnt/c[/]."
        )

    return (
        "No keyring backend is available to store the token.\n"
        "On Linux, start a Secret Service daemon (GNOME Keyring, KWallet, "
        "KeePassXC…); on a headless box you can use the "
        "[bold]keyrings.alt[/] package."
    )


def keyring_get(domain: str) -> str | None:
    """
    Read the stored auth info for a domain.

    Returns
    -------
    str | None
        The JSON blob stored by ``auth``, or ``None``.
    """
    try:
        return keyring.get_password(KEYRING_SERVICE, domain)
    except keyring.errors.NoKeyringError:
        console.print("[red bold]✗ Cannot read the auth token")
        console.print(_no_keyring_help())
        sys.exit(1)


def keyring_set(domain: str, value: str) -> None:
    """Store the auth info for a domain, or explain why it cannot be done."""
    try:
        keyring.set_password(KEYRING_SERVICE, domain, value)
    except keyring.errors.NoKeyringError:
        console.print("[red bold]✗ Auth succeeded but the token cannot be stored")
        console.print(_no_keyring_help())
        sys.exit(1)


@click.group()
def main():
    install_traceback()


@main.command()
@click.argument("domain", callback=validate_domain)
@click.argument("token", type=str)
@arun
async def auth(token, domain):
    jon = Jon(domain)
    success = False

    with console.status("[bold blue]Authenticating...", spinner="dots"):
        try:
            auth_token = await jon.get_auth_token(token)
            keyring_set(domain, json.dumps(auth_token))
            success = True
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 403:
                raise

    if success:
        console.print("[green bold]✓ Auth successful")
    else:
        console.print("[red bold]✗ Auth failed")


@main.command()
@click.argument("domain", callback=validate_domain)
@click.argument("product", type=str)
@click.argument("env", type=str)
@click.argument("component", type=str)
@arun
async def shell(domain, product, env, component):
    with console.status("[bold blue]Connecting...", spinner="dots"):
        if not (info := keyring_get(domain)):
            cli = detect_cli(domain)
            console.print("[red bold]Not authenticated, please run:")
            syntax = Syntax(cli, "bash")
            console.print(syntax)
            exit(1)

        info = json.loads(info)
        valid_until = datetime.fromisoformat(info["valid_until"])

        if datetime.now(UTC) > valid_until:
            cli = detect_cli(domain)
            console.print("[red bold]Auth token expired, please run:")
            syntax = Syntax(cli, "bash")
            console.print(syntax)
            exit(1)

        jon = Jon(domain)
        ws_url = await jon.get_shell_url(info["token"], product, env, component)

    if ws_url.success:
        warg = WargShell(ws_url.url)
        await warg.connect_tty()
    else:
        console.print(f"[red bold]{ws_url.error}")


@dataclass
class DumperChecker:
    """
    Tee for the dump stream that remembers its tail.

    Lets us check, once everything is written, that the stream ended with
    the sequence we expect.
    """

    expected: bytes
    output: BinaryIO
    _last_bytes: bytes = field(init=False, default=b"")

    def dump(self, data: bytes):
        self._last_bytes = (self._last_bytes + data)[-len(self.expected) :]
        self.output.write(data)

    def check(self) -> bool:
        return self._last_bytes == self.expected


@main.command()
@click.argument("domain", callback=validate_domain)
@click.argument("product", type=str)
@click.argument("env", type=str)
@click.argument("db", type=str)
@click.option(
    "-o",
    "--output",
    default="-",
    type=click.File("wb"),
    help="Output file (default: stdout)",
)
@arun
async def pg_dump(
    domain: str,
    product: str,
    env: str,
    db: str,
    output: BinaryIO,
):
    with console.status("[bold blue]🔭 Connecting...", spinner="dots"):
        if not (info := keyring_get(domain)):
            cli = detect_cli(domain)
            console.print("[red bold]Not authenticated, please run:")
            syntax = Syntax(cli, "bash")
            console.print(syntax)
            exit(1)

        info = json.loads(info)
        valid_until = datetime.fromisoformat(info["valid_until"])

        if datetime.now(UTC) > valid_until:
            cli = detect_cli(domain)
            console.print("[red bold]Auth token expired, please run:")
            syntax = Syntax(cli, "bash")
            console.print(syntax)
            exit(1)

        jon = Jon(domain)
        data = jon.get_pg_dump(info["token"], product, env, db)
        dc = DumperChecker(
            expected=b"\n\n--\n-- PostgreSQL database dump complete\n--\n\n",
            output=output,
        )

        async for chunk in data:
            if isinstance(chunk, PgDumpResponse):
                if not chunk.success:
                    console.print(f"[red bold]{chunk.error}")
                    exit(1)
            else:
                dc.dump(chunk)
                break

    with console.status("[bold green]🎣 Downloading...", spinner="dots"):
        async for chunk in data:
            dc.dump(chunk)

    if not dc.check():
        console.print("[red bold]⚠️ Dump looks incomplete")
        exit(2)

    console.print("[green bold]📚 Dump downloaded successfully")


if __name__ == "__main__":
    main()
