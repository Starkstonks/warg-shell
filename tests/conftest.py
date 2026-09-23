"""
Shared fixtures for the test suite.

The remote side is not mocked at the HTTP-client level: a real HTTP server
(faking the Jon API) and a real websocket server (faking a Warg console) are
started on localhost, so that the actual network code is exercised. The
keyring is swapped for an in-memory backend.
"""

import asyncio
import json
import socketserver
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import keyring
import keyring.backend
import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

PG_DUMP_FOOTER = b"\n\n--\n-- PostgreSQL database dump complete\n--\n\n"


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """Keyring backend that keeps everything in a dict, for tests."""

    priority = 1  # type: ignore[assignment]

    def __init__(self):
        super().__init__()
        self.storage: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        """Return the stored password or ``None``."""
        return self.storage.get((service, username))

    def set_password(self, service, username, password):
        """Store the password."""
        self.storage[(service, username)] = password

    def delete_password(self, service, username):
        """Forget the password."""
        del self.storage[(service, username)]


@pytest.fixture
def memory_keyring() -> Iterator[InMemoryKeyring]:
    """
    Swap the process-wide keyring for an in-memory one during the test.

    Yields
    ------
    InMemoryKeyring
        The backend, so tests can seed or inspect ``storage``.
    """
    previous = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@dataclass
class FakeJon:
    """State and knobs of the fake Jon server, mutable from the tests."""

    req_token: str = "request-token"
    auth_token: str = "auth-token"
    valid_until: datetime = field(
        default_factory=lambda: datetime.now(UTC) + timedelta(days=1)
    )
    shell_url: str = "ws://127.0.0.1:1/console"
    components: set[tuple[str, str, str]] = field(
        default_factory=lambda: {("product", "env", "component")}
    )
    dbs: set[tuple[str, str, str]] = field(
        default_factory=lambda: {("product", "env", "db")}
    )
    pg_dump_chunks: list[bytes] = field(
        default_factory=lambda: [b"-- header\n", b"CREATE TABLE foo();", PG_DUMP_FOOTER]
    )
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    port: int = 0

    @property
    def domain(self) -> str:
        """Domain argument to pass to the CLI to reach this server."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def stored_auth(self) -> str:
        """What the CLI stores in the keyring after a successful ``auth``."""
        return json.dumps(
            {"token": self.auth_token, "valid_until": self.valid_until.isoformat()}
        )


class _QuietHTTPServer(ThreadingHTTPServer):
    """HTTP server that skips the reverse DNS lookup of ``server_bind``.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn()``, which can stall
    for tens of seconds on hosts without reverse DNS (macOS CI runners).
    """

    def server_bind(self):
        """Bind without resolving our own name."""
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class _JsonHandler(BaseHTTPRequestHandler):
    """Quiet HTTP/1.1 handler with JSON and chunked response helpers."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        """Keep the test output quiet."""

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _chunked(self, chunks: list[bytes]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


def _make_handler(state: FakeJon) -> type[BaseHTTPRequestHandler]:
    """
    Build a request handler class bound to the given fake state.

    Parameters
    ----------
    state
        Shared state that the handler reads its answers from and records
        the received requests into.

    Returns
    -------
    type[BaseHTTPRequestHandler]
        Handler class to give to the HTTP server.
    """

    class Handler(_JsonHandler):
        """Implements the three Jon endpoints used by warg-shell."""

        def _token(self, body: dict) -> None:
            if body.get("token") != state.req_token:
                self._json(HTTPStatus.FORBIDDEN, {"detail": "Forbidden"})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "token": state.auth_token,
                    "valid_until": state.valid_until.isoformat(),
                },
            )

        def _shell(self, body: dict) -> None:
            key = (body.get("product"), body.get("env"), body.get("component"))
            if key not in state.components:
                self._json(HTTPStatus.NOT_FOUND, {"detail": "Component not found"})
                return
            self._json(HTTPStatus.OK, {"url": state.shell_url})

        def _pg_dump(self, body: dict) -> None:
            key = (body.get("product"), body.get("env"), body.get("db"))
            if key not in state.dbs:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Database not found"})
                return
            self._chunked(state.pg_dump_chunks)

        def do_POST(self):
            """Dispatch to the endpoint, checking the auth token first."""
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            state.requests.append((self.path, body))

            # (handler, needs the auth token)
            routes = {
                "/back/api/warg/token/": (self._token, False),
                "/back/api/warg/shell/": (self._shell, True),
                "/back/api/warg/pg_dump/": (self._pg_dump, True),
            }

            if (route := routes.get(self.path)) is None:
                self._json(HTTPStatus.NOT_FOUND, {"detail": "Not found"})
                return

            handler, needs_auth = route

            if needs_auth and body.get("token") != state.auth_token:
                self._json(HTTPStatus.FORBIDDEN, {"detail": "Forbidden"})
            else:
                handler(body)

    return Handler


@pytest.fixture(scope="session")
def _jon_server() -> Iterator[FakeJon]:
    """
    Run a real HTTP server faking the Jon API on a random localhost port.

    The server is shared across the whole session because ``shutdown()``
    costs half a second (its poll interval); state is reset per test in
    ``fake_jon``.

    Yields
    ------
    FakeJon
        The state object bound to the running server.
    """
    state = FakeJon()
    server = _QuietHTTPServer(("127.0.0.1", 0), _make_handler(state))
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def fake_jon(_jon_server: FakeJon) -> FakeJon:
    """
    Fresh fake Jon state for each test, on the shared server.

    Returns
    -------
    FakeJon
        State with default values; tests can tweak it before calling the CLI.
    """
    fresh = FakeJon(port=_jon_server.port)
    for name in fresh.__dataclass_fields__:
        setattr(_jon_server, name, getattr(fresh, name))
    return _jon_server


@dataclass
class FakeWarg:
    """State and knobs of the fake Warg websocket console."""

    url: str = ""
    received: list[dict[str, Any]] = field(default_factory=list)
    greeting: str = "Welcome to the fake console\r\n"
    connections: int = 0
    message_received: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait_for_messages(self, count: int) -> None:
        """Block (at most 5s) until ``count`` messages have been received."""
        async with asyncio.timeout(5):
            while len(self.received) < count:
                self.message_received.clear()
                await self.message_received.wait()

    async def handler(self, ws: ServerConnection) -> None:
        """
        Behave like a tiny console.

        Greets on the first ``resize``, echoes every ``stdin`` back as
        ``stdout`` and closes the connection when ``exit`` is typed.
        """
        self.connections += 1
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                self.message_received.set()
                if msg["op"] == "resize" and len(self.received) == 1:
                    await ws.send(json.dumps({"op": "stdout", "data": self.greeting}))
                elif msg["op"] == "stdin":
                    if "exit" in msg["data"]:
                        await ws.close()
                        return
                    await ws.send(
                        json.dumps({"op": "stdout", "data": f"echo: {msg['data']}"})
                    )
        except websockets.exceptions.ConnectionClosed:
            pass


@pytest.fixture
async def fake_warg() -> AsyncIterator[FakeWarg]:
    """
    Run a real websocket server faking a Warg console on a random port.

    Yields
    ------
    FakeWarg
        State with the ``url`` to connect to and the messages received.
    """
    state = FakeWarg()
    async with serve(state.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        state.url = f"ws://127.0.0.1:{port}/console"
        yield state
