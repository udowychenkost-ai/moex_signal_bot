# Official fundamental input

`official.json` is an optional reviewed import file. It is intentionally not
populated with synthetic ratios and must not be built from an opaque scraper.
Every fact set retains its official disclosure URL and the first timestamp at
which the report was usable by the strategy.

```json
{
  "reports": [
    {
      "ticker": "SBER",
      "sector": "Financials",
      "report_period": "2025-H1",
      "publication_date": "2025-08-28T10:00:00+03:00",
      "available_from": "2025-08-28T10:00:00+03:00",
      "source": "issuer_official",
      "source_url": "https://www.sberbank.com/investor-relations/report.pdf",
      "metrics": {
        "pe": 0.0,
        "pb": 0.0,
        "ev_ebitda": 0.0,
        "roe": 0.0,
        "roa": 0.0,
        "operating_margin": 0.0,
        "net_margin": 0.0,
        "debt_ebitda": 0.0,
        "net_debt_ebitda": 0.0,
        "revenue_yoy": 0.0,
        "earnings_yoy": 0.0,
        "eps_growth": 0.0,
        "fcf": 0.0,
        "fcf_growth": 0.0,
        "fcf_yield": 0.0,
        "dividend_yield": 0.0,
        "dividend_consistency": 0.0,
        "payout_ratio": 0.0
      }
    }
  ]
}
```

The numbers above describe the schema only and are not usable data. Copy this
shape to `official.json`, replace every value with a verified published fact and
use a real official URL. The importer rejects unknown metrics, non-finite
values, non-official URLs and `available_from` earlier than
`publication_date`. Backtests query only rows whose `available_from` is not
later than the decision timestamp.
