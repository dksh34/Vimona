#!/usr/bin/env python3
"""Launch Vimona native Fedora desktop interface."""

import sys

from vimona.app import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
