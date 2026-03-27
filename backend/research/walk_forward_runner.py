from __future__ import annotations

import statistics
import time
import uuid

import pandas as pd

from research.model_evaluator import evaluate_binary_classifier
from research.replay_runner import _compute_metrics, _safe_float
from research.trade_dataset_builder import TradeDatasetBuilder


class WalkForwardRunner:
    def __init__(self, db):
        self.db = db
        self.dataset_builder = TradeDatasetBuilder(db)

    def _require_sklearn(self):
        try:
            from sklearn.compose import ColumnTransformer
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import Pipeline
            from sklearn.preprocessing import OneHotEncoder
        except Exception as exc:
            raise RuntimeError(
                "scikit-learn is required for walk-forward evaluation."
            ) from exc
        return {
            "Pipeline": Pipeline,
            "ColumnTransformer": ColumnTransformer,
            "OneHotEncoder": OneHotEncoder,
            "LogisticRegression": LogisticRegression,
            "GradientBoostingClassifier": GradientBoostingClassifier,
        }

    @staticmethod
    def _numeric_columns(frame) -> list[str]:
        numeric_kinds = {"i", "u", "f", "b"}
        return [c for c in frame.columns if getattr(frame[c].dtype, "kind", None) in numeric_kinds]

    @staticmethod
    def _window_slices(rows: list[dict], windows: int, train_ratio: float = 0.7, test_ratio: float = 0.2):
        ordered = sorted(rows, key=lambda r: _safe_float(r.get("timestamp_utc", 0.0), 0.0))
        n = len(ordered)
        if n < 30:
            return []
        train_size = max(10, int(n * train_ratio))
        test_size = max(5, int(n * test_ratio))
        step = max(5, int((n - train_size - test_size) / max(1, windows - 1)))
        slices = []
        start = 0
        for _ in range(windows):
            train_start = start
            train_end = train_start + train_size
            test_end = train_end + test_size
            if test_end > n:
                break
            slices.append((train_start, train_end, train_end, test_end))
            start += step
        return [(ordered[a:b], ordered[c:d]) for a, b, c, d in slices]

    async def run_walk_forward(
        self,
        *,
        algorithm: str = "logistic_regression",
        target_column: str = "profitable_after_costs_90m",
        score_threshold: float = 0.55,
        windows: int = 5,
        include_unexecuted: bool = True,
        limit: int = 200000,
    ) -> dict:
        run_id = f"walkfwd-{uuid.uuid4().hex[:12]}"
        started_at = time.time()
        config = {
            "algorithm": algorithm,
            "target_column": target_column,
            "score_threshold": score_threshold,
            "windows": windows,
            "include_unexecuted": include_unexecuted,
            "limit": limit,
        }
        await self.db.log_replay_run(
            run_id=run_id,
            model_version_id="",
            config=config,
            status="running",
            started_at=started_at,
            notes="walk_forward",
        )
        try:
            libs = self._require_sklearn()
            Pipeline = libs["Pipeline"]
            ColumnTransformer = libs["ColumnTransformer"]
            OneHotEncoder = libs["OneHotEncoder"]
            LogisticRegression = libs["LogisticRegression"]
            GradientBoostingClassifier = libs["GradientBoostingClassifier"]

            rows, _ = await self.dataset_builder.build_dataset(
                limit=limit,
                include_unexecuted=include_unexecuted,
            )
            if not rows:
                raise RuntimeError("No dataset rows available for walk-forward.")

            feature_columns = sorted([k for k in rows[0].keys() if k.startswith("f_")])
            if not feature_columns:
                raise RuntimeError("No feature columns in dataset for walk-forward.")

            windows_data = self._window_slices(rows, windows=windows)
            if not windows_data:
                raise RuntimeError("Not enough rows to construct walk-forward windows.")

            per_window = []
            for idx, (train_rows, test_rows) in enumerate(windows_data, start=1):
                train_x = pd.DataFrame([{c: r.get(c, "") for c in feature_columns} for r in train_rows])
                test_x = pd.DataFrame([{c: r.get(c, "") for c in feature_columns} for r in test_rows])
                train_y = [int(_safe_float(r.get(target_column, 0.0), 0.0) >= 0.5) for r in train_rows]
                test_y = [int(_safe_float(r.get(target_column, 0.0), 0.0) >= 0.5) for r in test_rows]
                numeric_cols = self._numeric_columns(train_x)
                categorical_cols = [c for c in train_x.columns if c not in numeric_cols]
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
                        n_estimators=200,
                        learning_rate=0.05,
                        max_depth=3,
                        random_state=42,
                    )
                else:
                    raise ValueError(f"Unsupported algorithm: {algorithm}")

                model = Pipeline([("preprocessor", preprocessor), ("model", estimator)])
                model.fit(train_x, train_y)
                probs = list(model.predict_proba(test_x)[:, 1])
                classification = evaluate_binary_classifier(
                    y_true=test_y,
                    y_pred_prob=probs,
                    realized_returns=[_safe_float(r.get("expected_return_90m", 0.0), 0.0) for r in test_rows],
                )

                baseline_trades = []
                meta_trades = []
                for row, prob in zip(test_rows, probs):
                    if int(row.get("executed", 0)) != 1:
                        continue
                    trade = {
                        "timestamp_utc": row.get("timestamp_utc"),
                        "symbol": row.get("symbol"),
                        "event_type": row.get("event_type"),
                        "hold_bucket": row.get("hold_bucket"),
                        "pnl": _safe_float(row.get("expected_return_180m", 0.0), 0.0),
                    }
                    baseline_trades.append(trade)
                    if _safe_float(prob, 0.0) >= score_threshold:
                        meta_trades.append(trade)

                baseline_metrics = _compute_metrics(baseline_trades)
                meta_metrics = _compute_metrics(meta_trades)
                per_window.append(
                    {
                        "window_index": idx,
                        "train_rows": len(train_rows),
                        "test_rows": len(test_rows),
                        "classification": classification,
                        "baseline": baseline_metrics,
                        "with_meta_model_filter": meta_metrics,
                        "delta_expectancy": meta_metrics["expectancy"] - baseline_metrics["expectancy"],
                        "delta_drawdown": meta_metrics["max_drawdown"] - baseline_metrics["max_drawdown"],
                    }
                )

            mean_accuracy = statistics.mean([w["classification"]["accuracy"] for w in per_window])
            std_accuracy = statistics.pstdev([w["classification"]["accuracy"] for w in per_window]) if len(per_window) > 1 else 0.0
            mean_delta_expectancy = statistics.mean([w["delta_expectancy"] for w in per_window])
            mean_delta_drawdown = statistics.mean([w["delta_drawdown"] for w in per_window])
            report = {
                "run_id": run_id,
                "algorithm": algorithm,
                "target_column": target_column,
                "score_threshold": score_threshold,
                "windows": per_window,
                "aggregate": {
                    "window_count": len(per_window),
                    "mean_accuracy": mean_accuracy,
                    "std_accuracy": std_accuracy,
                    "mean_delta_expectancy": mean_delta_expectancy,
                    "mean_delta_drawdown": mean_delta_drawdown,
                },
                "completed_at": time.time(),
            }
            await self.db.update_replay_run(
                run_id=run_id,
                status="completed",
                metrics=report,
                finished_at=time.time(),
                notes="walk_forward_completed",
            )
            return report
        except Exception as exc:
            await self.db.update_replay_run(
                run_id=run_id,
                status="failed",
                finished_at=time.time(),
                notes=f"walk_forward_failed: {exc}",
            )
            raise
