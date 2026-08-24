"""``python -m mpftp`` — the CLI, or the local PWA with no arguments.

``python -m mpftp <subcommand> ...`` behaves exactly like the ``mpftp``
console script. ``python -m mpftp`` with no arguments launches the local PWA
(file transfer + REPL, served over loopback) instead of the CLI's usual
"the following arguments are required" error, since a bare invocation from a
terminal is far more likely to mean "open the app" than "show me the error."
The ``mpftp`` console script's no-argument behavior is unchanged.
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) == 1:
        from .pwa import main as pwa_main

        pwa_main([])
        return

    from .cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
