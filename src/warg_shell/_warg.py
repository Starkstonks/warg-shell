"""Bridge between the local terminal and a Warg console websocket."""

import argparse
import asyncio
import json
import sys

import websockets

from ._terminal import Terminal, make_terminal


class WargShell:
    """Pipe the local terminal to a remote console over a websocket.

    The protocol is a stream of JSON messages: ``{"op": "stdin", "data": ...}``
    and ``{"op": "resize", "height": ..., "width": ...}`` going up,
    ``{"op": "stdout", "data": ...}`` coming down.
    """

    def __init__(self, ws_url: str, terminal: Terminal | None = None):
        self.ws_url = ws_url
        self.loop = asyncio.get_event_loop()
        self.terminal = terminal or make_terminal(self.loop)

    async def connect_tty(self):
        """Run the session until the remote closes or stdin hits EOF."""
        try:
            self.terminal.enter_raw()
            # A console can legitimately send a huge blob in one message
            # (``cat`` of a big file), so don't let the default 1 MiB limit
            # kill the session.
            async with websockets.connect(self.ws_url, max_size=None) as ws:
                await self.send_resize(ws)
                tasks = [
                    self.loop.create_task(self.stdin_to_ws(ws)),
                    self.loop.create_task(self.ws_to_stdout(ws)),
                    self.loop.create_task(self.handle_resize(ws)),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                # Surface whatever made the first task stop instead of
                # dying silently.
                for task in done:
                    task.result()
        except websockets.exceptions.ConnectionClosedError as e:
            error = f"Connection closed by the remote: {e}\n"
        except Exception as e:
            error = f"Connection error: {e}\n"
        else:
            error = None
        finally:
            self.terminal.restore()

        if error:
            sys.stderr.write(error)

    def on_resize(self, signum=None, frame=None):
        """Signal-handler-compatible hook flagging that the size changed."""
        self.terminal.resize_event.set()

    def get_terminal_size(self) -> tuple[int, int]:
        """
        Measure the local terminal.

        Returns
        -------
        tuple[int, int]
            ``(rows, cols)`` as expected by the ``resize`` message.
        """
        return self.terminal.size()

    async def send_resize(self, ws):
        """Tell the remote what size the local terminal is."""
        rows, cols = self.get_terminal_size()
        resize_msg = {"op": "resize", "height": rows, "width": cols}
        await ws.send(json.dumps(resize_msg))

    async def stdin_to_ws(self, ws):
        """Forward keystrokes to the remote until EOF."""
        try:
            while True:
                data = await self.terminal.read_stdin()

                if not data:
                    break

                stdin_msg = {
                    "op": "stdin",
                    "data": data.decode("utf-8", errors="replace"),
                }

                await ws.send(json.dumps(stdin_msg))
        except (
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.ConnectionClosedError,
        ):
            pass
        except asyncio.CancelledError:
            pass

    async def ws_to_stdout(self, ws):
        """Print whatever the remote sends; an abnormal close propagates."""
        try:
            async for message in ws:
                data = json.loads(message)
                if data["op"] == "stdout":
                    self.terminal.write_stdout(data["data"])
        except (websockets.exceptions.ConnectionClosedOK, asyncio.CancelledError):
            pass

    async def handle_resize(self, ws):
        """Push the new size to the remote whenever the window is resized."""
        try:
            while True:
                await self.terminal.wait_resize()
                await self.send_resize(ws)
        except (
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.ConnectionClosedError,
        ):
            pass
        except asyncio.CancelledError:
            pass


async def main():
    """Connect straight to a websocket URL, bypassing Jon (debug helper)."""
    parser = argparse.ArgumentParser(
        description="Connect to remote shell via WebSocket."
    )
    parser.add_argument("ws_url", help="WebSocket URL to connect to.")
    args = parser.parse_args()

    warg = WargShell(ws_url=args.ws_url)
    await warg.connect_tty()


if __name__ == "__main__":
    asyncio.run(main())
