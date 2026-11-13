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
