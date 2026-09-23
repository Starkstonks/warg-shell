"""
Tests for the websocket <-> terminal bridge, against a real websocket server.

There is no TTY in CI, so the terminal side is faked: stdin is a pipe we
write into, stdout is read through pytest's capture, and the ``termios``
calls are neutralised.
"""

import asyncio
import os
import termios
import tty

import pytest

from warg_shell._warg import WargShell


class FakeTerminal:
    """Fake stdin (a pipe) and stdout (pytest's capture) for ``WargShell``."""

    def __init__(self, capsys):
        read_fd, self.write_fd = os.pipe()
        self.stdin = os.fdopen(read_fd, "rb", buffering=0)
        self._capsys = capsys
        self._out = ""

    def type(self, text: str) -> None:
        """Simulate the user typing ``text``."""
        os.write(self.write_fd, text.encode())

    def close_stdin(self) -> None:
        """Simulate an EOF on stdin (Ctrl-D, closed pipe)."""
        os.close(self.write_fd)

    def output(self) -> str:
        """Everything written to stdout so far."""
        self._out += self._capsys.readouterr().out
        return self._out

    async def wait_for(self, text: str) -> None:
        """Poll (at most 5s) until ``text`` shows up; capsys has no event."""
        async with asyncio.timeout(5):
            while True:
                if text in self.output():
                    return
                await asyncio.sleep(0.01)


@pytest.fixture
def fake_terminal(monkeypatch, capsys) -> FakeTerminal:
    """
    Replace the process' terminal with a controllable fake.

    Returns
    -------
    FakeTerminal
        Handle to type into stdin and read stdout.
    """
    term = FakeTerminal(capsys)
    monkeypatch.setattr("sys.stdin", term.stdin)
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: [0, 0, 0, 0, 0, 0, []])
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, attrs: None)
    monkeypatch.setattr(tty, "setraw", lambda fd: None)
    return term


async def test_shell_session_round_trip(fake_warg, fake_terminal):
    """Size is announced, remote output is printed and keystrokes are sent."""
    warg = WargShell(fake_warg.url)

    async def drive():
        await fake_terminal.wait_for("Welcome")
        fake_terminal.type("ls\n")
        await fake_terminal.wait_for("echo: ls")
        fake_terminal.type("exit\n")

    await asyncio.wait_for(asyncio.gather(warg.connect_tty(), drive()), timeout=5)

    assert fake_warg.received[0] == {"op": "resize", "height": 24, "width": 80}
    assert {"op": "stdin", "data": "ls\n"} in fake_warg.received
    assert {"op": "stdin", "data": "exit\n"} in fake_warg.received
    assert fake_terminal.output() == "Welcome to the fake console\r\necho: ls\n"


async def test_closing_stdin_ends_the_session(fake_warg, fake_terminal):
    """EOF on stdin terminates the session instead of hanging."""
    warg = WargShell(fake_warg.url)

    async def drive():
        await fake_terminal.wait_for("Welcome")
        fake_terminal.close_stdin()

    await asyncio.wait_for(asyncio.gather(warg.connect_tty(), drive()), timeout=5)

    assert fake_warg.connections == 1


async def test_resize_event_sends_new_size(fake_warg, fake_terminal, monkeypatch):
    """A resize notification pushes the new size to the remote."""
    warg = WargShell(fake_warg.url)
    monkeypatch.setattr(warg, "get_terminal_size", lambda: (50, 120))

    async def drive():
        await fake_terminal.wait_for("Welcome")
        warg.on_resize(None, None)
        await fake_warg.wait_for_messages(2)
        fake_terminal.type("exit\n")

    await asyncio.wait_for(asyncio.gather(warg.connect_tty(), drive()), timeout=5)

    assert fake_warg.received[1] == {"op": "resize", "height": 50, "width": 120}


async def test_connection_error_is_reported_and_terminal_restored(
    fake_terminal, monkeypatch, capsys
):
    """A failed connection is reported and the terminal is never left raw."""
    restored = []
    monkeypatch.setattr(
        termios, "tcsetattr", lambda fd, when, attrs: restored.append(attrs)
    )
    warg = WargShell("ws://127.0.0.1:1/nothing-listens-here")

    await asyncio.wait_for(warg.connect_tty(), timeout=5)

    assert "Connection error" in capsys.readouterr().err
    assert restored[-1] == warg.old_tty_attrs
