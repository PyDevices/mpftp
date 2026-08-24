"""``python -m mpftp`` — the command-line interface.

A future release adds a local PWA frontend here for the no-argument case; for
now every invocation goes to the CLI, exactly like the ``mpftp`` console script.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
