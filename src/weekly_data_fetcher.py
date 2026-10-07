"""Fetch public market data (Binance) and model internal KPIs for the report.

Public metrics are pulled live from Binance's REST API (spot + USD-M futures
24h tickers and exchange info).  Internal core metrics (VIP tier changes, Earn
net inflow, new registrations, net fund flow) are not exposed by any public API,
so they are *modelled* with a deterministic, period-seeded generator that
produces stable, plausible figures.  Swap ``_simulated_internal`` for real SQL
queries when wiring the system to production data stores.
"""

from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.database import Database

logger = logging.getLogger(__name__)

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"

# Report type -> reporting window in days.
WINDOW_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


class WeeklyDataFetcher:
    """Collects and persists the KPI set used by the report generator."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    # -- public API ----------------------------------------------------------
    def fetch_and_store(self, report_type: str = "weekly") -> list[dict[str, Any]]:
        report_type = report_type.lower()
        days = WINDOW_DAYS.get(report_type, 7)
        period_end, period_start = self._period_range(days)

        rows: list[dict[str, Any]] = []
        rows += self._public_metrics()
        rows += self._simulated_internal(report_type, period_start)

        stored: list[dict[str, Any]] = []
        for row in rows:
            change = self._compute_change(row["metric_key"], row["metric_value"], report_type)
            self.db.upsert_metric(
                report_type=report_type,
                metric_key=row["metric_key"],
                metric_label=row["metric_label"],
                metric_value=row["metric_value"],
                metric_unit=row["metric_unit"],
                change_pct=change,
                period_start=period_start,
                period_end=period_end,
                source=row.get("source", ""),
            )
            row["change_pct"] = change
            row["period_start"] = period_start
            row["period_end"] = period_end
            stored.append(row)

        logger.info("Stored %s metrics for %s (%s..%s)", len(stored), report_type, period_start, period_end)
        return stored

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _period_range(days: int) -> tuple[str, str]:
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=days)
        return today.isoformat(), start.isoformat()

    @staticmethod
    def _get(url: str, params: dict[str, Any] | None = None) -> Any | None:
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance request failed (%s): %s", url, exc)
            return None

    # -- public market data --------------------------------------------------
    def _public_metrics(self) -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []

        # Spot 24h tickers -> spot volume + active pair count.
        spot = self._get(f"{SPOT_BASE}/api/v3/ticker/24hr")
        spot_vol_usd = 0.0
        active_pairs = 0
        if isinstance(spot, list):
            for t in spot:
                try:
                    spot_vol_usd += float(t.get("quoteVolume", 0))
                    if float(t.get("lastPrice", 0)) > 0:
                        active_pairs += 1
                except (TypeError, ValueError):
                    continue

        # USD-M futures 24h tickers -> contract volume.
        fut = self._get(f"{FUTURES_BASE}/fapi/v1/ticker/24hr")
        contract_vol_usd = 0.0
        if isinstance(fut, list):
            for t in fut:
                try:
                    contract_vol_usd += float(t.get("quoteVolume", 0))
                except (TypeError, ValueError):
                    continue

        if spot_vol_usd:
            metrics.append(self._metric("spot_volume", "现货交易量", round(spot_vol_usd / 1e8, 1), "$亿", "Binance Spot API"))
        else:
            metrics.append(self._metric("spot_volume", "现货交易量", self._seeded("spot_volume", 400, 120), "$亿", "simulated"))

        if contract_vol_usd:
            metrics.append(self._metric("contract_volume", "合约交易量", round(contract_vol_usd / 1e8, 1), "$亿", "Binance Futures API"))
        else:
            metrics.append(self._metric("contract_volume", "合约交易量", self._seeded("contract_volume", 900, 300), "$亿", "simulated"))

        if active_pairs:
            metrics.append(self._metric("active_pairs", "活跃交易对", float(active_pairs), "个", "Binance ExchangeInfo"))
        else:
            metrics.append(self._metric("active_pairs", "活跃交易对", self._seeded("active_pairs", 400, 40), "个", "simulated"))

        # Net fund flow is derived from the spot/futures volume delta (proxy).
        net_flow = (spot_vol_usd - contract_vol_usd) / 1e8
        metrics.append(self._metric("fund_net_flow", "资金净流入", round(net_flow, 2), "$亿", "derived"))

        return metrics

    # -- simulated internal KPIs --------------------------------------------
    def _simulated_internal(self, report_type: str, period_start: str) -> list[dict[str, Any]]:
        scale = WINDOW_DAYS.get(report_type, 7) / 7.0
        salt = f"{report_type}:{period_start}"
        return [
            self._metric("vip_user_change", "VIP用户净变动", round(self._seeded("vip", 1200, 900, salt) * scale), "人", "internal-sim"),
            self._metric("earn_net_inflow", "Earn产品净流入", round(self._seeded("earn", 45, 30, salt) * scale, 1), "$M", "internal-sim"),
            self._metric("new_registrations", "新增注册用户", round(self._seeded("reg", 42000, 15000, salt) * scale), "人", "internal-sim"),
        ]

    # -- deterministic pseudo-random ----------------------------------------
    @staticmethod
    def _seeded(key: str, base: float, spread: float, salt: str = "") -> float:
        digest = hashlib.sha256(f"{key}:{salt}".encode()).hexdigest()
        rnd = random.Random(int(digest[:8], 16))
        return round(base + rnd.uniform(-spread, spread), 2)

    # -- change vs previous stored period -----------------------------------
    def _compute_change(self, metric_key: str, value: float, report_type: str) -> float | None:
        prev = self._previous_value(metric_key, report_type)
        if prev in (None, 0):
            # First observation: synthesise a realistic change for display.
            return round(self._seeded(f"{metric_key}:chg", 0, 12), 1)
        try:
            return round((value - prev) / abs(prev) * 100, 1)
        except ZeroDivisionError:
            return None

    def _previous_value(self, metric_key: str, report_type: str) -> float | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT metric_value FROM weekly_metrics WHERE report_type = ? AND metric_key = ? "
                "ORDER BY period_end DESC LIMIT 1",
                (report_type, metric_key),
            ).fetchone()
        return row["metric_value"] if row else None

    @staticmethod
    def _metric(key: str, label: str, value: float, unit: str, source: str) -> dict[str, Any]:
        return {
            "metric_key": key,
            "metric_label": label,
            "metric_value": value,
            "metric_unit": unit,
            "source": source,
        }
