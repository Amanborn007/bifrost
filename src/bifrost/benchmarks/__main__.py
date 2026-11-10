"""``python -m bifrost.benchmarks`` entry point — delegates to the runner CLI."""

from __future__ import annotations

from bifrost.benchmarks.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
