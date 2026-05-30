"""Frozen-app entry point for PyInstaller builds.

PyInstaller runs the spec's top-level script as ``__main__`` with no parent
package, so it cannot be the package's own ``__main__.py`` (which uses the
relative ``from .app import run``). This launcher uses an absolute import
against the bundled ``pulseudp`` package instead. See packaging/pulseudp.spec.
"""

import sys

from pulseudp.app import run

if __name__ == "__main__":
    sys.exit(run())
