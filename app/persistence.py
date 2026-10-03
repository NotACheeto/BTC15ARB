"""Local database persistence layer using SQLite.

Records all opportunities, equivalence results, trade executions,
orphan recovery actions, and system audit events with nanosecond precision.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import sqlite3
from typing import Any

from app.models import (
    ArbitrageOpportunity,
    ArbitrageTradeRecord,
    MarketEquivalenceResult,
    RejectedOpportunityRecord,
)

logger = logging.getLogger(__name__)


class DatabaseManager:
    """SQLite persistence service for trades, opportunities, and safety audits."""

    def __init__(self, db_path: str = "btc_arbitrage.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create tables if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Market matches & equivalence audits
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS market_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                is_equivalent INTEGER NOT NULL,
                polymarket_id TEXT,
                kalshi_id TEXT,
                reason TEXT,
                details_json TEXT,
                checked_at TEXT NOT NULL
            );
            """)

            # 2. Executed Trades
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_trades (
                trade_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                market_window TEXT NOT NULL,
                direction TEXT NOT NULL,
                is_hedged INTEGER NOT NULL,
                recovery_action TEXT,
                total_cost TEXT NOT NULL,
                payout_expected TEXT NOT NULL,
                fees_paid TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                latencies_json TEXT,
                created_at TEXT NOT NULL
            );
            """)

            # 3. Trade Legs (Order executions)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                order_id TEXT,
                client_order_id TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                price TEXT NOT NULL,
                fill_price TEXT,
                quantity INTEGER NOT NULL,
                fill_quantity INTEGER NOT NULL,
                fee_paid TEXT NOT NULL,
                submitted_at_ns INTEGER,
                ack_at_ns INTEGER,
                filled_at_ns INTEGER,
                raw_response_json TEXT,
                FOREIGN KEY(trade_id) REFERENCES arbitrage_trades(trade_id)
            );
            """)

            # 4. Rejected Opportunities
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS rejected_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id TEXT NOT NULL,
                market_window TEXT NOT NULL,
                direction TEXT NOT NULL,
                raw_combined_cost TEXT NOT NULL,
                theoretical_gross_edge TEXT NOT NULL,
                conservative_net_edge TEXT NOT NULL,
                rejection_reason TEXT NOT NULL,
                detected_at TEXT NOT NULL
            );
            """)

            # 5. System State Transitions & Kill Switch Audits
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                description TEXT NOT NULL,
                details_json TEXT,
                created_at TEXT NOT NULL
            );
            """)

            conn.commit()

    async def log_equivalence_result(self, result: MarketEquivalenceResult) -> None:
        """Persist equivalence validation event."""
        def _insert():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO market_matches
                    (status, is_equivalent, polymarket_id, kalshi_id, reason, details_json, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.status,
                        1 if result.is_equivalent else 0,
                        result.polymarket_id,
                        result.kalshi_id,
                        result.reason,
                        json.dumps(result.details),
                        result.checked_at.isoformat(),
                    )
                )
                conn.commit()

        await asyncio.to_thread(_insert)

    async def log_trade(self, trade: ArbitrageTradeRecord) -> None:
        """Persist executed trade record and both order legs."""
        def _insert():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO arbitrage_trades
                    (trade_id, opportunity_id, mode, market_window, direction, is_hedged,
                     recovery_action, total_cost, payout_expected, fees_paid, realized_pnl,
                     latencies_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade.trade_id,
                        trade.opportunity_id,
                        trade.mode,
                        trade.market_window,
                        trade.direction,
                        1 if trade.is_hedged else 0,
                        trade.recovery_action,
                        str(trade.total_cost),
                        str(trade.payout_expected),
                        str(trade.fees_paid),
                        str(trade.realized_pnl),
                        json.dumps(trade.latencies_ns),
                        trade.created_at.isoformat(),
                    )
                )

                # Insert Leg YES
                cursor.execute(
                    """
                    INSERT INTO trade_legs
                    (trade_id, venue, order_id, client_order_id, side, status, price, fill_price,
                     quantity, fill_quantity, fee_paid, submitted_at_ns, ack_at_ns, filled_at_ns, raw_response_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade.trade_id,
                        trade.leg_yes.venue.value,
                        trade.leg_yes.order_id,
                        trade.leg_yes.client_order_id,
                        "YES",
                        trade.leg_yes.status.value,
                        str(trade.leg_yes.price),
                        str(trade.leg_yes.fill_price) if trade.leg_yes.fill_price else None,
                        trade.leg_yes.quantity,
                        trade.leg_yes.fill_quantity,
                        str(trade.leg_yes.fee_paid),
                        trade.leg_yes.submitted_at_ns,
                        trade.leg_yes.ack_at_ns,
                        trade.leg_yes.filled_at_ns,
                        json.dumps(trade.leg_yes.raw_response),
                    )
                )

                # Insert Leg NO
                cursor.execute(
                    """
                    INSERT INTO trade_legs
                    (trade_id, venue, order_id, client_order_id, side, status, price, fill_price,
                     quantity, fill_quantity, fee_paid, submitted_at_ns, ack_at_ns, filled_at_ns, raw_response_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade.trade_id,
                        trade.leg_no.venue.value,
                        trade.leg_no.order_id,
                        trade.leg_no.client_order_id,
                        "NO",
                        trade.leg_no.status.value,
                        str(trade.leg_no.price),
                        str(trade.leg_no.fill_price) if trade.leg_no.fill_price else None,
                        trade.leg_no.quantity,
                        trade.leg_no.fill_quantity,
                        str(trade.leg_no.fee_paid),
                        trade.leg_no.submitted_at_ns,
                        trade.leg_no.ack_at_ns,
                        trade.leg_no.filled_at_ns,
                        json.dumps(trade.leg_no.raw_response),
                    )
                )
                conn.commit()

        await asyncio.to_thread(_insert)

    async def log_rejected_opportunity(self, opp: ArbitrageOpportunity) -> None:
        """Persist rejected opportunity for strategy tuning and debugging."""
        def _insert():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO rejected_opportunities
                    (opportunity_id, market_window, direction, raw_combined_cost,
                     theoretical_gross_edge, conservative_net_edge, rejection_reason, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        opp.opportunity_id,
                        opp.market_window,
                        opp.direction,
                        str(opp.raw_combined_cost),
                        str(opp.theoretical_gross_edge),
                        str(opp.conservative_net_edge),
                        opp.rejection_reason or "Unknown rejection reason",
                        opp.detected_at.isoformat(),
                    )
                )
                conn.commit()

        await asyncio.to_thread(_insert)

    async def log_system_event(self, event_type: str, description: str, details: dict | None = None) -> None:
        """Log state transitions, kill switch events, etc."""
        def _insert():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO system_events (event_type, description, details_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        description,
                        json.dumps(details or {}),
                        datetime.now(timezone.utc).isoformat(),
                    )
                )
                conn.commit()

        await asyncio.to_thread(_insert)

    async def get_recent_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve recent trade history."""
        def _query():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT trade_id, opportunity_id, mode, market_window, direction,
                           is_hedged, recovery_action, total_cost, payout_expected,
                           fees_paid, realized_pnl, created_at
                    FROM arbitrage_trades
                    ORDER BY id DESC LIMIT ?
                    """,
                    (limit,)
                )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_query)

    async def get_recent_rejections(self, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve recent rejected opportunities."""
        def _query():
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT opportunity_id, market_window, direction, raw_combined_cost,
                           theoretical_gross_edge, conservative_net_edge, rejection_reason, detected_at
                    FROM rejected_opportunities
                    ORDER BY id DESC LIMIT ?
                    """,
                    (limit,)
                )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]

        return await asyncio.to_thread(_query)
