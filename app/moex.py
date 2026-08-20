from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pandas as pd

from app.domain import (
    CandleData,
    InstrumentData,
    MarketCandleData,
    MoexApiError,
    OrderBookLevelData,
)

logger = logging.getLogger(__name__)

SOURCE_INTERVALS = {
    "5m": 1,
    "15m": 1,
    "1h": 60,
    "4h": 60,
    "1d": 24,
    "1w": 7,
}
RESAMPLE_RULES = {"5m": "5min", "15m": "15min", "4h": "4h"}


def _rows(payload: dict[str, Any], block: str) -> list[dict[str, Any]]:
    value = payload.get(block)
    if not isinstance(value, dict):
        raise MoexApiError(f"MOEX response has no '{block}' block")
    columns = value.get("columns", [])
    data = value.get("data", [])
    return [dict(zip(columns, row, strict=False)) for row in data]


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _parse_moex_datetime(value: str) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("Europe/Moscow")
    return timestamp.tz_convert("UTC").to_pydatetime()


class MoexClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        api_token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"User-Agent": "moex-signal-bot/0.3"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            headers=headers,
        )
        self._max_retries = max_retries

    async def __aenter__(self) -> MoexClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.get(path, params=params)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    access_marker = response.headers.get("x-micexpassport-marker", "unknown")
                    raise MoexApiError(
                        f"MOEX returned {content_type or 'unknown content'} "
                        f"(access marker: {access_marker})"
                    )
                payload = response.json()
                if not isinstance(payload, dict):
                    raise MoexApiError("MOEX returned a non-object JSON response")
                return payload
            except (httpx.HTTPError, ValueError, MoexApiError) as error:
                last_error = error
                if attempt == self._max_retries:
                    break
                delay = 0.4 * (2 ** (attempt - 1))
                logger.warning("MOEX request failed (attempt %s): %s", attempt, error)
                await asyncio.sleep(delay)
        raise MoexApiError(f"MOEX request failed after retries: {last_error}") from last_error

    async def _pages(
        self,
        path: str,
        block: str,
        params: dict[str, Any],
    ) -> AsyncIterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
        start = 0
        seen_first_rows: set[str] = set()
        while True:
            page_params = {**params, "start": start, "iss.meta": "off"}
            payload = await self._get(path, page_params)
            rows = _rows(payload, block)
            if not rows:
                return
            # Some public ISS methods omit the cursor block. Offsets still work, while
            # a few unpaginated methods ignore `start`. Detecting a repeated first row
            # supports both behaviours without duplicating rows or looping forever.
            first_row_signature = repr(rows[0])
            if first_row_signature in seen_first_rows:
                return
            seen_first_rows.add(first_row_signature)
            yield payload, rows

            cursor_rows = []
            cursor = payload.get(f"{block}.cursor")
            if isinstance(cursor, dict):
                cursor_rows = [
                    dict(zip(cursor.get("columns", []), row, strict=False))
                    for row in cursor.get("data", [])
                ]
            if cursor_rows:
                index = int(cursor_rows[0].get("INDEX", start))
                total = int(cursor_rows[0].get("TOTAL", len(rows)))
                page_size = int(cursor_rows[0].get("PAGESIZE", len(rows)))
                start = index + page_size
                if start >= total:
                    return
            else:
                start += len(rows)

    async def fetch_instruments(self, board_id: str = "TQBR") -> list[InstrumentData]:
        path = f"/engines/stock/markets/shares/boards/{board_id}/securities.json"
        params = {
            "iss.only": "securities,marketdata,securities.cursor",
            "securities.columns": (
                "SECID,BOARDID,SHORTNAME,SECNAME,ISIN,LOTSIZE,STATUS,ISSUECAPITALIZATION"
            ),
            "marketdata.columns": "SECID,LAST,VALTODAY,ISSUECAPITALIZATION",
        }
        instruments: list[InstrumentData] = []
        async for payload, security_rows in self._pages(path, "securities", params):
            market_rows = {row["SECID"]: row for row in _rows(payload, "marketdata")}
            for row in security_rows:
                if row.get("STATUS") not in (None, "A"):
                    continue
                market = market_rows.get(row["SECID"], {})
                instruments.append(
                    InstrumentData(
                        secid=str(row["SECID"]).upper(),
                        board_id=str(row.get("BOARDID") or board_id).upper(),
                        short_name=str(row.get("SHORTNAME") or row["SECID"]),
                        full_name=row.get("SECNAME"),
                        isin=row.get("ISIN"),
                        lot_size=int(row["LOTSIZE"]) if row.get("LOTSIZE") else None,
                        last_price=_optional_float(market.get("LAST")),
                        market_cap=_optional_float(
                            market.get("ISSUECAPITALIZATION") or row.get("ISSUECAPITALIZATION")
                        ),
                        daily_turnover=_optional_float(market.get("VALTODAY")),
                    )
                )
        return instruments

    async def fetch_candles(
        self,
        secid: str,
        timeframe: str,
        date_from: datetime,
        *,
        board_id: str = "TQBR",
        date_to: datetime | None = None,
    ) -> list[CandleData]:
        result = await self.fetch_candles_multi(
            secid,
            [timeframe],
            date_from,
            board_id=board_id,
            date_to=date_to,
        )
        return result[timeframe]

    async def fetch_candles_multi(
        self,
        secid: str,
        timeframes: list[str],
        date_from: datetime,
        *,
        board_id: str = "TQBR",
        date_to: datetime | None = None,
    ) -> dict[str, list[CandleData]]:
        """Fetch each native MOEX interval once and derive requested timeframes."""
        invalid = set(timeframes) - set(SOURCE_INTERVALS)
        if invalid:
            raise ValueError(f"Unsupported timeframes: {', '.join(sorted(invalid))}")
        grouped: dict[int, list[str]] = defaultdict(list)
        for timeframe in dict.fromkeys(timeframes):
            grouped[SOURCE_INTERVALS[timeframe]].append(timeframe)
        result: dict[str, list[CandleData]] = {}
        for source_interval, grouped_timeframes in grouped.items():
            raw_rows = await self._fetch_candle_rows(
                secid,
                source_interval,
                date_from,
                board_id=board_id,
                date_to=date_to,
            )
            for timeframe in grouped_timeframes:
                rows = raw_rows
                if timeframe in RESAMPLE_RULES:
                    rows = self._resample(raw_rows, RESAMPLE_RULES[timeframe])
                result[timeframe] = self._candle_data(
                    secid,
                    board_id,
                    timeframe,
                    rows,
                )
        return result

    async def _fetch_candle_rows(
        self,
        secid: str,
        source_interval: int,
        date_from: datetime,
        *,
        board_id: str,
        date_to: datetime | None,
    ) -> list[dict[str, Any]]:
        path = (
            f"/engines/stock/markets/shares/boards/{board_id}/"
            f"securities/{secid.upper()}/candles.json"
        )
        params = {
            "iss.only": "candles,candles.cursor",
            "candles.columns": "open,close,high,low,value,volume,begin,end",
            "interval": source_interval,
            "from": date_from.astimezone(UTC).date().isoformat(),
            "till": (date_to or datetime.now(UTC)).astimezone(UTC).date().isoformat(),
        }
        raw_rows: list[dict[str, Any]] = []
        async for _, rows in self._pages(path, "candles", params):
            raw_rows.extend(rows)
        return raw_rows

    async def fetch_market_candles(
        self,
        symbol: str,
        timeframe: str,
        date_from: datetime,
        *,
        date_to: datetime | None = None,
    ) -> list[MarketCandleData]:
        if timeframe not in SOURCE_INTERVALS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        source_interval = SOURCE_INTERVALS[timeframe]
        path = f"/engines/stock/markets/index/securities/{symbol.upper()}/candles.json"
        params = {
            "iss.only": "candles,candles.cursor",
            "candles.columns": "open,close,high,low,value,volume,begin,end",
            "interval": source_interval,
            "from": date_from.astimezone(UTC).date().isoformat(),
            "till": (date_to or datetime.now(UTC)).astimezone(UTC).date().isoformat(),
        }
        raw_rows: list[dict[str, Any]] = []
        async for _, rows in self._pages(path, "candles", params):
            raw_rows.extend(rows)
        if timeframe in RESAMPLE_RULES:
            raw_rows = self._resample(raw_rows, RESAMPLE_RULES[timeframe])
        result: list[MarketCandleData] = []
        for row in raw_rows:
            required = ("open", "high", "low", "close", "begin", "end")
            if any(row.get(key) is None for key in required):
                continue
            result.append(
                MarketCandleData(
                    symbol=symbol.upper(),
                    timeframe=timeframe,
                    begin=_parse_moex_datetime(str(row["begin"])),
                    end=_parse_moex_datetime(str(row["end"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                    value=float(row.get("value") or 0),
                )
            )
        return result

    @staticmethod
    def _candle_data(
        secid: str,
        board_id: str,
        timeframe: str,
        raw_rows: list[dict[str, Any]],
    ) -> list[CandleData]:
        result: list[CandleData] = []
        for row in raw_rows:
            required = ("open", "high", "low", "close", "begin", "end")
            if any(row.get(key) is None for key in required):
                continue
            result.append(
                CandleData(
                    secid=secid.upper(),
                    board_id=board_id.upper(),
                    timeframe=timeframe,
                    begin=_parse_moex_datetime(str(row["begin"])),
                    end=_parse_moex_datetime(str(row["end"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                    value=float(row.get("value") or 0),
                )
            )
        return result

    @staticmethod
    def _resample(rows: list[dict[str, Any]], rule: str) -> list[dict[str, Any]]:
        if not rows:
            return []
        frame = pd.DataFrame(rows)
        frame["begin"] = pd.to_datetime(frame["begin"])
        frame["end"] = pd.to_datetime(frame["end"])
        frame = frame.set_index("begin").sort_index()
        aggregated = frame.resample(rule, origin="start_day").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "value": "sum",
                "end": "max",
            }
        )
        aggregated = aggregated.dropna(subset=["open", "high", "low", "close"])
        aggregated["begin"] = aggregated.index
        return aggregated.reset_index(drop=True).to_dict(orient="records")

    async def fetch_orderbook(
        self,
        secid: str,
        *,
        board_id: str = "TQBR",
        depth: int = 20,
    ) -> list[OrderBookLevelData]:
        path = (
            f"/engines/stock/markets/shares/boards/{board_id}/"
            f"securities/{secid.upper()}/orderbook.json"
        )
        payload = await self._get(
            path,
            {
                "iss.only": "orderbook",
                "orderbook.columns": "BOARDID,SECID,BUYSELL,PRICE,QUANTITY",
                "iss.meta": "off",
            },
        )
        rows = _rows(payload, "orderbook")
        snapshot_at = datetime.now(UTC)
        result: list[OrderBookLevelData] = []
        for side in ("B", "S"):
            side_rows = [row for row in rows if row.get("BUYSELL") == side and row.get("PRICE")]
            side_rows.sort(key=lambda row: float(row["PRICE"]), reverse=side == "B")
            for level, row in enumerate(side_rows[:depth], start=1):
                result.append(
                    OrderBookLevelData(
                        secid=secid.upper(),
                        board_id=board_id.upper(),
                        snapshot_at=snapshot_at,
                        side=side,
                        level=level,
                        price=float(row["PRICE"]),
                        quantity=float(row.get("QUANTITY") or 0),
                    )
                )
        return result
