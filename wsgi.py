# Gunicorn entry point: gunicorn wsgi:app --workers 1 --bind 0.0.0.0:8765
#
# Use --workers 1 because the HTML cache is per-process in-memory.
# Multiple workers would each build their own cache independently,
# and a /refresh on one worker wouldn't propagate to the others.

import sys
from pathlib import Path

# Ensure the project venv's site-packages are on sys.path before any imports.
_venv_lib = Path(__file__).parent / "venv" / "lib"
if _venv_lib.exists():
    for _p in _venv_lib.iterdir():
        _sp = _p / "site-packages"
        if _sp.exists() and str(_sp) not in sys.path:
            sys.path.insert(0, str(_sp))

from main import create_app  # noqa: E402

app = create_app()
