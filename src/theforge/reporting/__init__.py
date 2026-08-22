"""Cross-repository bug reporting from the project where a failure was observed.

``forge report`` files a forge defect into a target repository from the
consuming project, carrying that run's evidence with it. Everything the report
asserts about the run — forge version, runtime identity, resolved configuration
— is read out of the recorded run artifacts, never out of the checkout the
report is later read on.
"""

from __future__ import annotations

__all__: list[str] = []
