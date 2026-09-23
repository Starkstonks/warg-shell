"""
Tests for the WSL keyring backend.

The PowerShell bridge itself is exercised for real on Windows (the CI runner
has ``powershell.exe`` and a Credential Manager, exactly like the Windows side
of a WSL setup). The WSL detection and the CLI error path run everywhere.
"""

import json
import shutil
import sys
import uuid

import keyring.backend
import keyring.errors
import pytest
from click.testing import CliRunner

from warg_shell import _keyring
from warg_shell._cli import main
from warg_shell._keyring import WslCredentialManager, find_powershell, is_wsl


@pytest.fixture
def _clear_caches():
    """``find_powershell`` is cached; tests poking at the environment need it fresh."""
    find_powershell.cache_clear()
    yield
    find_powershell.cache_clear()


@pytest.mark.usefixtures("_clear_caches")
def test_is_wsl_reads_osrelease(monkeypatch, tmp_path):
    """WSL is recognised from the kernel release string."""
    osrelease = tmp_path / "osrelease"
    monkeypatch.setattr(_keyring.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_keyring, "Path", lambda _: osrelease)

    osrelease.write_text("5.15.153.1-microsoft-standard-WSL2\n")
    assert is_wsl()

    osrelease.write_text("6.8.0-45-generic\n")
    assert not is_wsl()


def test_is_wsl_false_outside_linux(monkeypatch):
    """Native Windows and macOS are never WSL."""
    monkeypatch.setattr(_keyring.platform, "system", lambda: "Darwin")
    assert not is_wsl()


@pytest.mark.usefixtures("_clear_caches")
def test_backend_not_viable_outside_wsl(monkeypatch):
    """The backend must not get picked up on regular systems."""
    monkeypatch.setattr(_keyring, "is_wsl", lambda: False)
    assert not WslCredentialManager.viable


@pytest.mark.usefixtures("_clear_caches")
def test_backend_not_viable_without_powershell(monkeypatch):
    """Under WSL without interop, the backend steps aside."""
    monkeypatch.setattr(_keyring, "is_wsl", lambda: True)
    monkeypatch.setattr(_keyring.shutil, "which", lambda _: None)
    monkeypatch.setattr(_keyring, "Path", lambda _: _MissingPath())
    assert not WslCredentialManager.viable


@pytest.mark.usefixtures("_clear_caches")
def test_backend_viable_under_wsl(monkeypatch):
    """Under WSL with interop, the backend ranks just below native ones."""
    monkeypatch.setattr(_keyring, "is_wsl", lambda: True)
    monkeypatch.setattr(_keyring.shutil, "which", lambda _: "/mnt/c/powershell.exe")
    assert WslCredentialManager.priority == 4


def test_backend_is_registered_as_plugin():
    """The entry point lets ``keyring`` discover the backend by itself."""
    keyring.backend._load_plugins()
    assert WslCredentialManager in keyring.backend.KeyringBackend._classes


class _MissingPath:
    """Stand-in for a ``Path`` that does not exist."""

    def is_file(self) -> bool:
        return False

    def read_text(self) -> str:
        raise FileNotFoundError


def _raise_no_keyring(*args):
    msg = "No recommended backend was available"
    raise keyring.errors.NoKeyringError(msg)


def test_auth_explains_missing_keyring(fake_jon, monkeypatch):
    """A successful auth with no backend gives advice, not a traceback."""
    monkeypatch.setattr(keyring, "set_password", _raise_no_keyring)
    monkeypatch.setattr(_keyring, "is_wsl", lambda: False)

    result = CliRunner().invoke(main, ["auth", fake_jon.domain, fake_jon.req_token])

    assert result.exit_code == 1
    assert "cannot be stored" in result.output
    assert "No keyring backend" in result.output


def test_shell_explains_missing_powershell_under_wsl(fake_jon, monkeypatch):
    """Under WSL without interop, the advice points at wsl.conf."""
    monkeypatch.setattr(keyring, "get_password", _raise_no_keyring)
    monkeypatch.setattr(_keyring, "is_wsl", lambda: True)
    monkeypatch.setattr(_keyring, "find_powershell", lambda: None)

    result = CliRunner().invoke(
        main, ["shell", fake_jon.domain, "product", "env", "component"]
    )

    assert result.exit_code == 1
    assert "powershell.exe" in result.output
    assert "wsl.conf" in result.output


needs_powershell = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="needs a Windows Credential Manager and powershell.exe",
)


@needs_powershell
def test_powershell_bridge_round_trip():
    """Set, get, overwrite and delete through the real Credential Manager."""
    ring = WslCredentialManager()
    ring.powershell = shutil.which("powershell.exe")
    service = f"warg-shell-test-{uuid.uuid4()}"
    user = "https://warg.example.com"
    payload = json.dumps({"token": "sécrét 'quoted' \"double\"", "valid_until": "x"})

    try:
        assert ring.get_password(service, user) is None

        ring.set_password(service, user, payload)
        assert ring.get_password(service, user) == payload

        ring.set_password(service, user, "second")
        assert ring.get_password(service, user) == "second"
    finally:
        ring.delete_password(service, user)

    assert ring.get_password(service, user) is None
    with pytest.raises(keyring.errors.PasswordDeleteError):
        ring.delete_password(service, user)
