"""Regression guards for routing runtime footers through the final stream.

These tests intentionally inspect the gateway routing source. The full
``_process_message_background`` integration surface is huge; the regression we
care about here is narrow and structural: once streaming says it already sent
the final body, the gateway must not emit ``_footer_line`` as its own platform
message.
"""

from __future__ import annotations

import inspect

from gateway import run as gateway_run


def _source_between(source: str, start: str, end: str) -> str:
    start_idx = source.index(start)
    end_idx = source.index(end, start_idx)
    return source[start_idx:end_idx]


def test_streaming_already_sent_branch_never_sends_footer_only_message():
    source = inspect.getsource(gateway_run.GatewayRunner._handle_message_with_agent)
    branch = _source_between(
        source,
        'if agent_result.get("already_sent") and not agent_result.get("failed"):',
        'return response',
    )

    assert "_footer_line" not in branch
    assert "_foot_adapter" not in branch
    assert ".send(" not in branch
    assert "return None" in branch


def test_stream_consumer_footer_suffix_is_set_before_finish():
    source = inspect.getsource(gateway_run.GatewayRunner._run_agent)
    suffix_idx = source.index("_stream_consumer.set_final_suffix(_footer_suffix)")
    finish_idx = source.index("_stream_consumer.finish()", suffix_idx)

    assert suffix_idx < finish_idx
