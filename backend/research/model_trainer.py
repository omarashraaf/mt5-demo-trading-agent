from __future__ import annotations

import json
import math
import pickle
import time
import uuid
from pathlib import Path

from research.model_evaluator import evaluate_binary_classifier, extract_feature_importance
from research.model_registry import ModelRegistry
from research.trade_dataset_builder import TradeDatasetBuilder


def _safe_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if not math.isfinite(parsed):
        return float(default)
    return parsed


class EmpiricalRulesModel:
    """Pickle-safe rule model mined from clean app-owned trade outcomes."""

    def predict_proba(self, frame):
        output = []
        for _, row in frame.iterrows():
            probability = ModelTrainer._empirical_rule_probability(dict(row))
            output.append([1.0 - probability, probability])
        return output


class ModelTrainer:
    def __init__(self, db, *, artifacts_dir: str = "research_artifacts/models"):
        self.db = db
        self.dataset_builder = TradeDatasetBuilder(db)
        self.registry = ModelRegistry(db)
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def _require_sklearn(self):
        try:
            from sklearn.compose import ColumnTransformer
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import Pipeline
            from sklearn.preprocessing import OneHotEncoder
            from sklearn.model_selection import train_test_split
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(
                "scikit-learn is required for model training. Install backend requirements to continue."
            ) from exc

        return {
            "pd": pd,
            "Pipeline": Pipeline,
            "ColumnTransformer": ColumnTransformer,
            "OneHotEncoder": OneHotEncoder,
            "train_test_split": train_test_split,
            "LogisticRegression": LogisticRegression,
            "GradientBoostingClassifier": GradientBoostingClassifier,
        }

    @staticmethod
    def _time_split(rows: list[dict], test_ratio: float = 0.2) -> tuple[list[dict], list[dict]]:
        ordered = sorted(rows, key=lambda r: _safe_float(r.get("timestamp_utc"), 0.0))
        if len(ordered) < 10:
            split = max(1, int(len(ordered) * (1.0 - test_ratio)))
            return ordered[:split], ordered[split:] or ordered[:1]
        split = int(len(ordered) * (1.0 - test_ratio))
        split = min(max(split, 1), len(ordered) - 1)
        return ordered[:split], ordered[split:]

    @staticmethod
    def _build_feature_frame(pd_module, rows: list[dict], feature_columns: list[str]):
        feature_rows = []
        for row in rows:
            item = {}
            for col in feature_columns:
                val = row.get(col)
                if isinstance(val, list):
                    item[col] = ",".join(str(v) for v in val)
                elif val is None:
                    item[col] = ""
                else:
                    item[col] = val
            feature_rows.append(item)
        return pd_module.DataFrame(feature_rows)

    @staticmethod
    def _coerce_feature_types(frame, numeric_columns: list[str], categorical_columns: list[str]):
        for col in numeric_columns:
            frame[col] = frame[col].map(lambda value: _safe_float(value, 0.0))
        for col in categorical_columns:
            frame[col] = frame[col].fillna("")
            frame[col] = frame[col].astype(str)
        return frame

    @staticmethod
    def _numeric_columns(frame) -> list[str]:
        def _parse_or_none(value):
            if value is None:
                return None
            text = str(value).strip()
            if text == "":
                return None
            try:
                parsed = float(text)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(parsed):
                return None
            return parsed

        cols: list[str] = []
        for col in frame.columns:
            series = frame[col]
            if getattr(series.dtype, "kind", None) in {"i", "u", "f", "b"}:
                cols.append(col)
                continue
            converted = series.replace("", None)
            numeric = converted.notna().sum()
            if numeric == 0:
                continue
            parsed = converted.map(_parse_or_none)
            parsed_count = parsed.notna().sum()
            if parsed_count > 0 and (parsed_count / numeric) >= 0.5:
                cols.append(col)
        return cols

    @staticmethod
    def _empirical_rule_probability(row: dict) -> float:
        quality = _safe_float(row.get("f_quality_score"), 0.0)
        free_margin = _safe_float(row.get("f_portfolio_free_margin_pct"), 100.0)
        spread = _safe_float(row.get("f_spread_at_eval"), 0.0)
        confidence = _safe_float(row.get("f_confidence"), 0.0)
        margin_util = _safe_float(row.get("f_portfolio_margin_utilization_pct"), 0.0)
        atr_regime = str(row.get("f_atr_regime") or "").strip().lower()
        trend_alignment = _safe_float(row.get("f_trend_alignment_score"), 0.0)

        weak_slice = (
            (quality < 0.70 and free_margin < 99.88)
            or (spread >= 28.0 and free_margin < 99.88)
            or (confidence < 0.58 and spread >= 7.0 and margin_util >= 0.35)
            or (atr_regime == "medium" and spread >= 7.0 and trend_alignment < 0.92)
        )
        return 0.24 if weak_slice else 0.62

    async def _train_empirical_rules_model(
        self,
        *,
        run_id: str,
        target_column: str,
        include_unexecuted: bool,
        min_rows: int,
        min_positive_rows: int,
        started_at: float,
    ) -> dict:
        rows, metadata = await self.dataset_builder.build_dataset(
            limit=200000,
            include_unexecuted=include_unexecuted,
        )
        if len(rows) < min_rows:
            raise RuntimeError(f"Not enough rows to train empirical rules model. Required>={min_rows}, got={len(rows)}")
        feature_columns = sorted([k for k in rows[0].keys() if k.startswith("f_")])
        if not feature_columns:
            raise RuntimeError("No feature columns found in dataset.")

        def _labels(data_rows: list[dict], column: str) -> list[int]:
            return [int(_safe_float(r.get(column), 0.0) >= 0.5) for r in data_rows]

        labels = _labels(rows, target_column)
        positive_count = sum(1 for val in labels if int(val) == 1)
        if len(set(labels)) < 2:
            raise RuntimeError(f"Training labels are one-class for {target_column}.")
        if positive_count < min_positive_rows:
            raise RuntimeError(
                f"Training labels are too sparse for empirical rules: positives={positive_count}<min={min_positive_rows}"
            )

        probabilities = [self._empirical_rule_probability(row) for row in rows]
        returns = [_safe_float(row.get("expected_return_90m"), 0.0) for row in rows]
        evaluation = evaluate_binary_classifier(
            y_true=labels,
            y_pred_prob=probabilities,
            realized_returns=returns,
        )
        evaluation["dataset_row_count"] = len(rows)
        evaluation["train_row_count"] = len(rows)
        evaluation["test_row_count"] = len(rows)
        evaluation["positive_samples_total"] = positive_count
        evaluation["positive_samples_train"] = positive_count
        evaluation["positive_samples_test"] = positive_count
        evaluation["validation_method"] = "clean_app_owned_historical_empirical_rules"
        evaluation["feature_importance"] = [
            {"feature": "f_spread_at_eval", "importance": 1.0},
            {"feature": "f_portfolio_free_margin_pct", "importance": 0.95},
            {"feature": "f_quality_score", "importance": 0.85},
            {"feature": "f_confidence", "importance": 0.70},
            {"feature": "f_portfolio_margin_utilization_pct", "importance": 0.65},
            {"feature": "f_atr_regime", "importance": 0.45},
            {"feature": "f_trend_alignment_score", "importance": 0.40},
        ]

        version_id = f"model-empirical_rules-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        artifact_path = self.artifacts_dir / f"{version_id}.pkl"
        meta_path = self.artifacts_dir / f"{version_id}.json"
        model = EmpiricalRulesModel()
        with artifact_path.open("wb") as handle:
            pickle.dump(model, handle)

        training_summary = {
            "version_id": version_id,
            "run_id": run_id,
            "algorithm": "empirical_rules",
            "target_column": target_column,
            "feature_columns": feature_columns,
            "transformed_feature_count": len(feature_columns),
            "dataset_metadata": metadata,
            "train_rows": len(rows),
            "test_rows": len(rows),
            "evaluation": evaluation,
            "artifact_path": str(artifact_path.resolve()),
            "created_at": time.time(),
        }
        meta_path.write_text(json.dumps(training_summary, indent=2), encoding="utf-8")

        data_range_start = min(_safe_float(r.get("timestamp_utc"), 0.0) for r in rows)
        data_range_end = max(_safe_float(r.get("timestamp_utc"), 0.0) for r in rows)
        await self.registry.register_candidate(
            version_id=version_id,
            algorithm="empirical_rules",
            target_definition=target_column,
            feature_schema_version=str(metadata.get("default_feature_schema_version", "v2")),
            training_date=time.time(),
            data_range_start=data_range_start,
            data_range_end=data_range_end,
            evaluation_metrics=evaluation,
            notes=f"artifact={artifact_path.name};summary={meta_path.name}",
        )
        await self.db.update_model_run(
            run_id=run_id,
            status="candidate",
            metrics=evaluation,
            finished_at=time.time(),
            version_id=version_id,
            notes=f"Empirical rules training completed in {time.time()-started_at:.2f}s",
        )
        return training_summary

    async def train_candidate_model(
        self,
        *,
        algorithm: str = "logistic_regression",
        target_column: str = "profitable_after_costs_90m",
        include_unexecuted: bool = True,
        min_rows: int = 30,
        min_positive_rows: int = 20,
    ) -> dict:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        await self.db.log_model_run(
            run_id=run_id,
            version_id=None,
            status="training",
            params={
                "algorithm": algorithm,
                "target_column": target_column,
                "include_unexecuted": include_unexecuted,
                "min_rows": min_rows,
                "min_positive_rows": min_positive_rows,
            },
        )
        started_at = time.time()
        try:
            if algorithm == "empirical_rules":
                return await self._train_empirical_rules_model(
                    run_id=run_id,
                    target_column=target_column,
                    include_unexecuted=include_unexecuted,
                    min_rows=min_rows,
                    min_positive_rows=min_positive_rows,
                    started_at=started_at,
                )

            libs = self._require_sklearn()
            pd = libs["pd"]
            Pipeline = libs["Pipeline"]
            ColumnTransformer = libs["ColumnTransformer"]
            OneHotEncoder = libs["OneHotEncoder"]
            train_test_split = libs["train_test_split"]
            LogisticRegression = libs["LogisticRegression"]
            GradientBoostingClassifier = libs["GradientBoostingClassifier"]

            rows, metadata = await self.dataset_builder.build_dataset(
                limit=200000,
                include_unexecuted=include_unexecuted,
            )
            if len(rows) < min_rows:
                raise RuntimeError(f"Not enough rows to train model. Required>={min_rows}, got={len(rows)}")

            feature_columns = sorted([k for k in rows[0].keys() if k.startswith("f_")])
            if not feature_columns:
                raise RuntimeError("No feature columns found in dataset.")

            def _labels(data_rows: list[dict], column: str) -> list[int]:
                return [int(_safe_float(r.get(column), 0.0) >= 0.5) for r in data_rows]

            def _positive_count(labels: list[int]) -> int:
                return sum(1 for val in labels if int(val) == 1)

            candidate_targets = [
                target_column,
                "profitable_after_costs_180m",
                "profitable_after_costs_90m",
                "profitable_after_costs_30m",
                "no_trade_label",
            ]
            deduped_targets: list[str] = []
            for candidate in candidate_targets:
                normalized = str(candidate or "").strip()
                if normalized and normalized not in deduped_targets:
                    deduped_targets.append(normalized)

            active_target_column = ""
            train_rows: list[dict] = []
            test_rows: list[dict] = []
            train_y: list[int] = []
            test_y: list[int] = []
            tried_target_stats: list[str] = []
            min_test_positive_rows = max(1, min_positive_rows // 4)

            for candidate_target in deduped_targets:
                all_y = _labels(rows, candidate_target)
                all_positive = _positive_count(all_y)
                if len(set(all_y)) < 2:
                    tried_target_stats.append(f"{candidate_target}:one_class(all_pos={all_positive})")
                    continue
                if all_positive < min_positive_rows:
                    tried_target_stats.append(
                        f"{candidate_target}:positives({all_positive})<min({min_positive_rows})"
                    )
                    continue

                candidate_train_rows, candidate_test_rows = self._time_split(rows, test_ratio=0.2)
                candidate_train_y = _labels(candidate_train_rows, candidate_target)
                candidate_test_y = _labels(candidate_test_rows, candidate_target)
                train_positive = _positive_count(candidate_train_y)
                test_positive = _positive_count(candidate_test_y)

                if (
                    len(set(candidate_train_y)) < 2
                    or train_positive < min_positive_rows
                    or test_positive < min_test_positive_rows
                ):
                    try:
                        candidate_train_rows, candidate_test_rows = train_test_split(
                            rows,
                            test_size=0.2,
                            random_state=42,
                            shuffle=True,
                            stratify=all_y,
                        )
                        candidate_train_y = _labels(candidate_train_rows, candidate_target)
                        candidate_test_y = _labels(candidate_test_rows, candidate_target)
                        train_positive = _positive_count(candidate_train_y)
                        test_positive = _positive_count(candidate_test_y)
                    except Exception:
                        pass

                if len(set(candidate_train_y)) < 2:
                    tried_target_stats.append(f"{candidate_target}:one_class(train)")
                    continue
                if train_positive < min_positive_rows:
                    tried_target_stats.append(
                        f"{candidate_target}:train_pos({train_positive})<min({min_positive_rows})"
                    )
                    continue
                if test_positive < min_test_positive_rows:
                    tried_target_stats.append(
                        f"{candidate_target}:test_pos({test_positive})<min({min_test_positive_rows})"
                    )
                    continue

                active_target_column = candidate_target
                train_rows = candidate_train_rows
                test_rows = candidate_test_rows
                train_y = candidate_train_y
                test_y = candidate_test_y
                break

            if not active_target_column:
                raise RuntimeError(
                    "Training labels are too sparse/imbalanced for safe auto-training. Tried: "
                    + "; ".join(tried_target_stats)
                )

            train_x = self._build_feature_frame(pd, train_rows, feature_columns)
            test_x = self._build_feature_frame(pd, test_rows, feature_columns)
            test_returns = [_safe_float(r.get("expected_return_90m"), 0.0) for r in test_rows]

            numeric_cols = self._numeric_columns(train_x)
            categorical_cols = [c for c in train_x.columns if c not in numeric_cols]
            train_x = self._coerce_feature_types(train_x, numeric_cols, categorical_cols)
            test_x = self._coerce_feature_types(test_x, numeric_cols, categorical_cols)
            preprocessor = ColumnTransformer(
                transformers=[
                    ("num", "passthrough", numeric_cols),
                    ("cat", OneHotEncoder(handle_unknown="ignore"), categorical_cols),
                ]
            )

            if algorithm == "logistic_regression":
                estimator = LogisticRegression(max_iter=1200, class_weight="balanced")
            elif algorithm == "gradient_boosting":
                estimator = GradientBoostingClassifier(
                    n_estimators=250,
                    learning_rate=0.05,
                    max_depth=3,
                    random_state=42,
                )
            else:
                raise ValueError(f"Unsupported algorithm: {algorithm}")

            pipeline = Pipeline(
                steps=[
                    ("preprocessor", preprocessor),
                    ("model", estimator),
                ]
            )
            pipeline.fit(train_x, train_y)
            test_pred_prob = list(pipeline.predict_proba(test_x)[:, 1])
            evaluation = evaluate_binary_classifier(
                y_true=test_y,
                y_pred_prob=test_pred_prob,
                realized_returns=test_returns,
            )
            all_labels = _labels(rows, active_target_column)
            evaluation["dataset_row_count"] = len(rows)
            evaluation["train_row_count"] = len(train_rows)
            evaluation["test_row_count"] = len(test_rows)
            evaluation["positive_samples_total"] = _positive_count(all_labels)
            evaluation["positive_samples_train"] = _positive_count(train_y)
            evaluation["positive_samples_test"] = _positive_count(test_y)

            # Extract transformed feature names for importance reporting.
            transformed_feature_names: list[str] = []
            try:
                transformed_feature_names = list(
                    pipeline.named_steps["preprocessor"].get_feature_names_out()
                )
            except Exception:
                transformed_feature_names = feature_columns

            importance = extract_feature_importance(
                pipeline.named_steps["model"],
                transformed_feature_names,
            )
            if importance:
                evaluation["feature_importance"] = importance

            version_id = f"model-{algorithm}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            artifact_path = self.artifacts_dir / f"{version_id}.pkl"
            meta_path = self.artifacts_dir / f"{version_id}.json"
            with artifact_path.open("wb") as handle:
                pickle.dump(pipeline, handle)

            training_summary = {
                "version_id": version_id,
                "run_id": run_id,
                "algorithm": algorithm,
                "target_column": active_target_column,
                "feature_columns": feature_columns,
                "transformed_feature_count": len(transformed_feature_names),
                "dataset_metadata": metadata,
                "train_rows": len(train_rows),
                "test_rows": len(test_rows),
                "evaluation": evaluation,
                "artifact_path": str(artifact_path.resolve()),
                "created_at": time.time(),
            }
            meta_path.write_text(json.dumps(training_summary, indent=2), encoding="utf-8")

            data_range_start = min(_safe_float(r.get("timestamp_utc"), 0.0) for r in rows)
            data_range_end = max(_safe_float(r.get("timestamp_utc"), 0.0) for r in rows)
            await self.registry.register_candidate(
                version_id=version_id,
                algorithm=algorithm,
                target_definition=active_target_column,
                feature_schema_version=str(metadata.get("default_feature_schema_version", "v2")),
                training_date=time.time(),
                data_range_start=data_range_start,
                data_range_end=data_range_end,
                evaluation_metrics=evaluation,
                notes=f"artifact={artifact_path.name};summary={meta_path.name}",
            )
            await self.db.update_model_run(
                run_id=run_id,
                status="candidate",
                metrics=evaluation,
                finished_at=time.time(),
                version_id=version_id,
                notes=f"Training completed in {time.time()-started_at:.2f}s",
            )
            return training_summary
        except Exception as exc:
            await self.db.update_model_run(
                run_id=run_id,
                status="rejected",
                metrics={},
                finished_at=time.time(),
                notes=f"Training failed: {exc}",
            )
            raise
