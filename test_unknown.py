from theforge.coordinator.preflight import _p1_findings_match
from theforge.review import ReviewFinding

curr = ReviewFinding(severity="P1", file="unknown", line=None, description="foo", suggestion=None)
prev = ReviewFinding(severity="P1", file="unknown", line=None, description="bar", suggestion=None)

print(_p1_findings_match(curr, prev))
