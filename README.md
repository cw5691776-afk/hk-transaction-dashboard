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

**The daily refresh runs on Chris's Mac, not on GitHub Actions.**

28Hse and Midland permanently block GitHub's shared runner IP ranges (Midland returns `HTTP 403`, 28Hse serves a bot-check page with zero transaction rows). Centaline still works from the runner, so a CI-only pipeline would publish a single-source dataset — the exact failure the source guard was built to prevent. The runner's IP is simply not usable for two of the three providers.

Instead, a launchd agent runs the update on the Mac, where the residential IP is not blocked:

```text
~/Library/LaunchAgents/hk-transaction-dashboard.daily-refresh.plist
  └─ scripts/daily_refresh.sh   (daily 07:10 local time)
       ├─ git fetch / rebase origin/main
       ├─ python3 scripts/update_data.py      (3 attempts, 30s apart)
       ├─ python3 scripts/check_sources.py    (gate: all three providers must return records)
       ├─ git commit + push
       └─ push triggers .github/workflows/deploy.yml → GitHub Pages
```

`scripts/check_sources.py` is the gate: if any provider returns zero records, the script restores the previous dataset and pushes nothing, so a blocked run can never blank or degrade the site.

The PAT lives outside the repo at `~/.config/hk-transaction-dashboard/pat` (mode 600) and is passed to git via a temporary `http.extraheader`, never written into the remote URL or git config.

To check it ran:

```bash
tail -20 ~/Library/Logs/hk-transaction-dashboard/update.log
launchctl list | grep hk-transaction-dashboard
```

`.github/workflows/deploy.yml` rebuilds and deploys GitHub Pages on every push to `main`.

> **Requirement:** the Mac must be awake and online at 07:10 for the scheduled run. If it is asleep, launchd runs the job when the machine next wakes.

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
