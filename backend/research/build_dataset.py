from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from config import config
from research.trade_dataset_builder import TradeDatasetBuilder
from storage.db import Database


async def _run(args: argparse.Namespace):
    db = Database(config.DB_PATH)
    await db.initialize()
    try:
        builder = TradeDatasetBuilder(db)
        output_base = Path(args.output).resolve()
        output_base.parent.mkdir(parents=True, exist_ok=True)

        csv_path = str(output_base.with_suffix(".csv"))
        await builder.export_csv(
            csv_path,
            limit=args.limit,
            include_unexecuted=args.include_unexecuted,
        )

        metadata_path = str(output_base.with_suffix(".metadata.json"))
        await builder.export_metadata_json(
            metadata_path,
            limit=args.limit,
            include_unexecuted=args.include_unexecuted,
        )

        parquet_path = None
        if args.parquet:
            parquet_path = str(output_base.with_suffix(".parquet"))
            await builder.export_parquet(
                parquet_path,
                limit=args.limit,
                include_unexecuted=args.include_unexecuted,
            )

        print(f"CSV: {csv_path}")
        print(f"Metadata: {metadata_path}")
        if parquet_path:
            print(f"Parquet: {parquet_path}")
    finally:
        await db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild ML research dataset from SQLite trade candidates/outcomes."
    )
    parser.add_argument(
        "--output",
        default="research_exports/trade_dataset",
        help="Output base path without extension (default: research_exports/trade_dataset)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100000,
        help="Max number of candidates to include (default: 100000)",
    )
    parser.add_argument(
        "--include-unexecuted",
        action="store_true",
        default=False,
        help="Include blocked/unexecuted candidates (default: false)",
    )
    parser.add_argument(
        "--parquet",
        action="store_true",
        default=False,
        help="Also export parquet (requires pyarrow or fastparquet).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_run(parse_args()))
