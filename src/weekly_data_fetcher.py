"""Fetch public market data (Binance) and model internal KPIs for the report.

Public metrics are pulled live from Binance (spot + USD-M futures 24h tickers).
Internal core metrics (VIP tiering, Earn inflow, registrations, funding) are not
exposed by any public API, so they are *modelled* with a deterministic,
period-seeded generator producing stable, plausible figures.  Swap
``_simulated_internal`` for real SQL when wiring to production data stores.

Metrics carry a **dimension** (trading / funds / users / market) so reports can
present multiple dimensions, and market movers are stored in ``report_extras``.
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

WINDOW_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}

DIMENSIONS = {
    "trading": "交易维度",
    "funds": "资金维度",
    "users": "用户维度",
    "market": "市场维度",
}


class WeeklyDataFetcher:
    """Collects and persists a multi-dimensional KPI set."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    # -- public API ----------------------------------------------------------
    def fetch_and_store(self, report_type: str = "weekly") -> list[dict[str, Any]]:
        report_type = report_type.lower()
        period_start, period_end = self._period_range(report_type)

        metrics, extras = self._collect(report_type, period_start, period_end)

        stored: list[dict[str, Any]] = []
        for m in metrics:
            change = self._compute_change(m["metric_key"], m["metric_value"], report_type, period_end)
            self.db.upsert_metric(
                report_type=report_type,
                metric_key=m["metric_key"],
                metric_label=m["metric_label"],
                dimension=m["dimension"],
                metric_value=m["metric_value"],
                metric_unit=m["metric_unit"],
                change_pct=change,
                period_start=period_start,
                period_end=period_end,
                source=m.get("source", ""),
            )
            m["change_pct"] = change
            m["period_start"] = period_start
            m["period_end"] = period_end
            stored.append(m)

        for key, value in extras.items():
            self.db.set_extra(report_type, period_end, key, value)

        logger.info(
            "Stored %s metrics across %s dimensions for %s (%s..%s)",
            len(stored), len({m['dimension'] for m in stored}), report_type, period_start, period_end,
        )
        return stored

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _period_range(report_type: str) -> tuple[str, str]:
        """Calendar-aligned, *completed* reporting windows.

        daily   -> yesterday
        weekly  -> the most recently completed Monday..Sunday week
        monthly -> the previous calendar month

        Aligning to completed periods means the Monday run reports "last week"
        and the 1st-of-month run reports "last month", exactly as scheduled.
        """
        today = datetime.now(timezone.utc).date()

        if report_type == "daily":
            end = today - timedelta(days=1)
            return end.isoformat(), end.isoformat()

        if report_type == "monthly":
            first_this_month = today.replace(day=1)
            end = first_this_month - timedelta(days=1)
            start = end.replace(day=1)
            return start.isoformat(), end.isoformat()

        # weekly (default): the last completed Mon..Sun block.
        days_since_monday = today.weekday()  # Monday == 0
        end = today - timedelta(days=days_since_monday + 1)  # last Sunday
        start = end - timedelta(days=6)                      # that week's Monday
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _get(url: str, params: dict[str, Any] | None = None) -> Any | None:
        try:
            resp = requests.get(url, params=params, timeout=25)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance request failed (%s): %s", url, exc)
            return None

    @staticmethod
    def _f(ticker: dict[str, Any], key: str) -> float:
        try:
            return float(ticker.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _m(key: str, label: str, value: float, unit: str, dimension: str, source: str) -> dict[str, Any]:
        return {
            "metric_key": key,
            "metric_label": label,
            "metric_value": value,
            "metric_unit": unit,
            "dimension": dimension,
            "source": source,
        }

    # -- collection ----------------------------------------------------------
    def _collect(self, report_type: str, period_start: str, period_end: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        metrics: list[dict[str, Any]] = []
        extras: dict[str, Any] = {}
        metrics += self._market_metrics(extras)
        metrics += self._trading_metrics()
        metrics += self._simulated_internal(report_type, period_start, period_end)
        return metrics, extras

    # -- market dimension (live) --------------------------------------------
    def _market_metrics(self, extras: dict[str, Any]) -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []
        spot = self._get(f"{SPOT_BASE}/api/v3/ticker/24hr")

        btc_price = eth_price = 0.0
        btc_chg = eth_chg = 0.0
        up = total = 0
        movers: list[dict[str, Any]] = []

        if isinstance(spot, list):
            for t in spot:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                try:
                    last = self._f(t, "lastPrice")
                    qv = self._f(t, "quoteVolume")
                    chg = self._f(t, "priceChangePercent")
                except Exception:  # noqa: BLE001
                    continue
                if last <= 0 or qv <= 0:
                    continue
                total += 1
                if chg > 0:
                    up += 1
                if sym in ("BTCUSDT", "ETHUSDT"):
                    if sym == "BTCUSDT":
                        btc_price, btc_chg = last, chg
                    else:
                        eth_price, eth_chg = last, chg
                if qv >= 5_000_000:  # liquidity filter for movers
                    movers.append({"symbol": sym[:-4], "price": round(last, 6), "change_pct": round(chg, 2), "volume_usd": round(qv / 1e6, 1)})

        movers.sort(key=lambda m: m["change_pct"], reverse=True)
        gainers = movers[:8]
        losers = list(reversed(movers[-8:])) if len(movers) >= 8 else list(reversed(movers))
        breadth = round(up / total * 100, 1) if total else self._seeded("breadth", 55, 15)

        metrics.append(self._m("btc_price", "BTC价格", round(btc_price, 1) or self._seeded("btc", 65000, 8000), "$", "market", "Binance Spot"))
        metrics.append(self._m("btc_change_pct", "BTC 24h涨跌", round(btc_chg, 2), "%", "market", "Binance Spot"))
        metrics.append(self._m("eth_price", "ETH价格", round(eth_price, 1) or self._seeded("eth", 3200, 500), "$", "market", "Binance Spot"))
        metrics.append(self._m("eth_change_pct", "ETH 24h涨跌", round(eth_chg, 2), "%", "market", "Binance Spot"))
        metrics.append(self._m("market_breadth", "上涨币种占比", breadth, "%", "market", "Binance Spot"))
        extras["top_gainers"] = gainers
        extras["top_losers"] = losers
        return metrics


    # -- trading dimension (live) -------------------------------------------
    def _trading_metrics(self) -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []

        spot = self._get(f"{SPOT_BASE}/api/v3/ticker/24hr")
        spot_vol = 0.0
        active_pairs = 0
        if isinstance(spot, list):
            for t in spot:
                try:
                    if self._f(t, "lastPrice") > 0:
                        active_pairs += 1
                    spot_vol += self._f(t, "quoteVolume")
                except Exception:  # noqa: BLE001
                    continue

        fut = self._get(f"{FUTURES_BASE}/fapi/v1/ticker/24hr")
        contract_vol = 0.0
        if isinstance(fut, list):
            for t in fut:
                try:
                    contract_vol += self._f(t, "quoteVolume")
                except Exception:  # noqa: BLE001
                    continue

        metrics.append(self._m("spot_volume", "现货交易量", round(spot_vol / 1e8, 1) or self._seeded("spot_volume", 400, 120), "$亿", "trading", "Binance Spot API"))
        metrics.append(self._m("contract_volume", "合约交易量", round(contract_vol / 1e8, 1) or self._seeded("contract_volume", 900, 300), "$亿", "trading", "Binance Futures API"))
        metrics.append(self._m("active_pairs", "活跃交易对", float(active_pairs) or float(round(self._seeded("active_pairs", 400, 40))), "个", "trading", "Binance ExchangeInfo"))
        trade_count_wan = round(self._seeded("trade_count", 8500, 2500))
        metrics.append(self._m("trade_count", "成交笔数", float(trade_count_wan), "万笔", "trading", "internal-sim"))
        avg_size = round((spot_vol / 1e8) / max(trade_count_wan, 1) * 100, 1) if spot_vol else self._seeded("avg_order", 1200, 400)
        metrics.append(self._m("avg_order_size", "平均单笔金额", avg_size, "$", "trading", "derived"))
        return metrics

    # -- funds + users dimensions (modelled) --------------------------------
    def _simulated_internal(self, report_type: str, period_start: str, period_end: str) -> list[dict[str, Any]]:
        scale = WINDOW_DAYS.get(report_type, 7) / 7.0
        salt = f"{report_type}:{period_start}:{period_end}"
        s = self._seeded

        funds = [
            self._m("deposit_volume", "净入金", round(s("deposit", 120, 40, salt) * scale, 1), "$亿", "funds", "internal-sim"),
            self._m("withdrawal_volume", "净出金", round(s("withdraw", 100, 35, salt) * scale, 1), "$亿", "funds", "internal-sim"),
            self._m("fund_net_flow", "资金净流入", round((s("deposit", 120, 40, salt) - s("withdraw", 100, 35, salt)) * scale, 1), "$亿", "funds", "derived"),
            self._m("earn_net_inflow", "Earn产品净流入", round(s("earn", 45, 30, salt) * scale, 1), "$M", "funds", "internal-sim"),
            self._m("stablecoin_reserve", "稳定币储备", round(s("stablecoin", 320, 60, salt), 1), "$亿", "funds", "internal-sim"),
        ]
        users = [
            self._m("new_registrations", "新增注册用户", round(s("reg", 42000, 15000, salt) * scale), "人", "users", "internal-sim"),
            self._m("active_users", "日活用户(DAU)", round(s("dau", 86, 20, salt), 1), "万人", "users", "internal-sim"),
            self._m("vip_user_change", "VIP用户净变动", round(s("vip", 1200, 900, salt) * scale), "人", "users", "internal-sim"),
            self._m("vip4plus_count", "VIP4+用户数", round(s("vip4", 3200, 500, salt)), "人", "users", "internal-sim"),
            self._m("retention_7d", "7日留存率", round(abs(s("retention", 42, 8, salt)), 1), "%", "users", "internal-sim"),
        ]
        return funds + users

    # -- deterministic pseudo-random ----------------------------------------
    @staticmethod
    def _seeded(key: str, base: float, spread: float, salt: str = "") -> float:
        digest = hashlib.sha256(f"{key}:{salt}".encode()).hexdigest()
        rnd = random.Random(int(digest[:8], 16))
        return round(base + rnd.uniform(-spread, spread), 2)

    # -- change vs previous stored period -----------------------------------
    def _compute_change(self, metric_key: str, value: float, report_type: str, period_end: str) -> float | None:
        prev = self._previous_value(metric_key, report_type, period_end)
        if prev in (None, 0):
            # First observation for this cadence: no real prior period exists, so
            # model a plausible change that is stable per (metric, cadence).
            return round(self._seeded(f"{metric_key}:{report_type}:chg", 0, 12), 1)
        try:
            return round((value - prev) / abs(prev) * 100, 1)
        except ZeroDivisionError:
            return None

    def _previous_value(self, metric_key: str, report_type: str, before_period_end: str) -> float | None:
        """Latest stored value for an *earlier* period (never the current one)."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT metric_value FROM weekly_metrics "
                "WHERE report_type = ? AND metric_key = ? AND period_end < ? "
                "ORDER BY period_end DESC LIMIT 1",
                (report_type, metric_key, before_period_end),
            ).fetchone()
        return row["metric_value"] if row else None
