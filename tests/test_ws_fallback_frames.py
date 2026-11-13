"""RFC6455 frames use handbuilt bytes, independently of production encoding."""
import asyncio

import pytest

from muse_bridge import ws_fallback as ws

pytestmark = pytest.mark.asyncio


class Reader:
    def __init__(self, data):
        self.data = data
        self.reads = []

    async def read(self, size):
        self.reads.append(size)
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class Writer:
    def __init__(self, *, stall=None):
        self.writes = []
        self.closed = False
        self.aborted = False
        self.entered = asyncio.Event()
        self.stall = stall
        self.transport = self

    def write(self, data):
        assert not self.closed
        self.writes.append(data)

    async def drain(self):
        if self.stall == "drain":
            self.entered.set()
            await asyncio.Future()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        if self.stall == "wait_closed":
            self.entered.set()
            await asyncio.Future()

    def abort(self):
        self.aborted = True


def socket(data, *, max_size=1024, stall=None):
    reader, writer = Reader(data), Writer(stall=stall)
    return ws.WebSocket(reader, writer, max_size=max_size, close_timeout=.02), reader, writer


@pytest.mark.parametrize("raw", [
    b"\x82\x81",  # masked server frame, missing key/body: must reject first
    b"\x09\x00",  # fragmented ping
    b"\x89\x7e",  # extended control length before reading extension
    b"\x8a\x7f",  # extended pong length
    b"\x83\x00", b"\x8b\x00",  # reserved data/control opcodes
    b"\xc2\x00",  # RSV without negotiated extension
    b"\x82\x7e\x00\x7d",  # noncanonical 16 bit
    b"\x82\x7f\x00\x00\x00\x00\x00\x00\xff\xff",  # noncanonical64
    b"\x82\x7f\x80\x00\x00\x00\x00\x01\x00\x00",  # high bit64
    b"\x80\x03",  # orphan continuation, payload absent
])
async def test_invalid_client_frame_rejected_from_header_without_payload(raw):
    client, reader, writer = socket(raw)
    with pytest.raises(ws.WebSocketProtocolError):
        await client.recv()
    assert reader.data == b""
    assert sum(reader.reads) == len(raw)
    assert writer.closed and not writer.writes
    assert client.closed


@pytest.mark.parametrize("raw,size", [
    (b"\x82\x7e\x00\x7e", 126),
    (b"\x82\x7f\x00\x00\x00\x00\x00\x01\x00\x00", 65536),
])
async def test_canonical_extended_lengths_remain_accepted(raw, size):
    head, payload = await ws.read_frame(Reader(raw + b"x" * size), max_size=size)
    assert head.length == size and payload == b"x" * size


async def test_role_neutral_parser_still_accepts_masked_client_frame():
    # XOR of Hi with 01 02 03 04 is 49 6b.
    head, payload = await ws.read_frame(Reader(b"\x82\x82\x01\x02\x03\x04\x49\x6b"))
    assert head.masked and payload == b"Hi"


async def test_fragmented_utf8_with_interleaved_ping_and_pong():
    raw = b"\x01\x01\xe4\x89\x01p\x8a\x00\x80\x02\xbd\xa0"
    client, _, writer = socket(raw, max_size=3)
    assert await client.recv() == "你"
    assert len(writer.writes) == 1
    # Decode client pong independently: mandatory mask and payload p.
    pong = writer.writes[0]
    assert pong[0] == 0x8a and pong[1] == 0x81
    assert bytes([pong[6] ^ pong[2]]) == b"p"
    await client.close()


async def test_aggregate_fragment_limit_rejects_before_next_payload():
    client, reader, writer = socket(b"\x02\x03abc\x80\x02XY", max_size=4)
    with pytest.raises(ws.WebSocketProtocolError, match="too large"):
        await client.recv()
    assert reader.data == b"XY"
    assert reader.reads == [2, 3, 2]
    assert writer.closed


