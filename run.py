"""Entry point for the Discord collection service.

    python run.py

Reads HOST / PORT / LOG_LEVEL from the environment (see .env.example).

Development reload
------------------
    python run.py --reload

Background workers are guarded by an OS-level lock file, so reload never produces a
second Gateway connection or a second reconciliation worker.
"""

from __future__ import annotations

import argparse

import uvicorn

from app.core.config import get_settings


def main() -> None:
    settings = get_settings()

    parser = argparse.ArgumentParser(description="Run the Discord collection service.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload (development only)."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Uvicorn worker processes. Only one runs the Gateway and reconciler.",
    )
    args = parser.parse_args()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=None if args.reload else args.workers,
        log_level=settings.log_level.lower(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
