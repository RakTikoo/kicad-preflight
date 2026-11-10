#!/usr/bin/env python3
"""Shim so `python3 preflight.py <project>` works as well as
`python3 -m preflight <project>`."""

import sys

from preflight.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
