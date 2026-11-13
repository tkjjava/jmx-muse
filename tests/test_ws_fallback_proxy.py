"""Stdlib fallback CONNECT/TLS bootstrap; controlled streams, no public network."""
import asyncio
import ssl

import pytest

from muse_bridge import ws_fallback as ws

pytestmark = pytest.mark.asyncio
URL = "wss://target.test/v1/noise?auth_token=SECRET_TOKEN"


class Writer:
    def __init__(self, reader, *, connect_head=None, tls_error=None, stall=None):
        self.reader = reader
        self.connect_head = connect_head or b"HTTP/1.1 200 Established\r\n\r\n"
        self.tls_error = tls_error
        self.stall = stall
        self.events = []
        self.writes = []
        self.entered = asyncio.Event()
        self.closed = False
        self.waited = False
        self.tls = False

    def write(self, data):
        self.writes.append(data)
        if data.startswith(b"CONNECT"):
            self.events.append("CONNECT")
            self.reader.feed_data(self.connect_head)
            if self.stall == "connect_eof":
                self.reader.feed_eof()
            if self.stall == "connect_headers":
                self.entered.set()
        elif data.startswith(b"GET"):
            self.events.append("GET")
            if self.stall == "upgrade_headers":
                self.reader.feed_data(b"HTTP/1.1 101 Switching Protocols\r\n")
                self.entered.set()
                return
            key = next(line.split(b": ", 1)[1] for line in data.split(b"\r\n") if line.startswith(b"Sec-WebSocket-Key:"))
            accept = ws.build_accept(key.decode()).encode()
            self.reader.feed_data(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")

    async def drain(self):
        if self.stall == "connect_drain" and self.events[-1] == "CONNECT":
            self.entered.set()
            await asyncio.Future()

    async def start_tls(self, context, *, server_hostname, ssl_handshake_timeout):
        self.events.append("TLS")
        assert server_hostname == "target.test"
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert ssl_handshake_timeout > 0
        self.entered.set()
        if self.stall == "tls":
            await asyncio.Future()
        if self.tls_error:
            raise self.tls_error
        self.tls = True

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


def install(monkeypatch, **options):
    reader = asyncio.StreamReader()
    writer = Writer(reader, **options)

    async def open_connection(*args, **kwargs):
        assert args == ("proxy.test", 8080)
        assert not kwargs
        return reader, writer

    monkeypatch.setattr(ws.asyncio, "open_connection", open_connection)
    return reader, writer


async def test_wss_proxy_authenticates_tls_before_token_get(monkeypatch):
    _, writer = install(monkeypatch)
    socket = await ws.connect(URL, proxy="http://proxy.test:8080")
    assert writer.events == ["CONNECT", "TLS", "GET"]
    assert writer.tls
    assert b"SECRET_TOKEN" not in writer.writes[0]
    assert b"auth_token=SECRET_TOKEN" in writer.writes[1]
    assert not writer.closed
    await socket.close()


async def test_ws_proxy_has_no_tls_upgrade(monkeypatch):
    _, writer = install(monkeypatch)
    socket = await ws.connect("ws://target.test/chat", proxy="proxy.test:8080")
    assert writer.events == ["CONNECT", "GET"]
    assert not writer.tls
    await socket.close()


async def test_certificate_failure_closes_without_token_get_or_error_leak(monkeypatch):
    _, writer = install(monkeypatch, tls_error=ssl.SSLCertVerificationError("SECRET_TOKEN raw peer response"))
    with pytest.raises(ws.WebSocketProtocolError, match="TLS handshake failed") as caught:
        await ws.connect(URL, proxy="proxy.test:8080")
    assert "SECRET_TOKEN" not in str(caught.value)
    assert writer.events == ["CONNECT", "TLS"]
    assert all(b"SECRET_TOKEN" not in data for data in writer.writes)
    assert writer.closed and writer.waited


@pytest.mark.parametrize("stage", ["tls", "connect_drain", "upgrade_headers", "connect_headers"])
async def test_cancellation_reclaims_partial_writer(monkeypatch, stage):
    options = {"stall": stage}
    if stage == "connect_headers":
        options["connect_head"] = b"HTTP/1.1 200 Established\r\n"
    _, writer = install(monkeypatch, **options)
    task = asyncio.create_task(ws.connect(URL, proxy="proxy.test:8080"))
    await asyncio.wait_for(writer.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed and writer.waited
    if stage != "upgrade_headers":
        assert "GET" not in writer.events


@pytest.mark.parametrize("stage", ["tls", "connect_drain", "upgrade_headers", "connect_headers"])
async def test_open_budget_bounds_all_handshake_waits(monkeypatch, stage):
    options = {"stall": stage}
    if stage == "connect_headers":
        options["connect_head"] = b"HTTP/1.1 200 Established\r\nX-Partial: still-open\r\n"
    _, writer = install(monkeypatch, **options)
    with pytest.raises(ws.WebSocketProtocolError, match="TimeoutError"):
        await asyncio.wait_for(ws.connect(URL, proxy="proxy.test:8080", open_timeout=0.02), 1)
    assert writer.closed and writer.waited
    if stage != "upgrade_headers":
        assert "GET" not in writer.events


@pytest.mark.parametrize("head", [
    b"HTTP/1.1 407 SECRET_TOKEN auth_token=SECRET_TOKEN\r\n\r\n",
    b"HTTP/1.1 2000 not-200 SECRET_TOKEN\r\n\r\n",
    b"SECRET_TOKEN\r\n\r\n",
])
async def test_connect_rejection_closes_and_never_echoes_peer_status(monkeypatch, head):
    _, writer = install(monkeypatch, connect_head=head)
    with pytest.raises(ws.WebSocketProtocolError) as caught:
        await ws.connect(URL, proxy="proxy.test:8080")
    assert "SECRET_TOKEN" not in str(caught.value)
    assert writer.events == ["CONNECT"]
    assert writer.closed and writer.waited


async def test_connect_eof_inside_headers_is_not_success(monkeypatch):
    _, writer = install(monkeypatch, connect_head=b"HTTP/1.1 200 Established\r\n", stall="connect_eof")
    with pytest.raises(ws.WebSocketProtocolError, match="prematurely"):
        await ws.connect(URL, proxy="proxy.test:8080")
    assert writer.events == ["CONNECT"]
    assert writer.closed and writer.waited


async def test_connect_header_bytes_are_bounded(monkeypatch):
    header = b"HTTP/1.1 200 OK\r\n" + b"X: value\r\n" * 7000 + b"\r\n"
    _, writer = install(monkeypatch, connect_head=header)
    with pytest.raises(ws.WebSocketProtocolError, match="65536"):
        await ws.connect(URL, proxy="proxy.test:8080")
    assert writer.closed and "GET" not in writer.events


async def test_loopback_connect_tunnel_performs_real_tls_before_upgrade(tmp_path):
    """A self-generated trusted localhost cert tests actual StreamWriter.start_tls."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    finished = asyncio.get_running_loop().create_future()
    tasks = []

    async def handle(reader, writer):
        try:
            # This accepted TCP socket initially acts as the HTTP proxy. Upgrade
            # its tunnel endpoint to a real TLS server after CONNECT is flushed.
            head = await reader.readuntil(b"\r\n\r\n")
            assert head.startswith(b"CONNECT localhost:443 HTTP/1.1\r\n")
            assert b"SECRET_TOKEN" not in head
            writer.write(b"HTTP/1.1 200 Established\r\n\r\n")
            await writer.drain()
            await writer.start_tls(server_context, ssl_handshake_timeout=2)
            assert writer.get_extra_info("ssl_object") is not None
            request = await reader.readuntil(b"\r\n\r\n")
            assert request.startswith(b"GET /v1/noise?auth_token=SECRET_TOKEN HTTP/1.1\r\n")
            nonce = next(line.split(b": ", 1)[1] for line in request.split(b"\r\n") if line.startswith(b"Sec-WebSocket-Key:"))
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + ws.build_accept(nonce.decode()).encode() + b"\r\n\r\n")
            await writer.drain()
            frame, _ = await ws.read_frame(reader)
            assert frame.opcode == ws.OP_CLOSE
            finished.set_result(True)
        except BaseException as exc:
            if not finished.done():
                finished.set_exception(exc)
        finally:
            await ws._abort(writer)

    def accept(reader, writer):
        tasks.append(asyncio.create_task(handle(reader, writer)))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        proxy_port = server.sockets[0].getsockname()[1]
        socket = await ws.connect("wss://localhost/v1/noise?auth_token=SECRET_TOKEN", proxy=f"http://127.0.0.1:{proxy_port}", ssl_context=client_context, open_timeout=3)
        await socket.close()
        assert await asyncio.wait_for(finished, 3)
    finally:
        server.close()
        await server.wait_closed()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
