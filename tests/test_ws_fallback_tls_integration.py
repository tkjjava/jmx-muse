"""Real CONNECT -> verified TLS -> RFC6455 exchange, with loopback peers only.

The proxy/origin implement their wire side independently of ws_fallback helpers.
A trusted localhost-only certificate also catches accidental verification bypass.
"""

import asyncio
import base64
import contextlib
import hashlib
import ssl
import struct
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from muse_bridge import ws_fallback

pytestmark = pytest.mark.asyncio


@dataclass
class TunnelLab:
    trust: ssl.SSLContext
    origin_port: int = 0
    proxy_port: int = 0
    connect_lines: list[bytes] = field(default_factory=list)
    tunnel_prefixes: list[bytes] = field(default_factory=list)
    origin_requests: list[bytes] = field(default_factory=list)
    received_frames: list[tuple[int, bytes]] = field(default_factory=list)
    handler_errors: list[Exception] = field(default_factory=list)
    origin_finished: asyncio.Event = field(default_factory=asyncio.Event)


def _certificate_contexts(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "loopback-localhost.pem"
    key_path = tmp_path / "loopback-localhost.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    trust = ssl.create_default_context(cafile=str(cert_path))
    assert trust.check_hostname and trust.verify_mode == ssl.CERT_REQUIRED
    return server, trust


async def _read_masked_frame(reader):
    # All test payloads are deliberately short, including ping and close frames.
    first, second = await reader.readexactly(2)
    assert first & 0x80, "client test frames must not be fragmented"
    assert not first & 0x70, "reserved RFC6455 flags must be zero"
    assert second & 0x80, "RFC6455 client frames must be masked"
    length = second & 0x7F
    assert length < 126, "test peer only accepts short frames"
    mask = await reader.readexactly(4)
    encoded = await reader.readexactly(length)
    return first & 0x0F, bytes(value ^ mask[index % 4] for index, value in enumerate(encoded))


def _server_frame(opcode, payload):
    assert len(payload) < 126
    return bytes((0x80 | opcode, len(payload))) + payload


@asynccontextmanager
async def _loopback_tls_proxy(tmp_path):
    server_context, trust = _certificate_contexts(tmp_path)
    lab = TunnelLab(trust)
    handlers = set()
    writers = set()

    async def origin(reader, writer):
        # This handler is only called after asyncio's real TLS handshake succeeds.
        assert writer.get_extra_info("ssl_object") is not None
        request = await reader.readuntil(b"\r\n\r\n")
        lab.origin_requests.append(request)
        assert request.split(b"\r\n", 1)[0] == b"GET /noise?auth_token=synthetic-only HTTP/1.1"
        fields = {}
        for line in request.split(b"\r\n")[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                fields[name.strip().lower()] = value.strip()
        assert fields[b"upgrade"].lower() == b"websocket"
        assert b"upgrade" in fields[b"connection"].lower()
        assert fields[b"sec-websocket-version"] == b"13"
        accept = base64.b64encode(hashlib.sha1(
            fields[b"sec-websocket-key"] + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        ).digest())
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            + _server_frame(0x9, b"tls-ping")
        )
        await writer.drain()
        binary = None
        pong = False
        while binary is None or not pong:
            opcode, payload = await _read_masked_frame(reader)
            lab.received_frames.append((opcode, payload))
            if opcode == 0x2:
                binary = payload
            elif opcode == 0xA:
                assert payload == b"tls-ping"
                pong = True
            else:
                raise AssertionError(f"unexpected client opcode {opcode}")
        writer.write(_server_frame(0x2, binary))
        await writer.drain()
        opcode, payload = await _read_masked_frame(reader)
        lab.received_frames.append((opcode, payload))
        assert opcode == 0x8
        assert payload[:2] == struct.pack(">H", 1000)
        lab.origin_finished.set()

    async def relay(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass

    async def proxy(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        line = request.split(b"\r\n", 1)[0]
        lab.connect_lines.append(line)
        assert line in (
            f"CONNECT localhost:{lab.origin_port} HTTP/1.1".encode(),
            f"CONNECT 127.0.0.1:{lab.origin_port} HTTP/1.1".encode(),
        )
        # Never resolve arbitrary CONNECT targets: every byte goes to our own TLS peer.
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", lab.origin_port)
        writers.add(upstream_writer)
        pumps = []
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            first = await reader.read(65536)
            lab.tunnel_prefixes.append(first[:5])
            upstream_writer.write(first)
            await upstream_writer.drain()
            pumps = [
                asyncio.create_task(relay(reader, upstream_writer)),
                asyncio.create_task(relay(upstream_reader, writer)),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            upstream_writer.close()

    def launch(handler):
        def connected(reader, writer):
            writers.add(writer)

            async def run():
                try:
                    async with asyncio.timeout(6):
                        await handler(reader, writer)
                except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
                    pass
                except Exception as exc:
                    lab.handler_errors.append(exc)
                finally:
                    writer.close()

            task = asyncio.create_task(run())
            handlers.add(task)
        return connected

    origin_server = await asyncio.start_server(
        launch(origin), "127.0.0.1", 0, ssl=server_context,
        ssl_handshake_timeout=2, ssl_shutdown_timeout=0.5,
    )
    lab.origin_port = origin_server.sockets[0].getsockname()[1]
    proxy_server = None
    try:
        proxy_server = await asyncio.start_server(launch(proxy), "127.0.0.1", 0)
        lab.proxy_port = proxy_server.sockets[0].getsockname()[1]
        yield lab
    finally:
        origin_server.close()
        if proxy_server is not None:
            proxy_server.close()
        await origin_server.wait_closed()
        if proxy_server is not None:
            await proxy_server.wait_closed()
        for writer in writers:
            writer.close()
        for task in handlers:
            task.cancel()
        async with asyncio.timeout(3):
            await asyncio.gather(*handlers, return_exceptions=True)
            for writer in writers:
                with contextlib.suppress(ConnectionError, ssl.SSLError):
                    await writer.wait_closed()
        assert not any(not task.done() for task in handlers)


async def test_http_connect_tunnel_uses_verified_tls_before_websocket_get(tmp_path):
    async with asyncio.timeout(10), _loopback_tls_proxy(tmp_path) as lab:
        ws = None
        try:
            try:
                ws = await ws_fallback.connect(
                    f"wss://localhost:{lab.origin_port}/noise?auth_token=synthetic-only",
                    proxy=f"http://127.0.0.1:{lab.proxy_port}",
                    ssl_context=lab.trust, open_timeout=3,
                )
            except Exception:
                assert lab.tunnel_prefixes and lab.tunnel_prefixes[0][:1] == b"\x16", (
                    "CONNECT tunnel must carry a TLS ClientHello before any HTTP GET"
                )
                raise
            await ws.send(b"binary-through-verified-tls")
            assert await ws.recv() == b"binary-through-verified-tls"
        finally:
            if ws is not None:
                await ws.close()
        await asyncio.wait_for(lab.origin_finished.wait(), 2)
        assert lab.connect_lines == [f"CONNECT localhost:{lab.origin_port} HTTP/1.1".encode()]
        assert lab.tunnel_prefixes[0][:1] == b"\x16"
        assert len(lab.origin_requests) == 1
        assert (0xA, b"tls-ping") in lab.received_frames
        assert (0x2, b"binary-through-verified-tls") in lab.received_frames
        assert not lab.handler_errors


async def test_connect_proxy_does_not_bypass_tls_hostname_verification(tmp_path):
    async with asyncio.timeout(10), _loopback_tls_proxy(tmp_path) as lab:
        # The trusted cert has DNS localhost only, with NO 127.0.0.1 IP SAN.
        with pytest.raises(ws_fallback.WebSocketProtocolError, match=r"TLS handshake failed \(SSLCertVerificationError\)"):
            await ws_fallback.connect(
                f"wss://127.0.0.1:{lab.origin_port}/noise?auth_token=synthetic-only",
                proxy=f"http://127.0.0.1:{lab.proxy_port}",
                ssl_context=lab.trust, open_timeout=3,
            )
        assert lab.trust.check_hostname and lab.trust.verify_mode == ssl.CERT_REQUIRED
        assert lab.connect_lines == [f"CONNECT 127.0.0.1:{lab.origin_port} HTTP/1.1".encode()]
        assert lab.tunnel_prefixes[0][:1] == b"\x16"
        assert lab.origin_requests == []
        assert not lab.handler_errors
