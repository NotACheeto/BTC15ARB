"""High-precision latency telemetry and dynamic edge risk haircut tracker.

Measures all critical execution checkpoints using nanosecond monotonic clock.
Maintains rolling percentiles (p50, p90, p95, p99, max) without blocking hot path.
"""

from collections import deque
from decimal import Decimal
import time
from typing import Literal


class RollingLatencyTracker:
    """Tracks latencies for a specific metric over a rolling window."""

    def __init__(self, maxlen: int = 1000):
        self._samples: deque[float] = deque(maxlen=maxlen)

    def record_ms(self, ms: float) -> None:
        """Record sample in milliseconds."""
        self._samples.append(ms)

    def record_ns(self, ns: int) -> None:
        """Record sample in nanoseconds."""
        self._samples.append(ns / 1_000_000.0)

    def get_percentile(self, p: float) -> float:
        """Calculate percentile p (0 to 100). Returns 0.0 if empty."""
        if not self._samples:
            return 0.0
        sorted_samples = sorted(self._samples)
        k = (len(sorted_samples) - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, len(sorted_samples) - 1)
        d = k - f
        return sorted_samples[f] * (1 - d) + sorted_samples[c] * d

    @property
    def p50(self) -> float:
        return self.get_percentile(50.0)

    @property
    def p90(self) -> float:
        return self.get_percentile(90.0)

    @property
    def p95(self) -> float:
        return self.get_percentile(95.0)

    @property
    def p99(self) -> float:
        return self.get_percentile(99.0)

    @property
    def max(self) -> float:
        return max(self._samples) if self._samples else 0.0

    @property
    def count(self) -> int:
        return len(self._samples)

    @property
    def last(self) -> float:
        return self._samples[-1] if self._samples else 0.0


class LatencyManager:
    """Global latency management service tracking venues and execution stages."""

    def __init__(self):
        # Venue-specific network metrics
        self.polymarket_ws_latency = RollingLatencyTracker()
        self.kalshi_ws_latency = RollingLatencyTracker()
        self.polymarket_rest_rtt = RollingLatencyTracker()
        self.kalshi_rest_rtt = RollingLatencyTracker()

        # Critical path pipeline stages
        self.market_data_to_decision = RollingLatencyTracker()  # quote arrival -> arb decision
        self.decision_to_submission = RollingLatencyTracker()   # decision -> wire transmission
        self.order_submission_time = RollingLatencyTracker()    # concurrent leg submission time
        self.submit_to_ack = RollingLatencyTracker()            # order submit -> first ack
        self.ack_delta = RollingLatencyTracker()                # leg 1 ack -> leg 2 ack delta
        self.ack_to_fill = RollingLatencyTracker()              # ack -> fill confirmation
        self.fill_delta = RollingLatencyTracker()               # leg 1 fill -> leg 2 fill delta
        self.cancellation_latency = RollingLatencyTracker()

    def calculate_latency_risk_buffer(
        self,
        mode: Literal["p50", "p90", "p95", "p99", "max"] = "p95"
    ) -> Decimal:
        """Calculate dynamic latency risk haircut in dollars based on measured network performance.
        
        If round-trip / execution latency spikes, this buffer automatically widens to
        prevent executing into stale order books.
        Baseline: 1 cent per 100ms of end-to-end latency exceeding a 50ms baseline.
        Minimum buffer: $0.005.
        """
        metric_getter = getattr(self.order_submission_time, mode, None)
        obs_latency_ms = metric_getter if isinstance(metric_getter, (int, float)) else 50.0

        # Also consider max REST RTT between venues
        max_rtt = max(self.polymarket_rest_rtt.p95, self.kalshi_rest_rtt.p95, 40.0)
        total_pipeline_ms = obs_latency_ms + max_rtt

        # Formula: $0.01 per 100ms of latency over 50ms baseline
        excess_ms = max(0.0, total_pipeline_ms - 50.0)
        haircut = Decimal(str(round(0.01 * (excess_ms / 100.0), 4)))

        # Enforce minimum conservative buffer of 0.5 cents ($0.005)
        return max(Decimal("0.005"), haircut)

    def get_summary(self) -> dict:
        """Return comprehensive latency snapshot for telemetry and dashboard."""
        return {
            "polymarket_rest_rtt": {
                "p50": round(self.polymarket_rest_rtt.p50, 2),
                "p90": round(self.polymarket_rest_rtt.p90, 2),
                "p95": round(self.polymarket_rest_rtt.p95, 2),
                "p99": round(self.polymarket_rest_rtt.p99, 2),
                "max": round(self.polymarket_rest_rtt.max, 2),
                "last": round(self.polymarket_rest_rtt.last, 2),
            },
            "kalshi_rest_rtt": {
                "p50": round(self.kalshi_rest_rtt.p50, 2),
                "p90": round(self.kalshi_rest_rtt.p90, 2),
                "p95": round(self.kalshi_rest_rtt.p95, 2),
                "p99": round(self.kalshi_rest_rtt.p99, 2),
                "max": round(self.kalshi_rest_rtt.max, 2),
                "last": round(self.kalshi_rest_rtt.last, 2),
            },
            "pipeline": {
                "data_to_decision_p95_ms": round(self.market_data_to_decision.p95, 2),
                "decision_to_submit_p95_ms": round(self.decision_to_submission.p95, 2),
                "submit_to_ack_p95_ms": round(self.submit_to_ack.p95, 2),
                "ack_delta_p95_ms": round(self.ack_delta.p95, 2),
                "ack_to_fill_p95_ms": round(self.ack_to_fill.p95, 2),
            },
            "current_risk_buffer": float(self.calculate_latency_risk_buffer()),
        }


# Global latency manager instance
latency_tracker = LatencyManager()
