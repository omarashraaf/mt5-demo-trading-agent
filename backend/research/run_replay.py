from __future__ import annotations

import argparse
import asyncio
import json

from config import config
from research.replay_runner import ReplayRunner
from storage.db import Database


async def _run(args: argparse.Namespace):
    db = Database(config.DB_PATH)
    await db.initialize()
    try:
        runner = ReplayRunner(db, artifacts_dir=args.artifacts_dir)
        report = await runner.run_replay(
            version_id=args.version_id,
            score_threshold=args.score_threshold,
            include_unexecuted=args.include_unexecuted,
            limit=args.limit,
        )
        print(json.dumps(report, indent=2))
    finally:
        await db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run replay for an existing candidate model version.")
    parser.add_argument("--version-id", required=True, help="Model version id from model_versions table.")
    parser.add_argument("--score-threshold", type=float, default=0.55, help="Probability threshold for filtering.")
    parser.add_argument("--artifacts-dir", default="research_artifacts/models")
    parser.add_argument("--limit", type=int, default=200000)
    parser.add_argument("--include-unexecuted", action="store_true", default=False)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_run(parse_args()))
