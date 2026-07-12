"""Regression tests for transient dev-error classification.

_is_transient_dev_failure must only treat HTTP status codes (429/500/502/503/504)
as transient when they appear as standalone digit sequences, not as substrings of
unrelated numbers such as port numbers or token counts. "connection refused" must
not be classified as transient since it signals a misconfigured/unreachable
endpoint rather than transient provider load.
"""

from __future__ import annotations

from theforge.coordinator.dev_phase import _is_transient_dev_failure
from theforge.runners import AgentResult


def _failed_result(output: str) -> AgentResult:
    return AgentResult(
        success=False,
        output=output,
        session_id=None,
        cost_usd=0.10,
        exit_code=1,
        raw={},
        profile_name="dev",
    )


class TestTransientStatusCodeAnchoring:
    def test_bare_status_code_is_transient(self):
        assert _is_transient_dev_failure(_failed_result("upstream returned 503"))

    def test_status_code_with_punctuation_is_transient(self):
        assert _is_transient_dev_failure(_failed_result("HTTP/1.1 500 Internal Server Error"))

    def test_port_number_containing_status_code_digits_is_not_transient(self):
        result = _failed_result("connecting to localhost:5003 failed")
        assert not _is_transient_dev_failure(result)

    def test_token_count_containing_status_code_digits_is_not_transient(self):
        result = _failed_result("prompt used 1500 tokens of context")
        assert not _is_transient_dev_failure(result)

    def test_larger_number_containing_status_code_digits_is_not_transient(self):
        result = _failed_result("processed 50000 records before failing")
        assert not _is_transient_dev_failure(result)


class TestConnectionRefusedIsNotTransient:
    def test_connection_refused_is_not_transient(self):
        result = _failed_result("dial tcp 127.0.0.1:8080: connection refused")
        assert not _is_transient_dev_failure(result)

    def test_connection_reset_is_still_transient(self):
        result = _failed_result("read: connection reset by peer")
        assert _is_transient_dev_failure(result)
