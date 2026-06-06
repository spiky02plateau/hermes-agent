"""Regression tests for gateway fallback delivery after partial streaming."""

from types import SimpleNamespace

from gateway.run import _queued_followup_first_response, _stream_delivery_text


def test_stream_delivery_text_returns_unsent_tail_after_partial_multipart():
    consumer = SimpleNamespace(
        undelivered_final_text=lambda text: "tail chunk",
    )

    out = _stream_delivery_text(
        {"final_response": "already visible prefix\ntail chunk"},
        consumer,
    )

    assert out == "tail chunk"


def test_stream_delivery_text_keeps_full_response_when_no_partial_tail():
    consumer = SimpleNamespace(
        undelivered_final_text=lambda text: "",
    )

    out = _stream_delivery_text({"final_response": "full answer"}, consumer)

    assert out == "full answer"


def test_stream_delivery_text_keeps_full_response_without_stream_consumer():
    out = _stream_delivery_text({"final_response": "full answer"}, None)

    assert out == "full answer"


def test_queued_followup_flushes_unsent_tail_not_full_response():
    """Queued follow-up path must not replay visible chunks after flood-control delay."""
    out = _queued_followup_first_response(
        {
            "final_response": "visible chunk\nmissing tail",
            "stream_delivery_response": "missing tail",
        }
    )

    assert out == "missing tail"


def test_queued_followup_flushes_full_response_without_partial_stream_state():
    out = _queued_followup_first_response({"final_response": "full answer"})

    assert out == "full answer"
