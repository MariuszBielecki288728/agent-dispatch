#!/usr/bin/env python3
"""Allow ``python3 -m agent_dispatch`` as an alias for the CLI."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
