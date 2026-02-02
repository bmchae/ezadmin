# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the web server (port 9000)
python app.py

# Install dependencies
pip install -r requirements.txt
```

## Architecture

ezadmin is a Flask web dashboard that reads portfolio configurations from two sibling projects (`~/ez/ezgain` and `~/ez/ezinvest`) and queries KIS (Korea Investment & Securities) Open API to display account holdings and balances.

### Data Flow

1. **config_loader.py** scans `~/ez/ezgain/config/portfolio-*.yaml` and `~/ez/ezinvest/config/portfolio-*.yaml`, loads each portfolio config along with its referenced KIS account config (e.g., `kis-bmchae.yaml`), detects the owner from the filename, and returns a list of portfolio dicts.

2. **kis_client.py** calls KIS REST API directly (independent of ezgain/ezinvest's KIS modules which use global state). It reuses OAuth tokens from each project's `token/` directory (file naming: `KIS-{config_name}-{YYYYMMDD}`). Supports both domestic (`inquire-balance`) and overseas (`inquire-present-balance`) stock queries with pagination.

3. **app.py** groups portfolios by owner, and on detail view calculates actual weight vs target weight (from portfolio config's `universe` section) for each holding.

### Key Design Decisions

- KIS API client is standalone (not importing from ezgain/ezinvest) because the original modules use Python global state (`_cfg`, `_TRENV`, `_base_headers`) that conflicts when querying multiple accounts sequentially.
- Token files are shared with ezgain/ezinvest projects — ezadmin reads their `token/` directories first and only issues new tokens when none are valid.
- Portfolio configs reference account configs by filename (e.g., `account_config: kis-bmchae.yaml`), resolved relative to the same `config/` directory.

### KIS API Details

- Domestic balance: TR ID `TTTC8434R`, endpoint `/uapi/domestic-stock/v1/trading/inquire-balance`
- Overseas balance: TR ID `CTRP6504R`, endpoint `/uapi/overseas-stock/v1/trading/inquire-present-balance`
- Auth: OAuth2 client credentials at `/oauth2/tokenP`, tokens valid ~24h
- Rate limit: 0.1s sleep between paginated requests

### Routes

- `GET /` — Portfolio list grouped by owner (display order: bmchae, hitomato, 0eh, 9bong)
- `GET /portfolio/<name>` — Detail view with live KIS API balance query; computes actual vs target weight
- `GET /reload` — Clears cached portfolio list (portfolios are loaded once and cached in `_portfolios` global)

### Conventions

- Holdings dicts use Korean field names (`종목코드`, `종목명`, `평가금액`, `수익률`, etc.) throughout the backend and templates.
- Owner detection is filename-based via `KNOWN_OWNERS` list in `config_loader.py`.
