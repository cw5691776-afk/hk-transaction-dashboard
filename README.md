# Hong Kong Estate Transaction Dashboard

A zero-token, self-updating dashboard for recent residential sale transactions in six Hong Kong estates:

- 得寶花園 (Tak Bo Garden, 九龍灣)
- 康華苑 (Hong Wah Court, 藍田)
- 新成中心 (Sun Sing Centre, 西灣河)
- The Holborn (西灣河)
- 都會軒 (Metro Town, 紅磡)
- 新都城 (Metro City, 將軍澳)

## Data sources

| Provider | Access | Status |
|---|---|---|
| 中原 Centadata | Public JSON transaction endpoint | Enabled |
| 28Hse | Public estate transaction page | Enabled |
| 美聯 Midland | Public `data.midland.com.hk` search API (public build token read from their own bundle) | Enabled |
| House730 | Cloudflare-protected | Adapter slot reserved; disabled in the free pipeline |

Land Registry IRIS is authoritative but needs an account and paid searches, so it is not part of the free daily job.

## How it works

```text
scripts/update_data.py  →  data/transactions.json  →  index.html
```

The updater is plain Python 3.9+ with only the standard library. It:

1. Fetches each provider independently (one failure never blocks the others).
2. Normalises every deal to one canonical record: estate, block, floor, unit, date, price HKD, saleable area, saleable HKD/sq ft, transaction kind, provider, provider id, source URL.
3. Keeps **registry deals** (`土地註冊處`) separate from **market deals** (`市場成交`).
4. Merges overlapping republications of the same Land Registry deal while preserving every source label in `sources`.
5. Writes the dataset atomically only when at least one source returned data, so a bad run never blanks the site.
6. Never calls an LLM. Daily runs cost no tokens.

## Run locally

```bash
python3 scripts/update_data.py                 # all providers
python3 scripts/update_data.py --providers centaline,hse28
python3 -m http.server 8765                    # then open http://127.0.0.1:8765
```

## Schedule

`.github/workflows/daily-update.yml` runs every day at 07:10 Hong Kong time (23:10 UTC), commits the refreshed dataset, and deploys GitHub Pages.

Enable **Settings → Pages → Source: GitHub Actions** once after the first deployment.

## Configuration

`config/estates.json` holds the canonical estate list plus each provider's identifiers:

- `centaline.type_codes` — Centaline building type codes
- `hse28.estate_id` — 28Hse estate id
- `midland.estate_id` — Midland estate id (`E…`)

To add an estate, add one object with the three provider ids.

## Data notes

- Several providers republish the same Land Registry filings, so raw row counts are not transaction counts. The dashboard merges them and shows all source labels.
- Dates can differ by a few days between providers (contract date vs filing date). Matches therefore rely on unit + price + area, not date alone.
- Market deals may appear before registration and are labelled separately.
- Data is for reference only and should be verified against the Land Registry for any transaction decision.
