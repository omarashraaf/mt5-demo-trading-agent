import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import ExternalEvent
from adapters.benzinga_adapter import BenzingaAdapter, BenzingaAdapterError
from services.asset_mapping_service import AssetMappingService
from services.event_ingestion_service import EventIngestionService
from services.event_normalization_service import EventNormalizationService
from services.symbol_universe_service import SymbolUniverseService
from storage.db import Database


class EventServiceTests(unittest.TestCase):
    def test_benzinga_uses_header_auth_without_query_token(self):
        class Response:
            status_code = 200

            def json(self):
                return []

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        adapter = BenzingaAdapter(api_key="secret-key", enabled=True, session=session)

        self.assertEqual(adapter.get_market_news(limit=1), [])
        _, request = session.calls[0]
        self.assertNotIn("token", request["params"])
        self.assertEqual(request["headers"]["Authorization"], "token secret-key")

    def test_benzinga_redacts_key_from_provider_errors(self):
        class Response:
            status_code = 401
            text = 'invalid token=secret-key Authorization: token secret-key'

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        adapter = BenzingaAdapter(api_key="secret-key", enabled=True, session=Session())

        with self.assertRaises(BenzingaAdapterError) as captured:
            adapter.get_market_news(limit=1)

        self.assertNotIn("secret-key", str(captured.exception))
        self.assertIn("[REDACTED]", str(captured.exception))

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
                benzinga_adapter=BenzingaAdapter(enabled=False),
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

    def test_recent_symbol_news_sanitizes_malformed_confidence_adjustment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.db"))
            asyncio.run(db.initialize())
            try:
                event = ExternalEvent(
                    source="finnhub",
                    source_event_id="evt-bad-confidence",
                    dedupe_key="evt-bad-confidence",
                    title="Fed comments move indices",
                    summary="Policy comments create uncertainty.",
                    timestamp_utc=time.time(),
                    event_type="market_news",
                    importance="high",
                    affected_assets=["US100"],
                    raw_payload={},
                    fetched_at=time.time(),
                )
                event_id = asyncio.run(db.save_external_event(event.model_dump()))
                asyncio.run(
                    db.save_event_asset_mappings(
                        event_id,
                        [
                            {
                                "symbol": "US100",
                                "baseline_bias": "bearish",
                                "needs_gemini_clarification": True,
                                "tradable": True,
                                "mapping_score": 0.84,
                                "reason": "Index catalyst.",
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
                            "affected_assets": ["US100"],
                            "bias_by_asset": {"US100": "bearish"},
                            "event_risk": "high",
                            "confidence_adjustment": "bad-adjustment",
                            "summary_reason": "Unclear policy path.",
                        },
                    )
                )

                service = EventIngestionService(
                    benzinga_adapter=BenzingaAdapter(enabled=False),
                    normalization_service=EventNormalizationService(),
                    asset_mapping_service=AssetMappingService(),
                    db=db,
                )

                items = asyncio.run(
                    service.recent_symbol_news(
                        "US100",
                        universe_service=SymbolUniverseService(),
                        limit=5,
                    )
                )
            finally:
                asyncio.run(db.close())

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].metadata["gemini_confidence_adjustment"], 0.0)

    def test_malformed_event_timestamp_is_not_recent(self):
        service = EventIngestionService(
            benzinga_adapter=BenzingaAdapter(enabled=False),
            normalization_service=EventNormalizationService(),
            asset_mapping_service=AssetMappingService(),
        )

        self.assertFalse(
            service._is_recent_event(
                {"timestamp_utc": "bad-timestamp"},
                max_event_age_hours=24.0,
            )
        )

    def test_recent_symbol_news_sanitizes_malformed_item_timestamps(self):
        service = EventIngestionService(
            benzinga_adapter=BenzingaAdapter(enabled=False),
            normalization_service=EventNormalizationService(),
            asset_mapping_service=AssetMappingService(),
        )
        event = {
            "id": 1,
            "source": "benzinga",
            "title": "Rates headline moves index futures",
            "summary": "Policy uncertainty rises.",
            "timestamp_utc": "bad-published-at",
            "fetched_at": "bad-received-at",
            "event_type": "market_news",
            "importance": "high",
            "affected_assets": ["US100"],
        }

        item = service._normalized_news_item_for_event(
            event=event,
            mappings=[],
            assessment=None,
            canonical_symbol="US100",
            universe_service=SymbolUniverseService(),
        )

        self.assertGreater(item.published_at, 0.0)
        self.assertGreater(item.received_at, 0.0)

    def test_load_recent_event_bundles_skips_malformed_foreign_keys(self):
        class FakeEventDb:
            async def get_latest_external_events(self, *, limit, usable_only=True):
                return [{"id": 7, "title": "Valid event"}]

            async def get_event_asset_mappings(self, *, limit):
                return [
                    {"external_event_id": "bad-event-id", "symbol": "US100"},
                    {"external_event_id": 7, "symbol": "US500"},
                ]

            async def get_latest_gemini_event_assessments(self, *, limit):
                return [
                    {"external_event_id": "bad-assessment-id", "assessment_json": {"event_risk": "high"}},
                    {"external_event_id": 7, "assessment_json": {"event_risk": "medium"}},
                ]

        service = EventIngestionService(
            benzinga_adapter=BenzingaAdapter(enabled=False),
            normalization_service=EventNormalizationService(),
            asset_mapping_service=AssetMappingService(),
            db=FakeEventDb(),
        )

        bundles = asyncio.run(service._load_recent_event_bundles(limit=10))

        self.assertEqual(len(bundles), 1)
        event, mappings, assessment = bundles[0]
        self.assertEqual(event["id"], 7)
        self.assertEqual(mappings, [{"external_event_id": 7, "symbol": "US500"}])
        self.assertEqual(assessment, {"event_risk": "medium"})

    def test_load_recent_event_bundles_skips_malformed_assessment_payloads(self):
        class FakeEventDb:
            async def get_latest_external_events(self, *, limit, usable_only=True):
                return [{"id": 9, "title": "Valid event"}]

            async def get_event_asset_mappings(self, *, limit):
                return [{"external_event_id": 9, "symbol": "US100"}]

            async def get_latest_gemini_event_assessments(self, *, limit):
                return [
                    {"external_event_id": 9},
                    {"external_event_id": 9, "assessment_json": "not-a-dict"},
                ]

        service = EventIngestionService(
            benzinga_adapter=BenzingaAdapter(enabled=False),
            normalization_service=EventNormalizationService(),
            asset_mapping_service=AssetMappingService(),
            db=FakeEventDb(),
        )

        bundles = asyncio.run(service._load_recent_event_bundles(limit=10))

        self.assertEqual(len(bundles), 1)
        event, mappings, assessment = bundles[0]
        self.assertEqual(event["id"], 9)
        self.assertEqual(mappings, [{"external_event_id": 9, "symbol": "US100"}])
        self.assertIsNone(assessment)

    def test_event_helpers_treat_malformed_bias_by_asset_as_neutral(self):
        service = EventIngestionService(
            benzinga_adapter=BenzingaAdapter(enabled=False),
            normalization_service=EventNormalizationService(),
            asset_mapping_service=AssetMappingService(),
        )
        universe = SymbolUniverseService()
        assessment = {
            "affected_assets": [],
            "bias_by_asset": "not-a-dict",
        }

        self.assertFalse(
            service._event_relevant_to_symbol(
                canonical_symbol="US100",
                event={"affected_assets": []},
                mappings=[],
                assessment=assessment,
                universe_service=universe,
            )
        )
        self.assertEqual(
            service._affected_symbols_for_event(
                event={"affected_assets": []},
                mappings=[],
                assessment=assessment,
                universe_service=universe,
            ),
            [],
        )
        self.assertEqual(service._assessment_bias_for_symbol(assessment, "US100"), "neutral")

    def test_event_helpers_ignore_scalar_affected_assets(self):
        service = EventIngestionService(
            benzinga_adapter=BenzingaAdapter(enabled=False),
            normalization_service=EventNormalizationService(),
            asset_mapping_service=AssetMappingService(),
        )
        universe = SymbolUniverseService()
        assessment = {
            "affected_assets": "US100",
            "bias_by_asset": {},
        }

        self.assertFalse(
            service._event_relevant_to_symbol(
                canonical_symbol="US100",
                event={"affected_assets": "US100"},
                mappings=[],
                assessment=assessment,
                universe_service=universe,
            )
        )
        self.assertEqual(
            service._affected_symbols_for_event(
                event={"affected_assets": "US100"},
                mappings=[],
                assessment=assessment,
                universe_service=universe,
            ),
            [],
        )

    def test_event_helpers_ignore_malformed_mapping_rows(self):
        service = EventIngestionService(
            benzinga_adapter=BenzingaAdapter(enabled=False),
            normalization_service=EventNormalizationService(),
            asset_mapping_service=AssetMappingService(),
        )
        universe = SymbolUniverseService()

        self.assertTrue(
            service._event_relevant_to_symbol(
                canonical_symbol="US100",
                event={"affected_assets": []},
                mappings=["bad-mapping", {"symbol": "US100"}],
                assessment=None,
                universe_service=universe,
            )
        )
        self.assertEqual(
            service._affected_symbols_for_event(
                event={"affected_assets": []},
                mappings=["bad-mapping", {"symbol": "US100"}],
                assessment=None,
                universe_service=universe,
            ),
            ["US100"],
        )

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
                benzinga_adapter=BenzingaAdapter(enabled=False),
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

    def test_latest_candidate_symbols_ignores_malformed_market_symbol_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.db"))
            asyncio.run(db.initialize())
            try:
                event = ExternalEvent(
                    source="benzinga",
                    source_event_id="evt-broker-row",
                    dedupe_key="evt-broker-row",
                    title="Nasdaq futures rally on AI earnings",
                    summary="Tech index catalyst.",
                    timestamp_utc=time.time(),
                    event_type="market_news",
                    importance="high",
                    affected_assets=["US100"],
                    raw_payload={},
                    fetched_at=time.time(),
                )
                event_id = asyncio.run(db.save_external_event(event.model_dump()))
                asyncio.run(
                    db.save_event_asset_mappings(
                        event_id,
                        [
                            {
                                "symbol": "US100",
                                "baseline_bias": "bullish",
                                "needs_gemini_clarification": True,
                                "tradable": True,
                                "mapping_score": 0.86,
                                "reason": "Index catalyst.",
                            }
                        ],
                    )
                )

                service = EventIngestionService(
                    benzinga_adapter=BenzingaAdapter(enabled=False),
                    normalization_service=EventNormalizationService(),
                    asset_mapping_service=AssetMappingService(),
                    db=db,
                )
                candidates = asyncio.run(
                    service.latest_candidate_symbols(
                        universe_service=SymbolUniverseService(),
                        market_symbols=[
                            "bad-market-row",
                            {"name": "US100", "category": "Indices", "path": "Indices\\US", "trade_enabled": True, "visible": True},
                        ],
                        limit=5,
                    )
                )
            finally:
                asyncio.run(db.close())

        self.assertEqual(candidates, ["US100"])
