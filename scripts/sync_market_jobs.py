"""Cron entrypoint for the lightweight VieCareer job ingestion pipeline."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.job_ingestion_service import SOURCES, sync_authorized_sources  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync authorized job sources into PostgreSQL.")
    parser.add_argument("--source", choices=sorted(SOURCES), default=None)
    args = parser.parse_args()
    results = sync_authorized_sources(args.source)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if any(result["status"] == "failed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
