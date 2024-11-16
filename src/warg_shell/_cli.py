import asyncio
import json
import os
import re
import shlex
from datetime import datetime
from functools import wraps
from pathlib import Path
from sys import argv

import httpx
import keyring
import psutil
import rich_click as click
from rich import print
from rich.console import Console
from rich.syntax import Syntax
from rich.traceback import install as install_traceback
from zoneinfo import ZoneInfo

from ._jon import Jon
from ._warg import WargShell


def validate_domain(ctx, param, value):
    domain_pattern = r"^(https?:\/\/)?(localhost|(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})|([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,6})(:(\d{1,5}))?(\/.*)?$"
    if not re.match(domain_pattern, value):
        raise click.BadParameter(
            'Invalid domain. Please provide a valid hostname, IP address, or "localhost", optionally prefixed with http:// or https://, and optionally followed by a port number.'
        )
    return value


def detect_uvx_cli(a: list[str]) -> list[str]:
    if len(a) < 3:
        return []

    if a[0] != "uv" and not a[0].endswith("/uv"):
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

    if a[0] == "warg-shell" or a[0].endswith("/warg-shell"):
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


@click.group()
def main():
    install_traceback()


@main.command()
@click.argument("domain", callback=validate_domain)
@click.argument("token", type=str)
@arun
async def auth(token, domain):
    jon = Jon(domain)
    console = Console()
    success = False

    with console.status("[bold blue]Authenticating...", spinner="dots"):
        try:
            auth_token = await jon.get_auth_token(token)
            keyring.set_password("warg-shell", domain, json.dumps(auth_token))
            success = True
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 403:
                raise

    if success:
        print("[green bold]✓ Auth successful")
    else:
        print("[red bold]✗ Auth failed")


@main.command()
@click.argument("domain", callback=validate_domain)
@click.argument("product", type=str)
@click.argument("env", type=str)
@click.argument("component", type=str)
@arun
async def shell(domain, product, env, component):
    console = Console()

    with console.status("[bold blue]Connecting...", spinner="dots"):
        if not (info := keyring.get_password("warg-shell", domain)):
            cli = detect_cli(domain)
            print("[red bold]Not authenticated, please run:")
            syntax = Syntax(cli, "bash")
            console.print(syntax)
            exit(1)

        info = json.loads(info)
        valid_until = datetime.fromisoformat(info["valid_until"])

        if datetime.now(ZoneInfo("UTC")) > valid_until:
            cli = detect_cli(domain)
            print("[red bold]Auth token expired, please run:")
            syntax = Syntax(cli, "bash")
            console.print(syntax)
            exit(1)

        jon = Jon(domain)
        ws_url = await jon.get_shell_url(info["token"], product, env, component)

    if ws_url.success:
        warg = WargShell(ws_url.url)
        await warg.connect_tty()
    else:
        print(f"[red bold]{ws_url.error}")


if __name__ == "__main__":
    main()
