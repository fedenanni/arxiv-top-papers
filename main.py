"""Convenience entry point: `python main.py <ingest|refresh|report> ...`.

Delegates to the csai_citations CLI; equivalent to the `csai-citations` script.
"""

from csai_citations.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
