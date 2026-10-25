
"""Command-line entry point for the daily inventory check."""

import json
import logging

import app.modules.inventory.models  # Register inventory models
from app.db.session import init_db
from app.modules.inventory.inventory_jobs import run_daily_inventory_check


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        # Create any missing tables before the job queries them.
        init_db()

        result = run_daily_inventory_check()
        print(json.dumps(result, indent=2, default=str))
        return 0

    except Exception:
        logging.exception("Daily inventory job failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
