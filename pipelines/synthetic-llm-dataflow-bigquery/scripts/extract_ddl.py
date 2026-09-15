#!/usr/bin/env python
"""Thin CLI shim — delegates to `sdfb_beam.ddl.cli.main()`.

Usage:
    python scripts/extract_ddl.py --project P --dataset D --table T [--runner DirectRunner]
"""

from __future__ import annotations

import sys

from sdfb_beam.ddl.cli import main

if __name__ == "__main__":
  sys.exit(main())
