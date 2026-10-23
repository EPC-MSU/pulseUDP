"""PyInstaller entry point: an absolute import, since a frozen ``__main__`` has no parent package."""

import sys

from pulseudp.app import run

if __name__ == "__main__":
    sys.exit(run())
