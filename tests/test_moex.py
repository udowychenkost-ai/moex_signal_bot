from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.moex import MoexClient


@pytest.mark.asyncio
async def test_one_minute_candles_use_native_interval_without_resampling() -> None:
    seen_interval = 0
    columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    rows = [
        [100, 100.5, 101, 99, 1000, 10, "2025-01-10 10:00:00", "2025-01-10 10:00:59"],
        [100.5, 101, 102, 100, 1500, 15, "2025-01-10 10:01:00", "2025-01-10 10:01:59"],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_interval
        seen_interval = int(request.url.params["interval"])
        return httpx.Response(
            200,
            json={
                "candles": {"columns": columns, "data": rows},
                "candles.cursor": {
                    "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                    "data": [[0, 2, 100]],
                },
            },
        )

    async with MoexClient(
        "https://iss.moex.test/iss", transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch_candles("SBER", "1m", datetime(2025, 1, 10, tzinfo=UTC))

    assert seen_interval == 1
    assert len(result) == 2
    assert [item.close for item in result] == [100.5, 101]


@pytest.mark.asyncio
async def test_minute_candles_are_resampled_to_fifteen_minutes() -> None:
    columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    rows = []
    for minute in range(15):
        rows.append(
            [
                100 + minute,
                100.5 + minute,
                101 + minute,
                99 + minute,
                1000,
                10,
                f"2025-01-10 10:{minute:02d}:00",
                f"2025-01-10 10:{minute:02d}:59",
            ]
        )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candles": {"columns": columns, "data": rows},
                "candles.cursor": {
                    "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                    "data": [[0, 15, 100]],
                },
            },
        )

    async with MoexClient(
        "https://iss.moex.test/iss", transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch_candles("SBER", "15m", datetime(2025, 1, 10, tzinfo=UTC))
    assert len(result) == 1
    assert result[0].open == 100
    assert result[0].close == 114.5
    assert result[0].high == 115
    assert result[0].low == 99
    assert result[0].volume == 150


@pytest.mark.asyncio
async def test_hourly_candles_are_resampled_to_four_hours() -> None:
    columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    rows = [
        [100 + hour, 101 + hour, 102 + hour, 99 + hour, 1000, 10, begin, end]
        for hour, begin, end in [
            (0, "2025-01-10 10:00:00", "2025-01-10 10:59:59"),
            (1, "2025-01-10 11:00:00", "2025-01-10 11:59:59"),
            (2, "2025-01-10 12:00:00", "2025-01-10 12:59:59"),
            (3, "2025-01-10 13:00:00", "2025-01-10 13:59:59"),
        ]
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candles": {"columns": columns, "data": rows},
                "candles.cursor": {
                    "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                    "data": [[0, 4, 100]],
                },
            },
        )

    async with MoexClient(
        "https://iss.moex.test/iss", transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch_candles("SBER", "4h", datetime(2025, 1, 10, tzinfo=UTC))
    assert len(result) == 2
    assert result[0].open == 100
    assert result[0].close == 102
    assert result[0].volume == 20


@pytest.mark.asyncio
async def test_multi_timeframe_fetch_reuses_one_native_interval() -> None:
    columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        interval = int(request.url.params["interval"])
        start = int(request.url.params.get("start", 0))
        calls.append((interval, start))
        rows = (
            [
                [100 + hour, 101 + hour, 102 + hour, 99 + hour, 1000, 10, begin, end]
                for hour, begin, end in [
                    (0, "2025-01-10 10:00:00", "2025-01-10 10:59:59"),
                    (1, "2025-01-10 11:00:00", "2025-01-10 11:59:59"),
                    (2, "2025-01-10 12:00:00", "2025-01-10 12:59:59"),
                    (3, "2025-01-10 13:00:00", "2025-01-10 13:59:59"),
                ]
            ]
            if start == 0
            else []
        )
        return httpx.Response(200, json={"candles": {"columns": columns, "data": rows}})

    async with MoexClient(
        "https://iss.moex.test/iss", transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch_candles_multi(
            "SBER",
            ["1h", "4h"],
            datetime(2025, 1, 10, tzinfo=UTC),
        )

    assert set(result) == {"1h", "4h"}
    assert len(result["1h"]) == 4
    assert {interval for interval, _ in calls} == {60}
    assert calls == [(60, 0), (60, 4)]


@pytest.mark.asyncio
async def test_no_cursor_pagination_uses_offset_and_stops_on_empty_page() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", 0))
        calls.append(start)
        page = [[start + index] for index in range(2)] if start < 4 else []
        return httpx.Response(200, json={"items": {"columns": ["id"], "data": page}})

    async with MoexClient(
        "https://iss.moex.test/iss", transport=httpx.MockTransport(handler)
    ) as client:
        pages = [
            rows async for _, rows in client._pages("/items.json", "items", {"iss.only": "items"})
        ]
    assert calls == [0, 2, 4]
    assert [row["id"] for page in pages for row in page] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_no_cursor_pagination_stops_when_endpoint_ignores_offset() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"items": {"columns": ["id"], "data": [[1], [2]]}})

    async with MoexClient(
        "https://iss.moex.test/iss", transport=httpx.MockTransport(handler)
    ) as client:
        pages = [
            rows async for _, rows in client._pages("/items.json", "items", {"iss.only": "items"})
        ]
    assert calls == 2
    assert len(pages) == 1


