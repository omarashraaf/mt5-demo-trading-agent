import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import ExternalEvent
from adapters.finnhub_adapter import FinnhubAdapter
from services.asset_mapping_service import AssetMappingService
from services.event_ingestion_service import EventIngestionService
from services.event_normalization_service import EventNormalizationService
from services.symbol_universe_service import SymbolUniverseService
from storage.db import Database


class EventServiceTests(unittest.TestCase):
    def test_normalizes_finnhub_market_news(self):
        service = EventNormalizationService()
        events = service.normalize_market_news(
            [
                {
                    "id": 11,
                    "headline": "US CPI surprises higher",
                    "summary": "Inflation runs hot",
                    "datetime": 1710000000,
                    "category": "general",
                    "related": "US500,US100",
                }
            ]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "market_news")
        self.assertIn("US500", events[0].affected_assets)

    def test_event_deduplication_uses_unique_key(self):
        service = EventNormalizationService()
        event = service.normalize_market_news(
            [
                {
                    "id": 11,
                    "headline": "US CPI surprises higher",
                    "summary": "Inflation runs hot",
                    "datetime": 1710000000,
                    "category": "general",
                }
            ]
        )[0]

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.db"))
            asyncio.run(db.initialize())
            first_id = asyncio.run(db.save_external_event(event.model_dump()))
            second_id = asyncio.run(db.save_external_event(event.model_dump()))
            saved = asyncio.run(db.get_latest_external_events(limit=10))
            asyncio.run(db.close())

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(saved), 1)

    def test_asset_mapping_prefers_indices_and_commodities_only(self):
        service = AssetMappingService()
        event = ExternalEvent(
            source="finnhub",
            source_event_id="1",
            dedupe_key="a",
            title="US CPI runs hot",
            summary="Inflation and rates pressure tech",
            timestamp_utc=time.time(),
            event_type="economic_calendar",
            importance="high",
            affected_assets=["US100", "EURUSD", "GOLD"],
            raw_payload={},
            fetched_at=time.time(),
        )
        mappings = service.map_event(event)
        symbols = {mapping.symbol for mapping in mappings}
        self.assertIn("US100", symbols)
        self.assertIn("GOLD", symbols)
        self.assertNotIn("EURUSD", symbols)

    def test_recent_symbol_news_uses_stored_events_and_assessments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.db"))
            asyncio.run(db.initialize())
            event = ExternalEvent(
                source="finnhub",
                source_event_id="evt-1",
                dedupe_key="evt-1",
                title="OPEC supply cuts support crude",
                summary="Oil supply looks tighter.",
                timestamp_utc=time.time(),
                event_type="market_news",
                importance="high",
                affected_assets=["WTI", "BRENT"],
                raw_payload={},
                fetched_at=time.time(),
            )
            event_id = asyncio.run(db.save_external_event(event.model_dump()))
            asyncio.run(
                db.save_event_asset_mappings(
                    event_id,
                    [
                        {
                            "symbol": "WTI",
                            "baseline_bias": "bullish",
                            "needs_gemini_clarification": True,
                            "tradable": True,
                            "mapping_score": 0.84,
                            "reason": "Oil catalyst.",
                        }
                    ],
                )
            )
            asyncio.run(
                db.save_gemini_event_assessment(
                    event_id,
                    {
                        "used": True,
                        "available": True,
                        "degraded": False,
                        "affected_assets": ["WTI"],
                        "bias_by_asset": {"WTI": "bullish"},
                        "event_risk": "medium",
                        "confidence_adjustment": 0.05,
                        "summary_reason": "Crude supply catalyst supports oil.",
                    },
                )
            )

            service = EventIngestionService(
                finnhub_adapter=FinnhubAdapter(enabled=False),
                normalization_service=EventNormalizationService(),
                asset_mapping_service=AssetMappingService(),
                db=db,
            )

            items = asyncio.run(
                service.recent_symbol_news(
                    "WTI",
                    universe_service=SymbolUniverseService(),
                    limit=5,
                )
            )
            asyncio.run(db.close())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].affected_symbols, ["WTI", "BRENT"])
        self.assertEqual(items[0].metadata["gemini_bias"], "bullish")

    def test_latest_candidate_symbols_prefers_event_driven_commodity_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.db"))
            asyncio.run(db.initialize())
            event = ExternalEvent(
                source="finnhub",
                source_event_id="evt-2",
                dedupe_key="evt-2",
                title="Oil inventories draw sharply",
                summary="Crude supply shock lifts oil.",
                timestamp_utc=time.time(),
                event_type="market_news",
                importance="high",
                affected_assets=["WTI"],
                raw_payload={},
                fetched_at=time.time(),
            )
            event_id = asyncio.run(db.save_external_event(event.model_dump()))
            asyncio.run(
                db.save_event_asset_mappings(
                    event_id,
                    [
                        {
                            "symbol": "WTI",
                            "baseline_bias": "bullish",
                            "needs_gemini_clarification": True,
                            "tradable": True,
                            "mapping_score": 0.86,
                            "reason": "Oil inventory catalyst.",
                        }
                    ],
                )
            )

            service = EventIngestionService(
                finnhub_adapter=FinnhubAdapter(enabled=False),
                normalization_service=EventNormalizationService(),
                asset_mapping_service=AssetMappingService(),
                db=db,
            )
            candidates = asyncio.run(
                service.latest_candidate_symbols(
                    universe_service=SymbolUniverseService(),
                    market_symbols=[
                        {"name": "WTI", "category": "Stocks", "path": "Nasdaq\\Stock\\WTI", "trade_enabled": True, "visible": True},
                        {"name": "USOIL", "category": "Commodities", "path": "Commodities\\Energy", "trade_enabled": True, "visible": True},
                    ],
                    limit=5,
                )
            )
            asyncio.run(db.close())

        self.assertEqual(candidates, ["USOIL"])
