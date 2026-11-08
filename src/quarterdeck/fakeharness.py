"""Compatibility entry point for the terminal simulator implemented by adapter support."""

from .harness.simulator import main


if __name__ == "__main__":
    raise SystemExit(main())
