"""Upgrade metadata never leaks raw token URLs; partial Noise sessions close."""
import asyncio
from types import SimpleNamespace

import pytest
from test_protocol_client import COOKIES, FailingOpener, NoiseWebSocket, WSOpener

from muse_bridge.protocol_client import HatchProtocolClient, ProtocolError

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("status,expected", [(502, "HTTP 502"), (401, "HTTP 401"), ("502", None), (True, None), (999, None), (None, None)])
async def test_upgrade_retains_only_numeric_status_without_raw_response(status, expected):
    class InvalidStatus(Exception):
        pass

    exc = InvalidStatus("SECRET_TOKEN wss://host/?auth_token=SECRET_TOKEN")
    exc.response = SimpleNamespace(status_code=status, headers={"secret": "SECRET_TOKEN"})
    client = HatchProtocolClient(COOKIES, ws_opener=FailingOpener(exc))
    try:
        with pytest.raises(ProtocolError) as caught:
            await client._open_noise_ws("hatch_vm")
        message = str(caught.value)
        assert "InvalidStatus" in message
        assert "SECRET_TOKEN" not in message and "auth_token" not in message
        assert (expected in message) if expected else ("HTTP" not in message)
        assert not client.connected and client.ws is None
    finally:
        await client.close()


async def test_legacy_upgrade_status_metadata_supported():
    class InvalidStatusCode(Exception):
        status_code = 503

    client = HatchProtocolClient(COOKIES, ws_opener=FailingOpener(InvalidStatusCode("secret")))
    try:
        with pytest.raises(ProtocolError, match="HTTP 503"):
            await client._open_noise_ws("hatch_vm")
    finally:
        await client.close()


@pytest.mark.parametrize("stage", ["msg1_send", "msg2_recv", "msg3_send"])
async def test_handshake_cancel_closes_partial_session_before_return(stage):
    entered = asyncio.Event()

    class StallingWebSocket(NoiseWebSocket):
        async def send(self, data):
            if (stage == "msg1_send" and self.phase == 0) or (stage == "msg3_send" and self.phase == 1):
                entered.set()
                await asyncio.Future()
            await super().send(data)

        async def recv(self):
            if stage == "msg2_recv":
                entered.set()
                await asyncio.Future()
            return await super().recv()

    ws = StallingWebSocket()
    client = HatchProtocolClient(COOKIES, ws_opener=WSOpener(lambda: ws))
    task = asyncio.create_task(client._open_noise_ws("hatch_vm"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ws.closed and client.ws is None
        assert client.send_cipher is None and client.recv_cipher is None
        assert not client.connected
        assert client.receiver_task is None and client.heartbeat_task is None
    finally:
        await client.close()


@pytest.mark.parametrize("stage", ["msg1_send", "msg2_recv", "msg3_send"])
async def test_total_handshake_deadline_covers_all_await_points(stage):
    class StallingWebSocket(NoiseWebSocket):
        async def send(self, data):
            if (stage == "msg1_send" and self.phase == 0) or (stage == "msg3_send" and self.phase == 1):
                await asyncio.Future()
            await super().send(data)

        async def recv(self):
            if stage == "msg2_recv":
                await asyncio.Future()
            return await super().recv()

    ws = StallingWebSocket()
    client = HatchProtocolClient(COOKIES, ws_opener=WSOpener(lambda: ws))
    client.handshake_timeout = 0.02
    try:
        async with asyncio.timeout(1):
            with pytest.raises(ProtocolError, match="总预算"):
                await client._open_noise_ws("hatch_vm")
        assert ws.closed and client.ws is None
        assert client.send_cipher is None and client.recv_cipher is None
    finally:
        await client.close()


async def test_upstream_timeout_is_not_mislabeled_as_budget_expiry():
    class FailingWebSocket(NoiseWebSocket):
        async def recv(self):
            raise TimeoutError("underlying receive deadline")

    ws = FailingWebSocket()
    client = HatchProtocolClient(COOKIES, ws_opener=WSOpener(lambda: ws))
    try:
        with pytest.raises(TimeoutError, match="underlying receive deadline"):
            await client._open_noise_ws("hatch_vm")
        assert ws.closed and client.ws is None
    finally:
        await client.close()


async def test_handshake_budget_is_not_reset_for_message_two(monkeypatch):
    # Expire the actual outer timeout after msg1. An incorrectly nested/per-recv
    # budget would receive msg2 and complete; the shared deadline must cancel.
    original_timeout = asyncio.timeout
    scopes = []

    def recorded_timeout(delay):
        scope = original_timeout(delay)
        scopes.append(scope)
        return scope

    monkeypatch.setattr(asyncio, "timeout", recorded_timeout)

    class ExpiringWebSocket(NoiseWebSocket):
        async def send(self, data):
            await super().send(data)
            if self.phase == 1:
                scopes[0].reschedule(asyncio.get_running_loop().time())

        async def recv(self):
            await asyncio.sleep(0)
            return await super().recv()

    ws = ExpiringWebSocket()
    client = HatchProtocolClient(COOKIES, ws_opener=WSOpener(lambda: ws))
    try:
        with pytest.raises(ProtocolError, match="总预算"):
            await client._open_noise_ws("hatch_vm")
        assert len(scopes) == 1 and scopes[0].expired()
        assert ws.closed and client.ws is None
    finally:
        await client.close()


@pytest.mark.parametrize("size,accepted", [(65535, True), (65536, False)])
async def test_msg2_size_checked_before_crypto(monkeypatch, size, accepted):
    from muse_bridge import protocol_client as module

    decoded = []

    class StubNoise:
        def msg1(self, payload):
            return b"first"

        def read_msg2(self, value):
            decoded.append(len(value))
            return b""

        def msg3(self, payload):
            return b"third"

        def split(self):
            return object(), object()

    class SizedWebSocket:
        closed = False

        async def send(self, data):
            pass

        async def recv(self):
            return b"x" * size

        async def close(self):
            self.closed = True

    monkeypatch.setattr(module, "NoiseXX", StubNoise)
    ws = SizedWebSocket()
    client = HatchProtocolClient(COOKIES, ws_opener=WSOpener(lambda: ws))
    try:
        if accepted:
            await client._open_noise_ws("hatch_vm")
            assert decoded == [size]
        else:
            with pytest.raises(ProtocolError, match="65535"):
                await client._open_noise_ws("hatch_vm")
            assert decoded == []
            assert ws.closed and client.ws is None
    finally:
        await client.close()
