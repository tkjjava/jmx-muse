"""Interrupted turns must surface distinctly and must not blame the account."""
from muse_bridge import app as app_module
from muse_bridge.protocol_engine import (
    ProtocolGenerationError,
    ProtocolInterrupted,
    ProtocolModelRefusal,
    retryable,
)


def test_interrupted_maps_to_409_without_account_penalty():
    status, code, state = app_module._protocol_failure(
        ProtocolInterrupted("Muse 中止了这次对话")
    )
    assert (status, code, state) == (409, "protocol_interrupted", None)


def test_interrupted_is_not_retryable_and_keeps_other_mappings():
    assert retryable(ProtocolInterrupted("x")) is False
    # Existing behavior kept: engine refusal text flows through the generation
    # branch; only this new outcome gets its own explicit 409 mapping.
    status, code, _ = app_module._protocol_failure(ProtocolGenerationError("超时"))
    assert (status, code) == (504, "protocol_timeout")
    assert app_module._protocol_failure(ProtocolModelRefusal("no"))[0] == 504