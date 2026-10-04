"""qq: the quirq infra command line.

Subcommands land with their v0 items (V0-DEP-01 to V0-DEP-04).
"""
from __future__ import annotations

import argparse
import sys

from qqdepot import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qq", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"qq {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
