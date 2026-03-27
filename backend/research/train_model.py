from __future__ import annotations

import argparse
import asyncio
import json

from config import config
from research.model_trainer import ModelTrainer
from storage.db import Database


async def _run(args: argparse.Namespace):
    db = Database(config.DB_PATH)
    await db.initialize()
    try:
        trainer = ModelTrainer(db, artifacts_dir=args.artifacts_dir)
        result = await trainer.train_candidate_model(
            algorithm=args.algorithm,
            target_column=args.target,
            include_unexecuted=args.include_unexecuted,
            min_rows=args.min_rows,
        )
        print(json.dumps(result, indent=2))
    finally:
        await db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train a Step-3 research candidate model.")
    parser.add_argument(
        "--algorithm",
        default="logistic_regression",
        choices=["logistic_regression", "gradient_boosting"],
    )
    parser.add_argument(
        "--target",
        default="profitable_after_costs_90m",
        help="Target label column from dataset rows.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default="research_artifacts/models",
        help="Where to store model artifacts and summaries.",
    )
    parser.add_argument(
        "--include-unexecuted",
        action="store_true",
        default=False,
        help="Include blocked/no-trade candidates in training rows.",
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=30,
        help="Minimum dataset rows required to train.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_run(parse_args()))
