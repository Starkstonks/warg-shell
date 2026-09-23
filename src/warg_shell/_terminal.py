"""Platform-specific handling of the local terminal.

The websocket bridge in :mod:`warg_shell._warg` only needs a handful of
operations from the local terminal: put it in raw mode and restore it, know
its size, read what the user types and be told when the window is resized.
Those operations are wildly different between POSIX (``termios``, ``SIGWINCH``)
and Windows (console modes through ``kernel32``), hence this module.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from abc import ABC, abstractmethod

DEFAULT_SIZE = (24, 80)


class Terminal(ABC):
    """What the websocket bridge needs from the local terminal."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        self.loop = loop or asyncio.get_event_loop()
        self.stdin_fd = sys.stdin.fileno()
        self.resize_event = asyncio.Event()

    @abstractmethod
    def enter_raw(self) -> None:
        """Switch the terminal to raw mode and start watching for resizes."""

    @abstractmethod
    def restore(self) -> None:
        """Undo whatever :meth:`enter_raw` did."""

    @abstractmethod
    async def read_stdin(self) -> bytes:
        """Wait for the user to type something; ``b""`` means EOF."""

    def size(self) -> tuple[int, int]:
        """
        Measure the terminal.

        Returns
        -------
        tuple[int, int]
            ``(rows, cols)``, or 24x80 when there is no terminal to measure
            (pipes, CI).
        """
        try:
            s = os.get_terminal_size(self.stdin_fd)
        except (OSError, ValueError):
            return DEFAULT_SIZE
        return s.lines, s.columns

    def write_stdout(self, text: str) -> None:
        """
        Print remote output as-is, escape sequences included.

        Parameters
        ----------
        text
            Decoded ``stdout`` payload from the remote.
        """
        sys.stdout.write(text)
        sys.stdout.flush()

    async def wait_resize(self) -> None:
        """Block until the terminal has been resized."""
        await self.resize_event.wait()
        self.resize_event.clear()


class ThreadedStdinMixin:
    """
    Read stdin from a daemon thread and hand the bytes to the event loop.

    For loops that cannot watch the stdin descriptor (Windows Proactor, or a
    plain pipe in tests). The thread is a daemon so a read blocked forever
    after the session ended does not keep the process alive.
    """

    loop: asyncio.AbstractEventLoop

    def _init_threaded_stdin(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._thread: threading.Thread | None = None

    def _read_blocking(self) -> bytes:
        """Blocking read of up to 1 KiB; ``b""`` means EOF."""
        return os.read(sys.stdin.fileno(), 1024)

    def _pump_stdin(self) -> None:
        while True:
            try:
                data = self._read_blocking()
            except OSError:
                data = b""
            self.loop.call_soon_threadsafe(self._queue.put_nowait, data)
            if not data:
                return

    async def read_stdin(self) -> bytes:
        """Wait for the next chunk typed by the user."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._pump_stdin, daemon=True)
            self._thread.start()
        return await self._queue.get()

    def join_stdin_thread(self, timeout: float) -> None:
        """
        Wait for the reader thread to notice EOF and exit.

        Closing stdin while the thread is blocked reading it hangs on
        Windows, so callers must make stdin hit EOF first, then call this.
        """
        if self._thread is not None:
            self._thread.join(timeout)


class PosixTerminal(Terminal):
    """
    Terminal handling for Linux, macOS and other Unixes.

    Raw mode goes through ``termios``, resizes through ``SIGWINCH`` and stdin
    through the event loop's readiness API, which (unlike
    ``connect_read_pipe``) leaves the tty in blocking mode. Since stdin and
    stdout share the same tty, making stdin non-blocking would make big
    ``stdout`` writes fail with ``EAGAIN``.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        super().__init__(loop)
        import termios

        self._termios = termios
        self._old_attrs = termios.tcgetattr(self.stdin_fd)
        self._reader = asyncio.StreamReader()
        self._watching = False

    def enter_raw(self) -> None:
        import signal
        import tty

        tty.setraw(self.stdin_fd)
        attrs = self._termios.tcgetattr(self.stdin_fd)
        attrs[3] = attrs[3] & ~self._termios.ECHO
        self._termios.tcsetattr(self.stdin_fd, self._termios.TCSADRAIN, attrs)
        self.loop.add_signal_handler(signal.SIGWINCH, self.resize_event.set)

    def restore(self) -> None:
        import signal

        self._termios.tcsetattr(self.stdin_fd, self._termios.TCSADRAIN, self._old_attrs)
        self.loop.remove_signal_handler(signal.SIGWINCH)
        if self._watching:
            self.loop.remove_reader(self.stdin_fd)
            self._watching = False

    def _on_readable(self) -> None:
        try:
            data = os.read(self.stdin_fd, 1024)
        except BlockingIOError:
            return
        if data:
            self._reader.feed_data(data)
        else:
            self.loop.remove_reader(self.stdin_fd)
            self._watching = False
            self._reader.feed_eof()

    async def read_stdin(self) -> bytes:
        if not self._watching and not self._reader.at_eof():
            self.loop.add_reader(self.stdin_fd, self._on_readable)
            self._watching = True
        return await self._reader.read(1024)


