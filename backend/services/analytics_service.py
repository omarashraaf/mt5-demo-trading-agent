from __future__ import annotations

from collections import defaultdict
from statistics import median


class AnalyticsService:
    HOLDING_BUCKETS = {
        "0-15 min": (0.0, 15.0),
        "15-30 min": (15.0, 30.0),
        "30-60 min": (30.0, 60.0),
        "60-180 min": (60.0, 180.0),
        "180+ min": (180.0, float("inf")),
    }

    def confidence_calibration(self, trade_outcomes: list[dict]) -> dict:
        buckets = {
            "55-60": (0.55, 0.60),
            "60-65": (0.60, 0.65),
            "65-70": (0.65, 0.70),
            "70-75": (0.70, 0.75),
            "75+": (0.75, 1.01),
        }
        grouped: dict[str, list[dict]] = defaultdict(list)
        for outcome in trade_outcomes:
            confidence = float(outcome.get("confidence", 0.0))
            for label, (lower, upper) in buckets.items():
                if lower <= confidence < upper:
                    grouped[label].append(outcome)
                    break

        results = {}
        threshold_recommendation = 0.65
        for label, rows in grouped.items():
            results[label] = self._summarize_rows(rows)

        low_buckets = ["55-60", "60-65"]
        if any(results.get(bucket, {}).get("avg_return", 0.0) < 0 for bucket in low_buckets):
            threshold_recommendation = 0.70
        if any(results.get(bucket, {}).get("profit_factor", 1.0) < 1.0 for bucket in low_buckets):
            threshold_recommendation = max(threshold_recommendation, 0.72)

        return {
            "buckets": results,
            "recommended_min_confidence": threshold_recommendation,
        }

    def holding_time_analysis(self, trade_outcomes: list[dict]) -> dict:
        bucketed: dict[str, list[dict]] = defaultdict(list)
        by_category: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))

        for outcome in trade_outcomes:
            holding_minutes = float(outcome.get("holding_minutes", 0.0) or 0.0)
            bucket = self._holding_bucket_label(holding_minutes)
            bucketed[bucket].append(outcome)
            category = str(outcome.get("symbol_category", "") or "Unknown")
            by_category[category][bucket].append(outcome)

        bucket_metrics = {
            label: self._summarize_rows(bucketed.get(label, []))
            for label in self.HOLDING_BUCKETS
        }
        category_recommendations = {
            category: self._recommend_hold_for_category(bucket_rows)
            for category, bucket_rows in by_category.items()
        }
        return {
            "buckets": bucket_metrics,
            "recommendations_by_category": category_recommendations,
        }

    def _recommend_hold_for_category(self, bucket_rows: dict[str, list[dict]]) -> dict:
        metrics_by_bucket = {
            label: self._summarize_rows(rows)
            for label, rows in bucket_rows.items()
        }
        scored_buckets = []
        for label, rows in bucket_rows.items():
            metrics = metrics_by_bucket[label]
            count = metrics["count"]
            if count == 0:
                continue
            avg_return = metrics["avg_return"]
            profit_factor = metrics["profit_factor"]
            score = avg_return * max(1.0, min(profit_factor, 3.0)) * count
            scored_buckets.append((score, label, rows))

        if not scored_buckets:
            return {
                "recommended_max_hold_minutes": None,
                "best_bucket": None,
                "reason": "Not enough closed trades for this category yet.",
                "bucket_metrics": metrics_by_bucket,
            }

        _, best_bucket, rows = max(scored_buckets, key=lambda item: item[0])
        recommended_minutes = self._bucket_recommendation_minutes(best_bucket, rows)
        metrics = metrics_by_bucket[best_bucket]
        return {
            "recommended_max_hold_minutes": recommended_minutes,
            "best_bucket": best_bucket,
            "reason": (
                f"Best bucket has avg return {metrics['avg_return']:.4f}, "
                f"profit factor {metrics['profit_factor']:.3f}, and {metrics['count']} trades."
            ),
            "bucket_metrics": metrics_by_bucket,
        }

    def _bucket_recommendation_minutes(self, bucket_label: str, rows: list[dict]) -> int | None:
        lower, upper = self.HOLDING_BUCKETS[bucket_label]
        if upper != float("inf"):
            return int(upper)
        holds = [float(row.get("holding_minutes", 0.0) or 0.0) for row in rows if float(row.get("holding_minutes", 0.0) or 0.0) >= lower]
        return int(round(median(holds))) if holds else 240

    def _holding_bucket_label(self, holding_minutes: float) -> str:
        for label, (lower, upper) in self.HOLDING_BUCKETS.items():
            if lower <= holding_minutes < upper:
                return label
        return "180+ min"

    def _summarize_rows(self, rows: list[dict]) -> dict:
        profits = [float(row.get("profit", 0.0)) for row in rows]
        wins = [profit for profit in profits if profit > 0]
        losses = [abs(profit) for profit in profits if profit < 0]
        avg_return = sum(profits) / len(profits) if profits else 0.0
        win_rate = len(wins) / len(profits) if profits else 0.0
        gross_profit = sum(wins)
        gross_loss = sum(losses)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        return {
            "count": len(rows),
            "win_rate": round(win_rate, 3),
            "avg_return": round(avg_return, 4),
            "profit_factor": round(profit_factor, 3),
        }
