"""``python -m theforge.plan_advisory`` entry point."""

import sys

from .report import main

if __name__ == "__main__":
    sys.exit(main())