async def test_next_data_header_rejected_during_fragment_before_payload():
    client, reader, _ = socket(b"\x02\x01a\x82\x03XYZ")
    with pytest.raises(ws.WebSocketProtocolError, match="inside a fragment"):
        await client.recv()
    assert reader.data == b"XYZ"


async def test_empty_last_fragment_at_exact_cap_and_ping_does_not_consume_cap():
    client, _, writer = socket(b"\x02\x02ab\x89\x03xyz\x80\x00", max_size=2)
    assert await client.recv() == b"ab"
    assert len(writer.writes) == 1
    await client.close()


@pytest.mark.parametrize("raw", [
    b"\x81\x01\xff", b"\x01\x01\xe4\x80\x01a",
    b"\x88\x01a",  # invalid one-byte close
    b"\x88\x02\x03\xed",  #1005 forbidden wire sentinel
    b"\x88\x02\x03\xee",  #1006
    b"\x88\x02\x03\xec",  #1004
    b"\x88\x02\x03\xf7",  #1015
    b"\x88\x02\x07\xd0",  #2000 reserved
    b"\x88\x03\x03\xe8\xff",  # invalid close UTF8
])
async def test_invalid_utf8_or_close_payload_is_not_echoed(raw):
    client, _, writer = socket(raw)
    with pytest.raises(ws.WebSocketProtocolError):
        await client.recv()
    assert not writer.writes and writer.closed


@pytest.mark.parametrize("raw,code,reason", [
    (b"\x88\x00", None, ""),
    (b"\x88\x02\x03\xef", 1007, ""),
    (b"\x88\x08\x03\xe8SECRET", 1000, "SECRET"),
    (b"\x88\x02\x0b\xb8", 3000, ""),
])
async def test_valid_close_reply_is_once_and_reason_not_in_error(raw, code, reason):
    client, _, writer = socket(raw)
    with pytest.raises(ws.WebSocketClosed) as caught:
        await client.recv()
    assert caught.value.code == code and caught.value.reason == reason
    assert "SECRET" not in str(caught.value)
    assert client.closed and writer.closed and len(writer.writes) == 1
    await client.close()
    for action in (lambda: client.send(b"x"), client.ping, client.recv):
        with pytest.raises(ws.WebSocketClosed):
            await action()
    assert len(writer.writes) == 1


async def test_ping_over_limit_never_writes():
    client, _, writer = socket(b"")
    with pytest.raises(ws.WebSocketProtocolError, match="125"):
        await client.ping(b"x" * 126)
    assert not writer.writes
    await client.close()


async def test_close_gates_concurrent_public_writes_before_drain():
    client, _, writer = socket(b"", stall="drain")
    task = asyncio.create_task(client.close())
    await asyncio.wait_for(writer.entered.wait(), 1)
    for action in (lambda: client.send(b"late"), client.ping, client.recv):
        with pytest.raises(ws.WebSocketClosed):
            await action()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(writer.writes) == 1 and writer.aborted


@pytest.mark.parametrize("stall", ["drain", "wait_closed"])
async def test_close_budget_bounds_retained_writer_and_aborts(stall):
    client, _, writer = socket(b"", stall=stall)
    async with asyncio.timeout(1):
        await client.close()
    assert writer.closed and writer.aborted and client.closed
    assert len(writer.writes) == 1
    await client.close()
    assert len(writer.writes) == 1


@pytest.mark.parametrize("stall", ["drain", "wait_closed"])
async def test_cancel_close_cleans_retained_writer_before_return(stall):
    client, _, writer = socket(b"", stall=stall)
    task = asyncio.create_task(client.close())
    await asyncio.wait_for(writer.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed and writer.aborted and client.closed
    with pytest.raises(ws.WebSocketClosed):
        await client.ping()
    assert len(writer.writes) == 1


@pytest.mark.parametrize("code,reason", [(1005, ""), (1006, ""), (5000, ""), (1000, "a"*124)])
async def test_invalid_local_close_never_writes(code, reason):
    client, _, writer = socket(b"")
    with pytest.raises(ws.WebSocketProtocolError):
        await client.close(code, reason)
    assert not writer.writes and not client.closed
    await client.close()
