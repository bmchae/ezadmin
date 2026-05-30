# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the web server (default port 9900, override with PORT env)
python src/app.py

# Install dependencies
pip install -r requirements.txt
```

## Architecture

ezadmin is a FastAPI (served by uvicorn) web dashboard that reads portfolio configurations from sibling projects (`~/ez/ezgain`, `~/ez/ezinvest`, `~/ez/ezsplit`, and ezadmin's own `config/`) and queries KIS (Korea Investment & Securities) Open API — plus Kiwoom and Upbit — to display account holdings and balances.

### Data Flow

1. **src/config_loader.py** scans the `config/` directory of each project (`ezgain`, `ezinvest`, `ezsplit`, `ezadmin`) and classifies every YAML by its *content shape* (`_classify`) rather than its filename — shapes are `ezsplit` (top-level kis/kw/upbit blocks), `portfolio-ref` (references an external account config via `account_config`), `bog` (ezgain env/broker/account/bog), and `kis-account`/`unknown` (skipped). Each shape has a `_build_*` normalizer producing portfolio dicts. Owner is detected (`_detect_owner`) by substring-matching the owner list in `config/owners.yaml` against filename + description + name + my_htsid. Files prefixed with `-`, symlinks, fully-commented files, and `*example*` are skipped. Duplicate accounts (same broker + account no.) are deduped with `ezsplit` taking priority.

2. **src/kis_client.py** calls KIS REST API directly (independent of ezgain/ezinvest's KIS modules which use global state). It reuses OAuth tokens from each project's token directory. Supports both domestic (`inquire-balance`) and overseas (`inquire-present-balance`) stock queries with pagination. Kiwoom (`src/kw_client.py`) and Upbit (`src/upbit_client.py`) have their own standalone clients.

3. **src/app.py** groups portfolios by owner, and on detail view calculates actual weight vs target weight (from portfolio config's `universe` section) for each holding.

### Key Design Decisions

- KIS API client is standalone (not importing from ezgain/ezinvest) because the original modules use Python global state (`_cfg`, `_TRENV`, `_base_headers`) that conflicts when querying multiple accounts sequentially.
- Token files are shared with ezgain/ezinvest projects — ezadmin reads their `token/` directories first and only issues new tokens when none are valid.
- `portfolio-ref` shape configs reference account configs by filename (e.g., `account_config: kis-bmchae.yaml`), resolved relative to the same `config/` directory; `ezsplit` shape configs embed credentials inline.

### KIS API Details

- Domestic balance: TR ID `TTTC8434R`, endpoint `/uapi/domestic-stock/v1/trading/inquire-balance`
- Overseas balance: TR ID `CTRP6504R`, endpoint `/uapi/overseas-stock/v1/trading/inquire-present-balance`
- Auth: OAuth2 client credentials at `/oauth2/tokenP`, tokens valid ~24h
- Rate limit: 0.1s sleep between paginated requests

### Routes

- `GET /` — Portfolio list grouped by owner (display order follows the `owners.yaml` list, then unknown)
- `GET /portfolio/{name}` — Detail view with live balance query; computes actual vs target weight
- `GET /reload` — Clears cached portfolio list (portfolios are loaded once and cached in `_portfolios` global). **Must be called after editing any source config**, since the running server holds a stale cache otherwise.

### Conventions

- Holdings dicts use Korean field names (`종목코드`, `종목명`, `평가금액`, `수익률`, etc.) throughout the backend and templates.
- Owner detection is content-based via the owner list in `config/owners.yaml` (loaded by `config_loader.py` into `KNOWN_OWNERS`); add a new owner by appending to that file and calling `/reload`.