@pytest.mark.asyncio
async def test_market_index_candles_use_index_endpoint() -> None:
    seen_path = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_path
        seen_path = request.url.path
        return httpx.Response(
            200,
            json={
                "candles": {
                    "columns": [
                        "open",
                        "close",
                        "high",
                        "low",
                        "value",
                        "volume",
                        "begin",
                        "end",
                    ],
                    "data": [
                        [
                            2800,
                            2820,
                            2830,
                            2790,
                            1_000_000,
                            100,
                            "2025-01-10 00:00:00",
                            "2025-01-10 23:59:59",
                        ]
                    ],
                },
                "candles.cursor": {
                    "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                    "data": [[0, 1, 100]],
                },
            },
        )

    async with MoexClient(
        "https://iss.moex.test/iss", transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch_market_candles(
            "IMOEX",
            "1d",
            datetime(2025, 1, 1, tzinfo=UTC),
        )

    assert "/markets/index/securities/IMOEX/candles.json" in seen_path
    assert result[0].symbol == "IMOEX"
    assert result[0].close == 2820


@pytest.mark.asyncio
async def test_level1_batch_uses_official_marketdata_contract_and_lot_depth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "marketdata": {
                    "columns": [
                        "BOARDID",
                        "SECID",
                        "BID",
                        "BIDDEPTH",
                        "OFFER",
                        "OFFERDEPTH",
                        "UPDATETIME",
                        "SYSTIME",
                    ],
                    "data": [
                        ["TQBR", "SBER", 268.20, 125, 268.21, 80, "12:00:00", "x"],
                        ["TQBR", "GAZP", 84.00, None, 84.01, None, "12:00:00", "x"],
                    ],
                }
            },
        )

    async with MoexClient(
        "https://iss.moex.test/iss", transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch_top_of_book_batch(["SBER", "GAZP"])

    assert len(requests) == 1
    assert requests[0].url.path.endswith("/boards/TQBR/securities.json")
    assert requests[0].url.params["iss.only"] == "marketdata"
    assert requests[0].url.params["securities"] == "GAZP,SBER"
    assert {item.side for item in result["SBER"]} == {"B", "S"}
    assert {item.quantity for item in result["SBER"]} == {80, 125}
    assert all(item.quantity is None for item in result["GAZP"])
    assert len({item.snapshot_at for values in result.values() for item in values}) == 1


@pytest.mark.asyncio
async def test_level1_batch_ignores_malformed_prices_without_inventing_levels() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "marketdata": {
                    "columns": ["BOARDID", "SECID", "BID", "BIDDEPTH", "OFFER", "OFFERDEPTH"],
                    "data": [["TQBR", "SBER", "bad", 100, 100.01, "bad"]],
                }
            },
        )

    async with MoexClient(
        "https://iss.moex.test/iss", transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch_top_of_book_batch(["SBER"])

    assert len(result["SBER"]) == 1
    assert result["SBER"][0].side == "S"
    assert result["SBER"][0].quantity is None
