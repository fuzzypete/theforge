"""Tests for _extract_failed_tests xdist worker prefix handling."""

from theforge.coordinator.dev_phase import _extract_failed_tests


def test_plain_failed_line():
    output = "FAILED tests/test_foo.py::test_bar"
    assert _extract_failed_tests(output) == ["tests/test_foo.py::test_bar"]


def test_plain_error_line():
    output = "ERROR tests/test_foo.py::test_bar"
    assert _extract_failed_tests(output) == ["tests/test_foo.py::test_bar"]


def test_xdist_failed_line():
    output = "[gw7] FAILED tests/test_foo.py::test_bar"
    assert _extract_failed_tests(output) == ["tests/test_foo.py::test_bar"]


def test_xdist_error_line():
    output = "[gw0] ERROR tests/test_foo.py::TestClass::test_method"
    assert _extract_failed_tests(output) == ["tests/test_foo.py::TestClass::test_method"]


def test_xdist_multi_worker_deduplication():
    output = "[gw3] FAILED tests/test_a.py::test_one\n[gw8] FAILED tests/test_a.py::test_one"
    assert _extract_failed_tests(output) == ["tests/test_a.py::test_one"]


def test_xdist_mixed_with_plain():
    output = (
        "FAILED tests/test_a.py::test_one\n"
        "[gw7] FAILED tests/test_b.py::test_two\n"
        "[gw8] FAILED tests/test_a.py::test_one\n"
    )
    result = _extract_failed_tests(output)
    assert result == ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]


def test_no_worker_prefix_recorded():
    """Worker IDs like [gw7] must never appear as test names."""
    pool_test = (
        "tests/test_coord_review_phase_pool.py"
        "::TestReviewParseRetry::test_schema_error_also_retried"
    )
    output = f"[gw7] FAILED {pool_test}\n[gw8] FAILED tests/test_other.py::test_something\n"
    result = _extract_failed_tests(output)
    assert not any(name.startswith("[gw") for name in result), f"Worker IDs found: {result}"
    assert pool_test in result
    assert "tests/test_other.py::test_something" in result


def test_empty_output():
    assert _extract_failed_tests("") == []


def test_pass_output_no_failures():
    output = "1 passed in 0.12s"
    assert _extract_failed_tests(output) == []
