"""Allow ``python -m lead_finder_agent`` as an alternative to the console script."""

from __future__ import annotations

import sys

from lead_finder_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
