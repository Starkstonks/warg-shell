"""
Tests for the websocket <-> terminal bridge, against a real websocket server.

There is no TTY in CI, so the terminal is faked at the :class:`Terminal`
boundary: stdin is a pipe we write into and stdout is read through pytest's
capture. On POSIX the real :class:`PosixTerminal` is also exercised on top of
that pipe, with the ``termios`` calls neutralised.
"""

import asyncio
import os
import sys

import pytest

from warg_shell._terminal import Terminal, ThreadedStdinMixin, make_terminal
from warg_shell._warg import WargShell


class PipeTerminal(ThreadedStdinMixin, Terminal):
    """
    Terminal reading stdin from a pipe, no raw mode, no resize watching.

    Reads happen in a daemon thread so this works on every event loop,
    including the Windows Proactor which cannot ``add_reader`` a pipe.
    """

    def __init__(self):
        super().__init__()
        self._init_threaded_stdin()

    def enter_raw(self) -> None:
        """Nothing to do on a pipe."""

    def restore(self) -> None:
        """Nothing to undo."""


class FakeTty:
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
def fake_tty(monkeypatch, capsys) -> FakeTty:
    """
    Replace the process' stdin with a pipe and expose captured stdout.

    Returns
    -------
    FakeTty
        Handle to type into stdin and read stdout.
    """
    tty = FakeTty(capsys)
    monkeypatch.setattr("sys.stdin", tty.stdin)
    return tty


@pytest.fixture(params=["pipe", "native"])
def terminal(request, fake_tty, monkeypatch) -> Terminal | None:
    """
    Terminal implementation to run the bridge on.

    ``pipe`` is the portable fake; ``native`` is the platform's real
    implementation running on the pipe, with the raw-mode calls neutralised
    (there is no TTY in CI). ``native`` is skipped on Windows since the
    console API cannot be driven from a pipe.

    Returns
    -------
    Terminal | None
        The instance to inject, or ``None`` to let ``WargShell`` build the
        native one.
    """
    if request.param == "pipe":
        return PipeTerminal()

    if sys.platform == "win32":
        pytest.skip("Windows console modes cannot be tested on a pipe")

    import termios
    import tty

    monkeypatch.setattr(termios, "tcgetattr", lambda fd: [0, 0, 0, 0, 0, 0, []])
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, attrs: None)
    monkeypatch.setattr(tty, "setraw", lambda fd: None)
    return None


async def test_shell_session_round_trip(fake_warg, fake_tty, terminal):
    """Size is announced, remote output is printed and keystrokes are sent."""
    warg = WargShell(fake_warg.url, terminal)

    async def drive():
        await fake_tty.wait_for("Welcome")
        fake_tty.type("ls\n")
        await fake_tty.wait_for("echo: ls")
        fake_tty.type("exit\n")

    await asyncio.wait_for(asyncio.gather(warg.connect_tty(), drive()), timeout=5)

    assert fake_warg.received[0] == {"op": "resize", "height": 24, "width": 80}
    assert {"op": "stdin", "data": "ls\n"} in fake_warg.received
    assert {"op": "stdin", "data": "exit\n"} in fake_warg.received
    assert fake_tty.output() == "Welcome to the fake console\r\necho: ls\n"


async def test_closing_stdin_ends_the_session(fake_warg, fake_tty, terminal):
    """EOF on stdin terminates the session instead of hanging."""
    warg = WargShell(fake_warg.url, terminal)

    async def drive():
        await fake_tty.wait_for("Welcome")
        fake_tty.close_stdin()

    await asyncio.wait_for(asyncio.gather(warg.connect_tty(), drive()), timeout=5)

    assert fake_warg.connections == 1


async def test_resize_event_sends_new_size(fake_warg, fake_tty, terminal, monkeypatch):
    """A resize notification pushes the new size to the remote."""
    warg = WargShell(fake_warg.url, terminal)
    monkeypatch.setattr(warg, "get_terminal_size", lambda: (50, 120))

    async def drive():
        await fake_tty.wait_for("Welcome")
        warg.on_resize()
        await fake_warg.wait_for_messages(2)
        fake_tty.type("exit\n")

    await asyncio.wait_for(asyncio.gather(warg.connect_tty(), drive()), timeout=5)

    assert fake_warg.received[1] == {"op": "resize", "height": 50, "width": 120}


async def test_connection_error_is_reported_and_terminal_restored(
    fake_tty, monkeypatch, capsys
):
    """A failed connection is reported and the terminal is never left raw."""
    terminal = PipeTerminal()
    events = []
    monkeypatch.setattr(terminal, "enter_raw", lambda: events.append("raw"))
    monkeypatch.setattr(terminal, "restore", lambda: events.append("restored"))
    warg = WargShell("ws://127.0.0.1:1/nothing-listens-here", terminal)

    await asyncio.wait_for(warg.connect_tty(), timeout=5)

    assert "Connection error" in capsys.readouterr().err
    assert events == ["raw", "restored"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal only")
def test_posix_terminal_restores_attributes(fake_tty, monkeypatch):
    """The original ``termios`` attributes are put back on ``restore()``."""
    import termios
    import tty

    original = [1, 2, 3, 4, 5, 6, []]
    calls = []
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: list(original))
    monkeypatch.setattr(
        termios, "tcsetattr", lambda fd, when, attrs: calls.append(attrs)
    )
    monkeypatch.setattr(tty, "setraw", lambda fd: None)

    async def run():
        terminal = make_terminal()
        terminal.enter_raw()
        terminal.restore()

    asyncio.run(run())

    assert calls[-1] == original
    assert calls[0][3] & termios.ECHO == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows terminal only")
def test_windows_terminal_without_console(fake_tty):
    """Without an attached console, raw mode is a no-op and nothing crashes."""

    async def run():
        terminal = make_terminal()
        terminal.enter_raw()
        assert terminal.size() == (24, 80)
        terminal.restore()

    asyncio.run(run())
