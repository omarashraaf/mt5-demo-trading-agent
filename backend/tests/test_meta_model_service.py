import asyncio
import json
import os
import pickle
import tempfile
import time
import unittest
from pathlib import Path

from domain.models import (
    GeminiAssessment,
    MarketContext,
    NormalizedSymbolInfo,
    PortfolioRiskAssessment,
    TechnicalSignal,
    TradeDecisionAssessment,
    TradeQualityAssessment,
)
from services.meta_model_service import MetaModelService
from storage.db import Database


class _ProbModel:
    def predict_proba(self, frame):
        import numpy as np

        out = []
        for _, row in frame.iterrows():
            quality = float(row.get("f_quality_score", 0.0) or 0.0)
            p = max(0.01, min(0.99, quality))
            out.append([1.0 - p, p])
        return np.array(out)


class MetaModelServiceTests(unittest.TestCase):
    def test_meta_model_service_safe_fallback_without_approved_model(self):
        async def _run():
            fd, db_path = tempfile.mkstemp(prefix="ta-meta-", suffix=".db")
            os.close(fd)
            try:
                db = Database(db_path)
                await db.initialize()
                service = MetaModelService(db=db, artifacts_dir=tempfile.gettempdir())
                decision = TradeDecisionAssessment(
                    trade=True,
                    final_direction="BUY",
                    final_signal=TechnicalSignal(agent_name="SmartAgent", action="BUY", confidence=0.7),
                    trade_quality_assessment=TradeQualityAssessment(final_trade_quality_score=0.7, threshold=0.55),
                    reasons=[],
                )
                context = MarketContext(
                    symbol="XAUUSD",
                    user_policy={"mode": "balanced"},
                    symbol_info=NormalizedSymbolInfo(name="XAUUSD", category="Commodities"),
                    tick={"bid": 100.0, "ask": 100.1, "spread": 10.0},
                )
                updated, assessment = await service.assess_trade_decision(
                    context=context,
                    trade_decision=decision,
                    gemini_assessment=GeminiAssessment(),
                    portfolio_risk_assessment=PortfolioRiskAssessment(),
                )
                self.assertTrue(updated.trade)
                self.assertFalse(assessment["active"])
                await db.close()
            finally:
                if os.path.exists(db_path):
                    os.remove(db_path)

        asyncio.run(_run())

    def test_meta_model_service_blocks_low_probability_trade(self):
        async def _run():
            fd, db_path = tempfile.mkstemp(prefix="ta-meta-model-", suffix=".db")
            os.close(fd)
            artifacts = Path(tempfile.mkdtemp(prefix="ta-meta-artifacts-"))
            try:
                db = Database(db_path)
                await db.initialize()

                version_id = "approved-meta-v1"
                artifact_name = f"{version_id}.pkl"
                summary_name = f"{version_id}.json"
                with (artifacts / artifact_name).open("wb") as handle:
                    pickle.dump(_ProbModel(), handle)
                (artifacts / summary_name).write_text(
                    json.dumps({"feature_columns": ["f_quality_score", "f_symbol"]}),
                    encoding="utf-8",
                )
                await db.register_model_version(
                    version_id=version_id,
                    algorithm="logistic_regression",
                    target_definition="profitable_after_costs_90m",
                    feature_schema_version="v2",
                    training_date=time.time(),
                    data_range_start=time.time() - 1000,
                    data_range_end=time.time(),
                    evaluation_metrics={},
                    walk_forward_metrics={},
                    approval_status="approved",
                    notes=f"artifact={artifact_name};summary={summary_name}",
                )
                service = MetaModelService(
                    db=db,
                    artifacts_dir=str(artifacts),
                    min_profit_probability=0.55,
                    hard_block_threshold=0.35,
                )
                context = MarketContext(
                    symbol="XAUUSD",
                    user_policy={"mode": "balanced"},
                    symbol_info=NormalizedSymbolInfo(name="XAUUSD", category="Commodities"),
                    tick={"bid": 100.0, "ask": 100.1, "spread": 10.0},
                )
                decision = TradeDecisionAssessment(
                    trade=True,
                    final_direction="BUY",
                    final_signal=TechnicalSignal(
                        agent_name="SmartAgent",
                        action="BUY",
                        confidence=0.6,
                        metadata={"quality_score": 0.2},
                    ),
                    trade_quality_assessment=TradeQualityAssessment(
                        final_trade_quality_score=0.2,
                        threshold=0.5,
                    ),
                    reasons=[],
                )
                updated, assessment = await service.assess_trade_decision(
                    context=context,
                    trade_decision=decision,
                    gemini_assessment=GeminiAssessment(),
                    portfolio_risk_assessment=PortfolioRiskAssessment(),
                    anti_churn_blocked=False,
                )
                self.assertFalse(updated.trade)
                self.assertTrue(assessment["active"])
                self.assertTrue(assessment["blocked"])
                self.assertTrue((updated.final_signal.metadata or {}).get("meta_model", {}).get("blocked"))
                await db.close()
            finally:
                if os.path.exists(db_path):
                    os.remove(db_path)
                for file in artifacts.glob("*"):
                    file.unlink(missing_ok=True)
                artifacts.rmdir()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
