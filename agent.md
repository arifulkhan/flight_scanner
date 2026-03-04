# Flight Scanner Agent Guide

## Purpose
Maintain and extend the `flight_scanner` app.
This app compares round-trip flight options and returns normalized itinerary results.

## Architecture
- Frontend: static files (`index.html`, `styles.css`, `app.js`)
- Backend: Python HTTP server (`server.py`)
- Runner: `run.sh` (starts backend + static server)
- Cache: SQLite (`flight_cache.sqlite3`)

Browser calls backend only:
- `POST /api/flights/search-batch`
- `GET /health`
- `POST /cache/clear`

## Provider Logic
The backend uses provider-chain execution with retries and backoff.
Priority is controlled by `PROVIDER_PRIORITY` in `api.txt`.
Supported providers:
- `scraperapi`
- `browserless`
- `rapidapi`
- `serpapi`
- `amadeus`

Per-leg response includes execution trace and provider error details.

## Secrets and Config
- Never hardcode secrets.
- Read credentials from `api.txt` / environment.
- Keep `api.txt` out of commits.
- `RAPIDAPI_SEARCH_PATH` is accepted as alias for `RAPIDAPI_FLIGHT_PATH`.

## Development Rules
- Keep API response shape backward-compatible when possible.
- Preserve key fields used by UI:
  - `results`
  - `cache`
  - `provider`
  - `execution_status`
  - `provider_errors`
- If adding providers, normalize output through existing quote format.
- Maintain retry/backoff behavior and explicit provider failure messages.

## Local Run
```bash
cd flight_scanner
./run.sh
```

## Validation Checklist
- `python3 -m py_compile server.py`
- `GET /health` returns configured provider info.
- `POST /api/flights/search-batch` returns:
  - itinerary rows
  - cache metrics
  - execution status (attempts/retries/outcomes)

## Git Hygiene
- Do not commit cache DB files.
- Commit only source/config templates/docs.
