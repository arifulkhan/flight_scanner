# Flight Scanner

Flight Scanner is a local web app that compares round-trip options across multiple start and destination airports, then ranks trips using both flight cost and your custom ground-travel costs.  
It includes a Python API backend plus a lightweight frontend so you can run everything on your machine and evaluate itinerary combinations quickly.

## Features

- Comma-separated `start airports` input (default: `PSC, SEA, PDX, GEG`)
- Comma-separated `destination airports` input (default: `JFK, LGA`)
- Batch search and comparison across flexible date windows (`depart +/- 3 days`, `return +/- 3 days`)
- Dynamic cost matrix with per-start-airport:
  - one-time cost
  - per-day cost
- Result filtering, sorting, and CSV export
- Simplified total price calculation:

`totalPrice = flightPrice + oneTimeCost + perDayCost * tripDays`

## Run

```bash
cd flight_scanner
# first-time setup:
cp api.example.txt api.txt
# edit api.txt and add your own key(s)
./run.sh
```

Open:

- `http://127.0.0.1:5500/index.html`

Backend API default is `http://127.0.0.1:8787`.

## Credentials (safe for public GitHub repos)

Do not commit secrets. This project is configured so your local credentials stay out of git:

- `api.txt` is read automatically by `run.sh`
- `api.txt` is ignored via `.gitignore`

Create `api.txt` from template:

```bash
cp api.example.txt api.txt
```

Then edit `api.txt` with one of:

- `AMADEUS_CLIENT_ID` and `AMADEUS_CLIENT_SECRET` (recommended)
- `SERPAPI_KEY` (alternative)

You can also provide optional settings like:

- `FLIGHT_PROVIDER`
- `AMADEUS_HOST`
- `AMADEUS_MAX_RESULTS`
- `PREFERRED_AIRLINES`
- `ADMIN_TOKEN`

Each line in `api.txt` should be `KEY=VALUE`.
