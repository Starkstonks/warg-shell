"""
Keyring backend for WSL that stores secrets in the Windows Credential Manager.

Under WSL there is usually no Secret Service or KWallet daemon, so ``keyring``
has nowhere to put the auth token. Windows, on the other hand, always has a
credential vault, and WSL can run Windows executables through interop. This
backend shells out to ``powershell.exe`` and drives the
``Windows.Security.Credentials.PasswordVault`` WinRT API, which needs no
module, no elevation and no configuration.

The backend is registered through the ``keyring.backends`` entry point and
only declares itself viable when actually running under WSL with a reachable
``powershell.exe``, so on any other system it stays out of the way.
"""

from __future__ import annotations

import base64
import json
import platform
import shutil
import subprocess
from functools import cache
from pathlib import Path

from keyring import backend
from keyring.compat import properties
from keyring.errors import KeyringError, PasswordDeleteError

POWERSHELL_TIMEOUT = 30

# PasswordVault refuses to return more than one credential per (resource,
# user), and Windows caps the password at 512 chars for WinRT credentials.
# The auth token JSON is well under that.


def is_wsl() -> bool:
    """
    Detect whether we run inside the Windows Subsystem for Linux.

    Returns
    -------
    bool
        ``True`` on WSL 1 and 2, ``False`` anywhere else (including plain
        Linux and native Windows).
    """
    if platform.system() != "Linux":
        return False

    try:
        osrelease = Path("/proc/sys/kernel/osrelease").read_text()
    except OSError:
        return False

    return "microsoft" in osrelease.lower()


@cache
def find_powershell() -> str | None:
    """
    Locate ``powershell.exe`` from inside WSL.

    Returns
    -------
    str | None
        Path to the executable, or ``None`` when interop is disabled or the
        Windows system drive is not mounted.
    """
    if found := shutil.which("powershell.exe"):
        return found

    candidate = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if candidate.is_file():
        return str(candidate)

    return None


def _ps_string(value: str) -> str:
    """
    Quote a Python string for safe interpolation into PowerShell.

    Single-quoted PowerShell strings have no escapes other than doubling the
    quote, which makes them immune to injection.
    """
    return "'" + value.replace("'", "''") + "'"


class WslCredentialManager(backend.KeyringBackend):
    """
    Store credentials in the Windows Credential Manager from WSL.

    The ``service`` maps to the vault ``Resource`` and ``username`` to the
    vault ``UserName``.
    """

    powershell: str = ""

    @properties.classproperty
    def priority(cls) -> float:
        """
        Rank this backend among the others.

        Returns
        -------
        float
            4, just below the native Linux backends (5) so that a working
            Secret Service in WSL still wins.

        Raises
        ------
        RuntimeError
            When not running under WSL or ``powershell.exe`` is unreachable,
            which tells ``keyring`` this backend is not viable.
        """
        if not is_wsl():
            msg = "Only viable under WSL"
            raise RuntimeError(msg)
        if find_powershell() is None:
            msg = "powershell.exe not found, is WSL interop enabled?"
            raise RuntimeError(msg)
        return 4

    def _run(self, script: str) -> str:
        """
        Run a PowerShell snippet and return its stdout.

        Parameters
        ----------
        script
            PowerShell code; it can assume the ``PasswordVault`` WinRT type is
            loaded in ``$vault``.

        Returns
        -------
        str
            Stripped standard output.

        Raises
        ------
        KeyringError
            If PowerShell fails or times out.
        """
        program = (
            "[void][Windows.Security.Credentials.PasswordVault,"
            "Windows.Security.Credentials,ContentType=WindowsRuntime]\n"
            "$vault = New-Object Windows.Security.Credentials.PasswordVault\n"
            "$ErrorActionPreference = 'Stop'\n" + script
        )
        encoded = base64.b64encode(program.encode("utf-16-le")).decode()
        try:
            proc = subprocess.run(  # noqa: S603
                [
                    self.powershell or find_powershell() or "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                text=True,
                timeout=POWERSHELL_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            msg = f"Could not run powershell.exe: {e}"
            raise KeyringError(msg) from e

        if proc.returncode != 0:
            msg = f"PowerShell failed ({proc.returncode}): {proc.stderr.strip()}"
            raise KeyringError(msg)

        return proc.stdout.strip()

    def get_password(self, service: str, username: str) -> str | None:
        """
        Read a credential from the vault.

        Returns
        -------
        str | None
            The stored password, or ``None`` if there is none.
        """
        out = self._run(
            "try {\n"
            f"  $c = $vault.Retrieve({_ps_string(service)}, {_ps_string(username)})\n"
            "  $c.RetrievePassword()\n"
            "  ConvertTo-Json -Compress @{ found = $true; password = $c.Password }\n"
            "} catch {\n"
            "  ConvertTo-Json -Compress @{ found = $false }\n"
            "}"
        )
        data = json.loads(out)
        return data["password"] if data["found"] else None

    def set_password(self, service: str, username: str, password: str) -> None:
        """Write (or overwrite) a credential in the vault."""
        self._run(
            "$vault.Add((New-Object Windows.Security.Credentials.PasswordCredential("
            f"{_ps_string(service)}, {_ps_string(username)}, {_ps_string(password)})))"
        )

    def delete_password(self, service: str, username: str) -> None:
        """
        Remove a credential from the vault.

        Raises
        ------
        PasswordDeleteError
            If there was no such credential.
        """
        out = self._run(
            "try {\n"
            f"  $c = $vault.Retrieve({_ps_string(service)}, {_ps_string(username)})\n"
            "  $vault.Remove($c)\n"
            "  'deleted'\n"
            "} catch {\n"
            "  'missing'\n"
            "}"
        )
        if out != "deleted":
            msg = f"No credential for {service}/{username}"
            raise PasswordDeleteError(msg)
