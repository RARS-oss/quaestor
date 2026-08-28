"""Entry point for `python -m quaestor` — dispatches to quaestor.cli:main.

The cli import happens inside main() so that importing the quaestor package (and
this module) stays light and side-effect free.
"""
from __future__ import annotations


def main() -> None:
    from quaestor.cli import main as cli_main

    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
