"""Module entrypoint for issue-event and sweep runs."""

from __future__ import annotations

import sys

from theforge.shape_check.action import main

if __name__ == "__main__":
    sys.exit(main())
