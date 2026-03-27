from __future__ import annotations

import argparse
import asyncio
import json

from config import config
from research.walk_forward_runner import WalkForwardRunner
from storage.db import Database


async def _run(args: argparse.Namespace):
    db = Database(config.DB_PATH)
    await db.initialize()
    try:
        runner = WalkForwardRunner(db)
        report = await runner.run_walk_forward(
            algorithm=args.algorithm,
            target_column=args.target,
            score_threshold=args.score_threshold,
            windows=args.windows,
            include_unexecuted=args.include_unexecuted,
            limit=args.limit,
        )
        print(json.dumps(report, indent=2))
    finally:
        await db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run walk-forward offline evaluation.")
    parser.add_argument(
        "--algorithm",
        default="logistic_regression",
        choices=["logistic_regression", "gradient_boosting"],
    )
    parser.add_argument("--target", default="profitable_after_costs_90m")
    parser.add_argument("--score-threshold", type=float, default=0.55)
    parser.add_argument("--windows", type=int, default=5)
    parser.add_argument("--limit", type=int, default=200000)
    parser.add_argument("--include-unexecuted", action="store_true", default=False)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_run(parse_args()))
