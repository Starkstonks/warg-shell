import argparse
import asyncio
import json
import os
import signal
import sys
import termios
import tty

import websockets


class WargShell:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.loop = asyncio.get_event_loop()
        self.stdin_fd = sys.stdin.fileno()
        self.old_tty_attrs = termios.tcgetattr(self.stdin_fd)
        self.resize_event = asyncio.Event()

    async def connect_tty(self):
        try:
            self.set_raw_terminal()
            async with websockets.connect(self.ws_url) as ws:
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
        except Exception as e:
            print(f"Connection error: {e}")
        finally:
            self.restore_terminal()

    def set_raw_terminal(self):
        # Set terminal to raw mode
        tty.setraw(self.stdin_fd)
        # Disable echo
        attrs = termios.tcgetattr(self.stdin_fd)
        attrs[3] = attrs[3] & ~termios.ECHO
        termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, attrs)
        # Set resize signal handler
        signal.signal(signal.SIGWINCH, self.on_resize)

    def restore_terminal(self):
        # Restore terminal settings
        termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self.old_tty_attrs)
        # Reset signal handler
        signal.signal(signal.SIGWINCH, signal.SIG_DFL)

    def on_resize(self, signum, frame):
        self.resize_event.set()

    async def send_resize(self, ws):
        # Send initial window size
        rows, cols = self.get_terminal_size()
        resize_msg = {"op": "resize", "height": rows, "width": cols}
        await ws.send(json.dumps(resize_msg))

    def get_terminal_size(self):
        # Return terminal (rows, cols)
        try:
            size = os.get_terminal_size(self.stdin_fd)
            return size.lines, size.columns
        except OSError:
            return 24, 80  # Default if unable to get size

    async def stdin_to_ws(self, ws):
        loop = self.loop
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)

        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        try:
            while True:
                data = await reader.read(1024)

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
        try:
            async for message in ws:
                data = json.loads(message)
                if data["op"] == "stdout":
                    sys.stdout.write(data["data"])
                    sys.stdout.flush()
        except (
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.ConnectionClosedError,
            asyncio.CancelledError,
        ):
            pass

    async def handle_resize(self, ws):
        try:
            while True:
                await self.resize_event.wait()
                self.resize_event.clear()
                rows, cols = self.get_terminal_size()
                resize_msg = {"op": "resize", "height": rows, "width": cols}
                await ws.send(json.dumps(resize_msg))
        except (
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.ConnectionClosedError,
        ):
            pass
        except asyncio.CancelledError:
            pass


async def main():
    parser = argparse.ArgumentParser(
        description="Connect to remote shell via WebSocket."
    )
    parser.add_argument("ws_url", help="WebSocket URL to connect to.")
    args = parser.parse_args()

    warg = WargShell(ws_url=args.ws_url)
    await warg.connect_tty()


if __name__ == "__main__":
    asyncio.run(main())
