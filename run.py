#!/usr/bin/env python3
"""Thin entrypoint so the project can be run without installation:

    python run.py test
    python run.py scrape --dry-run --limit 50

Equivalent to the ``providermap`` console script installed by
``pip install -e .`` (see pyproject.toml) - use whichever is more convenient.
"""

from providermap.cli import main

if __name__ == "__main__":
    main()
