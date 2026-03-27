from __future__ import annotations

import math
from typing import Iterable


def _safe_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        if math.isnan(parsed) or math.isinf(parsed):
            return default
        return parsed
    except (TypeError, ValueError):
        return default


def _binary_labels(values: Iterable) -> list[int]:
    return [1 if _safe_float(v, 0.0) >= 0.5 else 0 for v in values]


def _accuracy(y_true: list[int], y_pred: list[int]) -> float:
    if not y_true:
        return 0.0
    correct = sum(1 for a, b in zip(y_true, y_pred) if a == b)
    return correct / len(y_true)


def _precision(y_true: list[int], y_pred: list[int]) -> float:
    tp = sum(1 for a, b in zip(y_true, y_pred) if a == 1 and b == 1)
    fp = sum(1 for a, b in zip(y_true, y_pred) if a == 0 and b == 1)
    if tp + fp == 0:
        return 0.0
    return tp / (tp + fp)


def _recall(y_true: list[int], y_pred: list[int]) -> float:
    tp = sum(1 for a, b in zip(y_true, y_pred) if a == 1 and b == 1)
    fn = sum(1 for a, b in zip(y_true, y_pred) if a == 1 and b == 0)
    if tp + fn == 0:
        return 0.0
    return tp / (tp + fn)


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def _brier_score(y_true: list[int], probs: list[float]) -> float:
    if not y_true:
        return 0.0
    errors = [(p - y) ** 2 for y, p in zip(y_true, probs)]
    return float(sum(errors) / len(errors))


def _bucket_index(prob: float, buckets: int) -> int:
    p = max(0.0, min(0.999999, _safe_float(prob, 0.0)))
    return int(p * buckets)


def _calibration_table(y_true: list[int], probs: list[float], buckets: int = 10) -> list[dict]:
    stats = [{"count": 0, "pred_sum": 0.0, "true_sum": 0.0} for _ in range(buckets)]
    for y, p in zip(y_true, probs):
        idx = _bucket_index(p, buckets)
        stats[idx]["count"] += 1
        stats[idx]["pred_sum"] += p
        stats[idx]["true_sum"] += y
    table = []
    for idx, item in enumerate(stats):
        count = item["count"]
        if count == 0:
            continue
        table.append(
            {
                "bucket": idx,
                "count": count,
                "avg_predicted_probability": item["pred_sum"] / count,
                "observed_positive_rate": item["true_sum"] / count,
            }
        )
    return table


def _expected_value_buckets(probs: list[float], realized_returns: list[float], buckets: int = 10) -> list[dict]:
    stats = [{"count": 0, "score_sum": 0.0, "ret_sum": 0.0} for _ in range(buckets)]
    for score, realized in zip(probs, realized_returns):
        idx = _bucket_index(score, buckets)
        stats[idx]["count"] += 1
        stats[idx]["score_sum"] += _safe_float(score, 0.0)
        stats[idx]["ret_sum"] += _safe_float(realized, 0.0)
    output = []
    for idx, item in enumerate(stats):
        count = item["count"]
        if count == 0:
            continue
        output.append(
            {
                "bucket": idx,
                "count": count,
                "avg_score": item["score_sum"] / count,
                "avg_realized_return": item["ret_sum"] / count,
            }
        )
    return output


def evaluate_binary_classifier(
    *,
    y_true: list,
    y_pred_prob: list[float],
    realized_returns: list[float] | None = None,
) -> dict:
    binary_true = _binary_labels(y_true)
    probs = [_safe_float(p, 0.0) for p in y_pred_prob]
    preds = [1 if p >= 0.5 else 0 for p in probs]

    precision = _precision(binary_true, preds)
    recall = _recall(binary_true, preds)
    confusion = {
        "tp": sum(1 for a, b in zip(binary_true, preds) if a == 1 and b == 1),
        "tn": sum(1 for a, b in zip(binary_true, preds) if a == 0 and b == 0),
        "fp": sum(1 for a, b in zip(binary_true, preds) if a == 0 and b == 1),
        "fn": sum(1 for a, b in zip(binary_true, preds) if a == 1 and b == 0),
    }
    realized = realized_returns or [0.0] * len(probs)

    return {
        "sample_count": len(binary_true),
        "accuracy": _accuracy(binary_true, preds),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "brier_score": _brier_score(binary_true, probs),
        "confusion_matrix": confusion,
        "calibration": _calibration_table(binary_true, probs, buckets=10),
        "expected_value_by_score_bucket": _expected_value_buckets(probs, realized, buckets=10),
    }


def evaluate_regression(*, y_true: list[float], y_pred: list[float]) -> dict:
    truth = [_safe_float(v, 0.0) for v in y_true]
    pred = [_safe_float(v, 0.0) for v in y_pred]
    if not truth:
        return {"sample_count": 0, "mae": 0.0, "rmse": 0.0, "bias": 0.0}
    errors = [p - t for p, t in zip(pred, truth)]
    mae = sum(abs(e) for e in errors) / len(errors)
    mse = sum(e * e for e in errors) / len(errors)
    rmse = math.sqrt(mse)
    bias = sum(errors) / len(errors)
    return {
        "sample_count": len(truth),
        "mae": mae,
        "rmse": rmse,
        "bias": bias,
    }


def extract_feature_importance(model, feature_names: list[str]) -> list[dict]:
    if hasattr(model, "feature_importances_"):
        scores = list(getattr(model, "feature_importances_"))
    elif hasattr(model, "coef_"):
        coef = getattr(model, "coef_")
        scores = list(coef[0]) if hasattr(coef, "__len__") else []
    else:
        return []
    pairs = []
    for idx, score in enumerate(scores):
        if idx >= len(feature_names):
            break
        pairs.append({"feature": feature_names[idx], "importance": float(abs(_safe_float(score, 0.0)))})
    pairs.sort(key=lambda x: x["importance"], reverse=True)
    return pairs[:30]
