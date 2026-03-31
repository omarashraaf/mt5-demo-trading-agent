from __future__ import annotations

import json
import pickle
import time
import uuid
from pathlib import Path

from research.model_evaluator import evaluate_binary_classifier, extract_feature_importance
from research.model_registry import ModelRegistry
from research.trade_dataset_builder import TradeDatasetBuilder


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
        ordered = sorted(rows, key=lambda r: float(r.get("timestamp_utc") or 0.0))
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
            frame[col] = frame[col].replace("", 0.0).fillna(0.0)
            frame[col] = frame[col].astype(float)
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
                return float(text)
            except (TypeError, ValueError):
                return None

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
            if parsed.notna().sum() == numeric:
                cols.append(col)
        return cols

    async def train_candidate_model(
        self,
        *,
        algorithm: str = "logistic_regression",
        target_column: str = "profitable_after_costs_90m",
        include_unexecuted: bool = True,
        min_rows: int = 30,
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
            },
        )
        started_at = time.time()
        try:
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

            train_rows, test_rows = self._time_split(rows, test_ratio=0.2)
            feature_columns = sorted([k for k in rows[0].keys() if k.startswith("f_")])
            if not feature_columns:
                raise RuntimeError("No feature columns found in dataset.")

            train_x = self._build_feature_frame(pd, train_rows, feature_columns)
            test_x = self._build_feature_frame(pd, test_rows, feature_columns)
            train_y = [int(float(r.get(target_column, 0) or 0) >= 0.5) for r in train_rows]
            test_y = [int(float(r.get(target_column, 0) or 0) >= 0.5) for r in test_rows]
            active_target_column = target_column
            # Time split can produce one-class train sets when positives are sparse/recent.
            # Fallback to stratified split before downgrading to no_trade_label.
            if len(set(train_y)) < 2:
                all_y = [int(float(r.get(target_column, 0) or 0) >= 0.5) for r in rows]
                if len(set(all_y)) >= 2:
                    train_rows, test_rows = train_test_split(
                        rows,
                        test_size=0.2,
                        random_state=42,
                        shuffle=True,
                        stratify=all_y,
                    )
                    train_x = self._build_feature_frame(pd, train_rows, feature_columns)
                    test_x = self._build_feature_frame(pd, test_rows, feature_columns)
                    train_y = [int(float(r.get(target_column, 0) or 0) >= 0.5) for r in train_rows]
                    test_y = [int(float(r.get(target_column, 0) or 0) >= 0.5) for r in test_rows]
            if len(set(train_y)) < 2 and target_column != "no_trade_label":
                fallback_train_y = [int(float(r.get("no_trade_label", 0) or 0) >= 0.5) for r in train_rows]
                fallback_test_y = [int(float(r.get("no_trade_label", 0) or 0) >= 0.5) for r in test_rows]
                if len(set(fallback_train_y)) >= 2:
                    train_y = fallback_train_y
                    test_y = fallback_test_y
                    active_target_column = "no_trade_label"
                else:
                    raise RuntimeError(
                        f"Training labels have one class for {target_column} and no_trade_label. "
                        "Need more varied historical outcomes."
                    )
            test_returns = [float(r.get("expected_return_90m", 0.0) or 0.0) for r in test_rows]

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

            data_range_start = min(float(r.get("timestamp_utc") or 0.0) for r in rows)
            data_range_end = max(float(r.get("timestamp_utc") or 0.0) for r in rows)
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
