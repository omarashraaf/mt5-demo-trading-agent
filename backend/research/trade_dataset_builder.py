from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from research.label_builder import build_labels


DEFAULT_FEATURE_SCHEMA_VERSION = "v2"
DEFAULT_LABEL_SCHEMA_VERSION = "v2"


def _post_analysis_features(post_analysis: dict) -> dict:
    if not isinstance(post_analysis, dict):
        return {
            "f_post_analysis_available": 0,
        }
    quality_scores = post_analysis.get("quality_scores", {}) or {}
    mistakes = post_analysis.get("mistakes", []) if isinstance(post_analysis.get("mistakes"), list) else []
    improvements = post_analysis.get("improvement_actions", []) if isinstance(post_analysis.get("improvement_actions"), list) else []
    root_causes = post_analysis.get("root_causes", []) if isinstance(post_analysis.get("root_causes"), list) else []
    diagnostics = post_analysis.get("diagnostics", {}) if isinstance(post_analysis.get("diagnostics"), dict) else {}
    recommendation = post_analysis.get("recommendation", {}) if isinstance(post_analysis.get("recommendation"), dict) else {}

    return {
        "f_post_analysis_available": int(bool(post_analysis)),
        "f_post_generated_by": str(post_analysis.get("generated_by", "")),
        "f_post_future_confidence_adjustment": float(post_analysis.get("future_confidence_adjustment", 0.0) or 0.0),
        "f_post_execution_quality": float(quality_scores.get("execution_quality", 0.0) or 0.0),
        "f_post_risk_discipline": float(quality_scores.get("risk_discipline", 0.0) or 0.0),
        "f_post_timing_quality": float(quality_scores.get("timing_quality", 0.0) or 0.0),
        "f_post_mistakes_count": float(len(mistakes)),
        "f_post_improvements_count": float(len(improvements)),
        "f_post_root_causes_count": float(len(root_causes)),
        "f_post_diag_late_entry": float(int(diagnostics.get("late_entry_flag", 0) or 0)),
        "f_post_diag_spread_stress": float(int(diagnostics.get("spread_stress_flag", 0) or 0)),
        "f_post_diag_trend_conflict": float(int(diagnostics.get("trend_conflict_flag", 0) or 0)),
        "f_post_diag_overexposure": float(int(diagnostics.get("risk_overexposure_flag", 0) or 0)),
        "f_post_diag_stop_too_tight": float(int(diagnostics.get("stop_too_tight_flag", 0) or 0)),
        "f_post_diag_target_too_far": float(int(diagnostics.get("target_too_far_flag", 0) or 0)),
        "f_post_diag_news_shock": float(int(diagnostics.get("news_shock_flag", 0) or 0)),
        "f_post_diag_execution_delay": float(int(diagnostics.get("execution_delay_flag", 0) or 0)),
        "f_post_rec_expected_winrate_adj": float(recommendation.get("expected_win_rate_adjustment", 0.0) or 0.0),
        "f_post_rec_suggested_sl_pct": float(recommendation.get("suggested_sl_pct_of_start", 0.0) or 0.0),
        "f_post_rec_suggested_tp_pct": float(recommendation.get("suggested_tp_pct_of_start", 0.0) or 0.0),
        "f_post_rec_size_adjustment_pct": float(recommendation.get("next_trade_size_adjustment_pct", 0.0) or 0.0),
    }


