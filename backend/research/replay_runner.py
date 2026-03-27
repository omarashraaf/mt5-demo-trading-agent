from __future__ import annotations

import json
import pickle
import time
import uuid
from pathlib import Path

import pandas as pd

from research.trade_dataset_builder import TradeDatasetBuilder


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _max_drawdown(points: list[tuple[float, float]]) -> float:
    if not points:
        return 0.0
    peak = 0.0
    running = 0.0
    max_dd = 0.0
    for _, pnl in points:
        running += pnl
        if running > peak:
            peak = running
        drawdown = peak - running
        if drawdown > max_dd:
            max_dd = drawdown
    return max_dd


def _group_expectancy(rows: list[dict], key: str) -> dict:
    buckets: dict[str, dict] = {}
    for row in rows:
        name = str(row.get(key) or "unknown")
        pnl = _safe_float(row.get("pnl", 0.0), 0.0)
        item = buckets.setdefault(name, {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        item["trades"] += 1
        item["pnl"] += pnl
        if pnl > 0:
            item["wins"] += 1
        elif pnl < 0:
            item["losses"] += 1
    for bucket in buckets.values():
        trades = max(1, bucket["trades"])
        bucket["expectancy"] = bucket["pnl"] / trades
        bucket["win_rate"] = bucket["wins"] / trades
    return buckets


def _compute_metrics(rows: list[dict]) -> dict:
    if not rows:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
            "pnl_by_symbol": {},
            "pnl_by_event_type": {},
            "pnl_by_hold_bucket": {},
        }

    wins = [r for r in rows if _safe_float(r.get("pnl")) > 0]
    losses = [r for r in rows if _safe_float(r.get("pnl")) < 0]
    total_pnl = sum(_safe_float(r.get("pnl", 0.0)) for r in rows)
    gross_profit = sum(_safe_float(r.get("pnl", 0.0)) for r in wins)
    gross_loss_abs = abs(sum(_safe_float(r.get("pnl", 0.0)) for r in losses))
    ordered_points = sorted(
        [(_safe_float(r.get("timestamp_utc", 0.0)), _safe_float(r.get("pnl", 0.0))) for r in rows],
        key=lambda item: item[0],
    )
    return {
        "trades": len(rows),
        "win_rate": len(wins) / len(rows),
        "avg_win": gross_profit / len(wins) if wins else 0.0,
        "avg_loss": (sum(_safe_float(r.get("pnl", 0.0)) for r in losses) / len(losses)) if losses else 0.0,
        "expectancy": total_pnl / len(rows),
        "profit_factor": (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else float("inf"),
        "total_pnl": total_pnl,
        "max_drawdown": _max_drawdown(ordered_points),
        "pnl_by_symbol": _group_expectancy(rows, "symbol"),
        "pnl_by_event_type": _group_expectancy(rows, "event_type"),
        "pnl_by_hold_bucket": _group_expectancy(rows, "hold_bucket"),
    }


class ReplayRunner:
    def __init__(self, db, *, artifacts_dir: str = "research_artifacts/models"):
        self.db = db
        self.dataset_builder = TradeDatasetBuilder(db)
        self.artifacts_dir = Path(artifacts_dir)

    async def _resolve_model_artifacts(self, version_id: str) -> tuple[Path, Path]:
        version = await self.db.get_model_version(version_id)
        if version is None:
            raise ValueError(f"Model version not found: {version_id}")

        notes = str(version.get("notes") or "")
        artifact_name = ""
        summary_name = ""
        for part in notes.split(";"):
            if part.startswith("artifact="):
                artifact_name = part.split("=", 1)[1].strip()
            if part.startswith("summary="):
                summary_name = part.split("=", 1)[1].strip()

        artifact_path = self.artifacts_dir / (artifact_name or f"{version_id}.pkl")
        summary_path = self.artifacts_dir / (summary_name or f"{version_id}.json")
        if not artifact_path.exists():
            raise FileNotFoundError(f"Missing artifact file: {artifact_path}")
        if not summary_path.exists():
            raise FileNotFoundError(f"Missing model summary file: {summary_path}")
        return artifact_path, summary_path

    async def run_replay(
        self,
        *,
        version_id: str,
        score_threshold: float = 0.55,
        include_unexecuted: bool = True,
        limit: int = 200000,
    ) -> dict:
        run_id = f"replay-{uuid.uuid4().hex[:12]}"
        config = {
            "version_id": version_id,
            "score_threshold": score_threshold,
            "include_unexecuted": include_unexecuted,
            "limit": limit,
        }
        started_at = time.time()
        await self.db.log_replay_run(
            run_id=run_id,
            model_version_id=version_id,
            config=config,
            status="running",
            started_at=started_at,
        )
        try:
            artifact_path, summary_path = await self._resolve_model_artifacts(version_id)
            with artifact_path.open("rb") as handle:
                model = pickle.load(handle)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            feature_columns = list(summary.get("feature_columns", []))
            if not feature_columns:
                raise RuntimeError("Model summary has no feature_columns.")

            rows, _ = await self.dataset_builder.build_dataset(
                limit=limit,
                include_unexecuted=include_unexecuted,
            )
            if not rows:
                raise RuntimeError("No dataset rows available for replay.")

            frame = pd.DataFrame(
                [
                    {k: (",".join(v) if isinstance(v, list) else ("" if v is None else v)) for k, v in row.items()}
                    for row in rows
                ]
            )
            for col in feature_columns:
                if col not in frame.columns:
                    frame[col] = ""

            probabilities = list(model.predict_proba(frame[feature_columns])[:, 1])
            executed_rows = []
            meta_rows = []
            for row, prob in zip(rows, probabilities):
                if int(row.get("executed", 0)) != 1:
                    continue
                pnl = _safe_float(row.get("expected_return_180m", 0.0), 0.0)
                trade_item = {
                    "timestamp_utc": row.get("timestamp_utc"),
                    "symbol": row.get("symbol"),
                    "event_type": row.get("event_type"),
                    "hold_bucket": row.get("hold_bucket"),
                    "pnl": pnl,
                }
                executed_rows.append(trade_item)
                if _safe_float(prob, 0.0) >= score_threshold:
                    meta_rows.append(trade_item)

            baseline = _compute_metrics(executed_rows)
            meta = _compute_metrics(meta_rows)
            report = {
                "run_id": run_id,
                "model_version_id": version_id,
                "score_threshold": score_threshold,
                "sample_size_total_candidates": len(rows),
                "sample_size_executed_baseline": len(executed_rows),
                "sample_size_after_meta_filter": len(meta_rows),
                "baseline": baseline,
                "with_meta_model_filter": meta,
                "delta": {
                    "trades": meta["trades"] - baseline["trades"],
                    "win_rate": meta["win_rate"] - baseline["win_rate"],
                    "expectancy": meta["expectancy"] - baseline["expectancy"],
                    "total_pnl": meta["total_pnl"] - baseline["total_pnl"],
                    "max_drawdown": meta["max_drawdown"] - baseline["max_drawdown"],
                },
                "completed_at": time.time(),
            }
            await self.db.update_replay_run(
                run_id=run_id,
                status="completed",
                metrics=report,
                finished_at=time.time(),
                notes=f"Replay completed with threshold={score_threshold}",
            )
            return report
        except Exception as exc:
            await self.db.update_replay_run(
                run_id=run_id,
                status="failed",
                finished_at=time.time(),
                notes=f"Replay failed: {exc}",
            )
            raise