# Console mode flags, see SetConsoleMode in the Windows console docs.
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
DISABLE_NEWLINE_AUTO_RETURN = 0x0008
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11


class WindowsTerminal(ThreadedStdinMixin, Terminal):
    """
    Terminal handling for Windows.

    Raw mode goes through the console API (``SetConsoleMode``), resizes are
    detected by polling since there is no ``SIGWINCH``, and stdin is read from
    a daemon thread because the Proactor loop cannot watch a console handle.

    Every console call is best-effort: without an attached console (CI, pipes)
    the modes are simply left alone and the bridge still works on the streams.
    """

    resize_poll_interval = 0.25

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        super().__init__(loop)
        import ctypes

        self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._ctypes = ctypes
        self._old_modes: dict[int, int] = {}
        self._resize_task: asyncio.Task | None = None
        self._init_threaded_stdin()

    def _get_mode(self, std_handle: int) -> tuple[int, int] | None:
        handle = self._kernel32.GetStdHandle(std_handle)
        mode = self._ctypes.c_uint32()
        if not self._kernel32.GetConsoleMode(handle, self._ctypes.byref(mode)):
            return None
        return handle, mode.value

    def _set_mode(self, std_handle: int, clear: int, add: int) -> None:
        if (current := self._get_mode(std_handle)) is None:
            return
        handle, mode = current
        self._old_modes.setdefault(std_handle, mode)
        self._kernel32.SetConsoleMode(handle, (mode & ~clear) | add)

    def enter_raw(self) -> None:
        self._set_mode(
            STD_INPUT_HANDLE,
            clear=ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT | ENABLE_PROCESSED_INPUT,
            add=ENABLE_VIRTUAL_TERMINAL_INPUT,
        )
        self._set_mode(
            STD_OUTPUT_HANDLE,
            clear=0,
            add=ENABLE_VIRTUAL_TERMINAL_PROCESSING | DISABLE_NEWLINE_AUTO_RETURN,
        )
        self._resize_task = self.loop.create_task(self._poll_resize())

    def restore(self) -> None:
        for std_handle, mode in self._old_modes.items():
            handle = self._kernel32.GetStdHandle(std_handle)
            self._kernel32.SetConsoleMode(handle, mode)
        self._old_modes.clear()
        if self._resize_task is not None:
            self._resize_task.cancel()
            self._resize_task = None

    async def _poll_resize(self) -> None:
        last = self.size()
        while True:
            await asyncio.sleep(self.resize_poll_interval)
            current = self.size()
            if current != last:
                last = current
                self.resize_event.set()

    def _read_blocking(self) -> bytes:
        raw = getattr(getattr(sys.stdin, "buffer", None), "raw", None)
        if raw is not None and type(raw).__name__ == "_WindowsConsoleIO":
            # ReadConsoleW honours the console mode (VT input, no line
            # buffering) and gives us proper UTF-8, unlike os.read().
            return raw.read(1024) or b""
        return os.read(self.stdin_fd, 1024)


def make_terminal(loop: asyncio.AbstractEventLoop | None = None) -> Terminal:
    """
    Pick the right :class:`Terminal` for the current platform.

    Parameters
    ----------
    loop
        Event loop to schedule callbacks on; defaults to the current one.

    Returns
    -------
    Terminal
        A :class:`WindowsTerminal` on Windows, a :class:`PosixTerminal`
        everywhere else.
    """
    if sys.platform == "win32":
        return WindowsTerminal(loop)
    return PosixTerminal(loop)
