"""Tests for _extract_failed_tests xdist worker prefix handling."""

from theforge.coordinator.dev_phase import _extract_failed_tests, extract_failed_tests


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


# ── Structured extraction: applicability signal (issue #1738) ──────────────────


def test_struct_pytest_failures_recognized():
    result = extract_failed_tests("FAILED tests/test_foo.py::test_bar")
    assert result.tests == ["tests/test_foo.py::test_bar"]
    assert result.format_recognized is True
    assert result.source == "pytest"


def test_struct_pytest_clean_run_is_recognized_but_empty():
    """A pytest gate that failed on lint (no test failed) is a genuine empty list."""
    result = extract_failed_tests("collected 5 items\n\n1 passed in 0.12s")
    assert result.tests == []
    assert result.format_recognized is True
    assert result.source == "pytest"


def test_struct_unrecognized_gate_format_is_not_recognized():
    """xcodebuild/make output matches no pytest grammar → extraction did not apply."""
    xcode_output = (
        "Testing failed:\n"
        "  testBuildWindowPlanKeepsDisplayedCountBoundedByActualWindows()\n"
        "** TEST FAILED **\n"
        "make[2]: *** [test-ios] Error 65\n"
    )
    result = extract_failed_tests(xcode_output)
    assert result.tests == []
    assert result.format_recognized is False
    assert result.source == "unrecognized"


def test_struct_custom_pattern_extracts_from_unrecognized_format():
    """A project-configured pattern recovers failing tests from a non-pytest gate."""
    xcode_output = (
        "Test Case '-[ForgeTests testBuildWindowPlanKeepsDisplayedCountBounded]'"
        " failed (0.003 seconds).\n"
        "** TEST FAILED **\n"
    )
    pattern = r"Test Case .*\s(?P<test>\w+)\]' failed"
    result = extract_failed_tests(xcode_output, failed_test_pattern=pattern)
    assert result.tests == ["testBuildWindowPlanKeepsDisplayedCountBounded"]
    assert result.format_recognized is True
    assert result.source == "custom_pattern"


def test_struct_custom_pattern_no_match_is_still_recognized():
    """When a pattern is configured, an empty result means genuinely no failures."""
    result = extract_failed_tests("All tests passed", failed_test_pattern=r"FAILED (?P<test>\S+)")
    assert result.tests == []
    assert result.format_recognized is True
    assert result.source == "custom_pattern"


def test_struct_custom_pattern_group_one_fallback():
    result = extract_failed_tests("test_alpha ... FAIL", failed_test_pattern=r"^(\S+) \.\.\. FAIL")
    assert result.tests == ["test_alpha"]


def test_struct_empty_output_is_unrecognized():
    result = extract_failed_tests("")
    assert result.tests == []
    assert result.format_recognized is False
