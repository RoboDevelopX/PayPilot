"""Load the demo sample data into the configured database (if it's empty).

    python -m scripts.seed      (or: make seed)

DEMO_MODE=1 does this automatically on startup; this script is for loading the
sample data into a local database without demo mode.
"""

from __future__ import annotations

from app.config import get_settings
from app.container import build_container
from app.db import init_db, make_engine, make_session_factory
from app.services.seed import seed_demo


def main() -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    c = build_container(settings, make_session_factory(engine))
    if seed_demo(c.actions, c.ml):
        print(f"Loaded sample data into {settings.database_url} (marked as sample data).")
    else:
        print("Sample data is already there; nothing to do.")


if __name__ == "__main__":
    main()
