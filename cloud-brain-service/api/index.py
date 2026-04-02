from fastapi import FastAPI

app = FastAPI(title="LinkTrade Cloud Brain", version="1.0.0")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "linktrade-cloud-brain"}


@app.post("/api/cloud-brain/decide")
async def cloud_brain_decide(payload: dict):
    signal = dict(payload.get("signal") or {})
    quality = dict(payload.get("quality") or {})
    user_policy = dict(payload.get("user_policy") or {})
    gemini = dict(payload.get("gemini") or {}) if isinstance(payload.get("gemini"), dict) else {}
    meta_model = dict(payload.get("meta_model") or {}) if isinstance(payload.get("meta_model"), dict) else {}

    action = str(signal.get("action", "HOLD")).upper()
    confidence = float(signal.get("confidence", 0.0) or 0.0)
    quality_score = float(quality.get("score", 0.0) or 0.0)
    no_trade_zone = bool(quality.get("no_trade_zone", False))
    no_trade_reasons = list(quality.get("no_trade_reasons") or [])
    mode = str(user_policy.get("mode", "balanced")).lower()

    min_conf_by_mode = {"safe": 0.70, "balanced": 0.60, "aggressive": 0.45}
    min_quality_by_mode = {"safe": 0.74, "balanced": 0.66, "aggressive": 0.55}
    min_conf = min_conf_by_mode.get(mode, 0.60)
    min_quality = min_quality_by_mode.get(mode, 0.66)

    reasons = []
    final_action = action if action in {"BUY", "SELL"} else "HOLD"

    if no_trade_zone:
        final_action = "HOLD"
        reasons.append("No-trade zone from local quality gate.")
        reasons.extend(no_trade_reasons[:2])

    if confidence < min_conf:
        final_action = "HOLD"
        reasons.append(f"Confidence below {min_conf:.0%} threshold for {mode} mode.")

    if quality_score < min_quality:
        final_action = "HOLD"
        reasons.append(f"Quality below {min_quality:.0%} threshold for {mode} mode.")

    contradiction = bool(gemini.get("contradiction_flag", False))
    if contradiction and mode in {"safe", "balanced"}:
        final_action = "HOLD"
        reasons.append("Gemini contradiction in non-aggressive mode.")

    no_trade_prob = float(meta_model.get("no_trade_probability", 0.0) or 0.0)
    if no_trade_prob >= 0.60:
        final_action = "HOLD"
        reasons.append("Meta-model no-trade probability is high.")

    target_confidence = confidence
    if final_action == "HOLD":
        target_confidence = min(confidence, max(0.0, confidence - 0.08))
    else:
        target_confidence = max(confidence, min(0.95, confidence + 0.02))

    return {
        "source": "cloud_brain",
        "action": final_action,
        "confidence": round(float(target_confidence), 4),
        "reason": "; ".join(reasons) if reasons else "Cloud brain approved local setup.",
        "mode": mode,
        "thresholds": {"min_confidence": min_conf, "min_quality": min_quality},
    }