def _safe_json_load(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _normalize_feature_row(schema_version: str, feature_map: dict, expected_features: set[str]) -> dict:
    row = {
        "feature_schema_version": schema_version or DEFAULT_FEATURE_SCHEMA_VERSION,
    }
    for key in expected_features:
        row[key] = feature_map.get(key)
    return row


class TradeDatasetBuilder:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _chunked(values: list, size: int = 800) -> list[list]:
        if not values:
            return []
        return [values[i:i + size] for i in range(0, len(values), size)]

    async def _fetch_candidates(self, limit: int) -> list[dict]:
        cursor = await self.db._db.execute(
            """SELECT *
               FROM trade_candidates
               ORDER BY timestamp_utc DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def _fetch_feature_snapshots(self, candidate_ids: list[str]) -> dict[str, dict]:
        if not candidate_ids:
            return {}
        latest: dict[str, dict] = {}
        for chunk in self._chunked(candidate_ids, size=800):
            placeholders = ",".join("?" for _ in chunk)
            cursor = await self.db._db.execute(
                f"""SELECT candidate_id, schema_version, features_json
                    FROM feature_snapshots
                    WHERE candidate_id IN ({placeholders})
                    ORDER BY created_at DESC""",
                tuple(chunk),
            )
            rows = await cursor.fetchall()
            for candidate_id, schema_version, features_json in rows:
                if candidate_id in latest:
                    continue
                latest[candidate_id] = {
                    "schema_version": schema_version or DEFAULT_FEATURE_SCHEMA_VERSION,
                    "features": _safe_json_load(features_json, {}),
                }
        return latest

    async def _fetch_outcomes_by_signal(self, signal_ids: list[int]) -> dict[int, dict]:
        if not signal_ids:
            return {}
        by_signal: dict[int, dict] = {}
        for chunk in self._chunked(signal_ids, size=800):
            placeholders = ",".join("?" for _ in chunk)
            cursor = await self.db._db.execute(
                f"""SELECT *
                    FROM trade_outcomes
                    WHERE signal_id IN ({placeholders})
                    ORDER BY closed_at DESC""",
                tuple(chunk),
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            for row in rows:
                item = dict(zip(cols, row))
                signal_id = item.get("signal_id")
                if signal_id is None or signal_id in by_signal:
                    continue
                by_signal[int(signal_id)] = item
        return by_signal

    async def _fetch_post_analysis_by_signal(self, signal_ids: list[int]) -> dict[int, dict]:
        if not signal_ids:
            return {}
        if not hasattr(self.db, "get_trade_post_analysis_by_signal_ids"):
            return {}
        try:
            return await self.db.get_trade_post_analysis_by_signal_ids(signal_ids)
        except Exception:
            return {}

    async def build_dataset(
        self,
        *,
        limit: int = 10000,
        include_unexecuted: bool = True,
    ) -> tuple[list[dict], dict]:
        candidates = await self._fetch_candidates(limit=limit)
        if not include_unexecuted:
            candidates = [c for c in candidates if bool(c.get("executed", False))]

        candidate_ids = [str(c.get("candidate_id", "")) for c in candidates if c.get("candidate_id")]
        feature_map = await self._fetch_feature_snapshots(candidate_ids)
        signal_ids = [int(c["signal_id"]) for c in candidates if c.get("signal_id") is not None]
        outcome_map = await self._fetch_outcomes_by_signal(signal_ids)
        post_analysis_map = await self._fetch_post_analysis_by_signal(signal_ids)

        all_feature_names: set[str] = set()
        for info in feature_map.values():
            all_feature_names.update((info.get("features") or {}).keys())

        rows: list[dict] = []
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id", ""))
            signal_id = candidate.get("signal_id")
            feature_info = feature_map.get(candidate_id, {})
            raw_features = feature_info.get("features", {}) or {}
            schema_version = str(feature_info.get("schema_version", DEFAULT_FEATURE_SCHEMA_VERSION))

            normalized_features = _normalize_feature_row(schema_version, raw_features, all_feature_names)
            labels = build_labels(
                candidate=candidate,
                outcome=outcome_map.get(int(signal_id)) if signal_id is not None else None,
            )
            post_analysis = {}
            if signal_id is not None:
                post = post_analysis_map.get(int(signal_id), {})
                post_analysis = post.get("analysis_json", {}) if isinstance(post, dict) else {}
            row = {
                "candidate_id": candidate_id,
                "signal_id": signal_id,
                "timestamp_utc": candidate.get("timestamp_utc"),
                "symbol": candidate.get("symbol", ""),
                "asset_class": candidate.get("asset_class", ""),
                "strategy_mode": candidate.get("strategy_mode", ""),
                "technical_direction": candidate.get("technical_direction", "HOLD"),
                "risk_decision": candidate.get("risk_decision", ""),
                "executed": int(bool(candidate.get("executed", False))),
                "rejection_reasons": _safe_json_load(candidate.get("rejection_reasons_json"), []),
                "event_type": candidate.get("event_type", ""),
                "event_importance": candidate.get("event_importance", ""),
                "gemini_changed_decision": int(bool(candidate.get("gemini_changed_decision", False))),
                "meta_model_changed_decision": int(bool(candidate.get("meta_model_changed_decision", False))),
            }
            row.update(_post_analysis_features(post_analysis))
            row.update({f"f_{k}": v for k, v in normalized_features.items()})
            row.update(labels)
            rows.append(row)

        metadata = {
            "generated_at_utc": time.time(),
            "row_count": len(rows),
            "feature_columns": sorted([f"f_{name}" for name in all_feature_names] + ["f_feature_schema_version"]),
            "label_columns": sorted(
                {
                    "label_schema_version",
                    "no_trade_label",
                    "profitable_after_costs_30m",
                    "profitable_after_costs_90m",
                    "profitable_after_costs_180m",
                    "hit_target_before_stop",
                    "expected_return_30m",
                    "expected_return_90m",
                    "expected_return_180m",
                    "mfe_label",
                    "mae_label",
                    "hold_bucket",
                }
            ),
            "default_feature_schema_version": DEFAULT_FEATURE_SCHEMA_VERSION,
            "default_label_schema_version": DEFAULT_LABEL_SCHEMA_VERSION,
        }
        return rows, metadata

    async def build_rows(self, limit: int = 5000) -> list[dict]:
        rows, _ = await self.build_dataset(limit=limit, include_unexecuted=True)
        return rows

    async def export_csv(self, output_path: str, limit: int = 5000, include_unexecuted: bool = True) -> str:
        rows, _ = await self.build_dataset(limit=limit, include_unexecuted=include_unexecuted)
        if not rows:
            Path(output_path).write_text("", encoding="utf-8")
            return output_path
        fieldnames = sorted({k for row in rows for k in row.keys()})
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return output_path

    async def export_parquet(self, output_path: str, limit: int = 5000, include_unexecuted: bool = True) -> str:
        rows, _ = await self.build_dataset(limit=limit, include_unexecuted=include_unexecuted)
        if not rows:
            Path(output_path).write_bytes(b"")
            return output_path
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError("pandas is required for parquet export") from exc

        frame = pd.DataFrame(rows)
        try:
            frame.to_parquet(output_path, index=False)
        except Exception as exc:
            raise RuntimeError(
                "Parquet export failed. Install pyarrow or fastparquet in the backend venv."
            ) from exc
        return output_path

    async def export_metadata_json(self, output_path: str, limit: int = 5000, include_unexecuted: bool = True) -> str:
        _, metadata = await self.build_dataset(limit=limit, include_unexecuted=include_unexecuted)
        Path(output_path).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return output_path
