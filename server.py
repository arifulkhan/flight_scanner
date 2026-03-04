#!/usr/bin/env python3
import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8787"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "6"))

# Provider selection:
# - set FLIGHT_PROVIDER=amadeus or serpapi explicitly
# - if unset: prefer amadeus when AMADEUS creds exist; otherwise use serpapi
FLIGHT_PROVIDER = os.getenv("FLIGHT_PROVIDER", "").strip().lower()
PROVIDER_PRIORITY = [
    p.strip().lower()
    for p in os.getenv(
        "PROVIDER_PRIORITY",
        "serpapi,rapidapi,scraperapi,browserless,amadeus",
    ).split(",")
    if p.strip()
]

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
SERPAPI_BASE = "https://serpapi.com/search.json"
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPERAPI_PREMIUM = os.getenv("SCRAPERAPI_PREMIUM", "1").strip().lower() in ("1", "true", "yes", "y")
SCRAPERAPI_ULTRA_PREMIUM = os.getenv("SCRAPERAPI_ULTRA_PREMIUM", "0").strip().lower() in ("1", "true", "yes", "y")
SCRAPERAPI_TIMEOUT_SEC = int(os.getenv("SCRAPERAPI_TIMEOUT_SEC", "20"))
BROWSERLESS_TOKEN = os.getenv("BROWSERLESS_TOKEN", "").strip()
BROWSERLESS_BASE = os.getenv("BROWSERLESS_BASE", "https://chrome.browserless.io").strip()
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "").strip()
RAPIDAPI_FLIGHT_PATH = os.getenv("RAPIDAPI_FLIGHT_PATH", os.getenv("RAPIDAPI_SEARCH_PATH", "")).strip()

AMADEUS_HOST = os.getenv("AMADEUS_HOST", "test.api.amadeus.com").strip()
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID", "").strip()
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET", "").strip()
AMADEUS_MAX_RESULTS = int(os.getenv("AMADEUS_MAX_RESULTS", "5"))
PREFERRED_AIRLINES_RAW = os.getenv("PREFERRED_AIRLINES", "Alaska,Delta,United").strip()

CACHE_DB_PATH = os.getenv("CACHE_DB_PATH", "./flight_cache.sqlite3")
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "1800"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
PROVIDER_RETRY_ATTEMPTS = int(os.getenv("PROVIDER_RETRY_ATTEMPTS", "2"))
PROVIDER_BACKOFF_BASE_SEC = float(os.getenv("PROVIDER_BACKOFF_BASE_SEC", "1.0"))
AMADEUS_RETRY_ATTEMPTS = int(os.getenv("AMADEUS_RETRY_ATTEMPTS", "2"))
SERPAPI_RETRY_ATTEMPTS = int(os.getenv("SERPAPI_RETRY_ATTEMPTS", "2"))

CACHE_LOCK = Lock()
TOKEN_LOCK = Lock()
AMADEUS_TOKEN = ""
AMADEUS_TOKEN_EXPIRES_AT = 0

AIRPORT_CITY_BY_CODE = {
    "ATL": "Atlanta",
    "AUS": "Austin",
    "BNA": "Nashville",
    "BOS": "Boston",
    "BWI": "Baltimore",
    "BUR": "Burbank",
    "CLT": "Charlotte",
    "CVG": "Cincinnati",
    "DAL": "Dallas",
    "DCA": "Washington",
    "DEN": "Denver",
    "DFW": "Dallas",
    "DTW": "Detroit",
    "EWR": "Newark",
    "FLL": "Fort Lauderdale",
    "GEG": "Spokane",
    "HNL": "Honolulu",
    "IAD": "Washington",
    "IAH": "Houston",
    "IND": "Indianapolis",
    "JAX": "Jacksonville",
    "JFK": "New York",
    "LAS": "Las Vegas",
    "LAX": "Los Angeles",
    "LGA": "New York",
    "MCI": "Kansas City",
    "MCO": "Orlando",
    "MDW": "Chicago",
    "MEM": "Memphis",
    "MIA": "Miami",
    "MSP": "Minneapolis",
    "MSY": "New Orleans",
    "OAK": "Oakland",
    "ONT": "Ontario",
    "ORD": "Chicago",
    "PAE": "Everett",
    "PDX": "Portland",
    "PHL": "Philadelphia",
    "PHX": "Phoenix",
    "PIT": "Pittsburgh",
    "PSC": "Pasco",
    "RDU": "Raleigh",
    "RNO": "Reno",
    "SAN": "San Diego",
    "SAT": "San Antonio",
    "SDF": "Louisville",
    "SEA": "Seattle",
    "SFO": "San Francisco",
    "SJC": "San Jose",
    "SLC": "Salt Lake City",
    "SMF": "Sacramento",
    "SNA": "Santa Ana",
    "STL": "St Louis",
    "TPA": "Tampa",
}


def preferred_airline_tokens():
    if not PREFERRED_AIRLINES_RAW:
        return []
    parts = [p.strip().lower() for p in PREFERRED_AIRLINES_RAW.split(",")]
    return [p for p in parts if p]


PREFERRED_AIRLINES = preferred_airline_tokens()


def configured_providers():
    if FLIGHT_PROVIDER in ("scraperapi", "browserless", "rapidapi", "serpapi", "amadeus"):
        return [FLIGHT_PROVIDER]

    available = []
    for provider in PROVIDER_PRIORITY:
        if provider == "scraperapi" and SCRAPERAPI_KEY and SERPAPI_KEY:
            available.append("scraperapi")
        elif provider == "browserless" and BROWSERLESS_TOKEN and SERPAPI_KEY:
            available.append("browserless")
        elif provider == "rapidapi" and RAPIDAPI_KEY and RAPIDAPI_HOST and RAPIDAPI_FLIGHT_PATH:
            available.append("rapidapi")
        elif provider == "serpapi" and SERPAPI_KEY:
            available.append("serpapi")
        elif provider == "amadeus" and AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET:
            available.append("amadeus")
    return available


def active_provider():
    providers = configured_providers()
    if providers:
        return providers[0]
    return "none"


def provider_validation():
    checks = []
    checks.append(
        {
            "provider": "serpapi",
            "configured": bool(SERPAPI_KEY),
            "required": ["SERPAPI_KEY"],
            "free_tier_notes": "Free tier is limited; quota exhaustion is common for frequent searches.",
        }
    )
    checks.append(
        {
            "provider": "rapidapi",
            "configured": bool(RAPIDAPI_KEY and RAPIDAPI_HOST and RAPIDAPI_FLIGHT_PATH),
            "required": ["RAPIDAPI_KEY", "RAPIDAPI_HOST", "RAPIDAPI_FLIGHT_PATH (or RAPIDAPI_SEARCH_PATH alias)"],
            "free_tier_notes": "Depends on subscribed API listing; monthly quotas on free/basic plans are often low.",
        }
    )
    checks.append(
        {
            "provider": "scraperapi",
            "configured": bool(SCRAPERAPI_KEY and SERPAPI_KEY),
            "required": ["SCRAPERAPI_KEY", "SERPAPI_KEY"],
            "free_tier_notes": "Proxying API endpoints may fail for protected targets; premium/ultra_premium may be required.",
        }
    )
    checks.append(
        {
            "provider": "browserless",
            "configured": bool(BROWSERLESS_TOKEN and SERPAPI_KEY),
            "required": ["BROWSERLESS_TOKEN", "SERPAPI_KEY", "BROWSERLESS_BASE(optional)"],
            "free_tier_notes": "May require paid plan for reliable use; API-JSON proxying via /content can be unstable.",
        }
    )
    checks.append(
        {
            "provider": "amadeus",
            "configured": bool(AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET),
            "required": ["AMADEUS_CLIENT_ID", "AMADEUS_CLIENT_SECRET", "AMADEUS_HOST(optional)"],
            "free_tier_notes": "Self-service test environment available but requires account setup.",
        }
    )
    return {
        "checks": checks,
        "recommended_free_tier_order": ["serpapi", "rapidapi", "scraperapi", "browserless", "amadeus"],
    }


def parse_price(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    digits = re.sub(r"[^0-9.]", "", str(value))
    if not digits:
        return None
    return int(round(float(digits)))


def layover_duration_text(total_min):
    if total_min is None:
        return "Unknown duration"
    hours = int(total_min) // 60
    mins = int(total_min) % 60
    return f"{hours:02d} hr: {mins:02d} mins"


def compact_time_text(value):
    text = str(value or "").replace("\u202f", " ").replace("\xa0", " ").strip()
    if not text:
        return "Unknown"
    if " on " in text:
        text = text.split(" on ", 1)[0].strip()

    # Already in AM/PM style.
    if re.search(r"\b(AM|PM)\b", text, flags=re.IGNORECASE):
        return text.upper().replace("  ", " ")

    # ISO-like datetime: 2026-06-14T19:35[:ss][Z|+hh:mm]
    iso_match = re.search(
        r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)",
        text,
    )
    if iso_match:
        dt = parse_iso_datetime(iso_match.group(1).replace(" ", "T"))
        if dt:
            return dt.strftime("%I:%M %p").lstrip("0")

    # Plain date + time: 2026-06-14 19:35
    dt_match = re.search(r"\d{4}-\d{2}-\d{2}\s+(\d{2}):(\d{2})", text)
    if dt_match:
        hour = int(dt_match.group(1))
        minute = int(dt_match.group(2))
        return _format_hour_minute(hour, minute)

    # Plain 24h time: 19:35
    hm_match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if hm_match:
        hour = int(hm_match.group(1))
        minute = int(hm_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return _format_hour_minute(hour, minute)

    return text


def _format_hour_minute(hour, minute):
    suffix = "PM" if hour >= 12 else "AM"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{minute:02d} {suffix}"


def format_clock_from_iso(iso_value):
    dt = parse_iso_datetime(iso_value)
    if not dt:
        return "Unknown"
    return dt.strftime("%I:%M %p").lstrip("0")


def guess_city_from_airport_name(name):
    text = str(name or "").strip()
    if not text:
        return ""
    suffixes = (
        " International Airport",
        " Regional Airport",
        " Municipal Airport",
        " Airport",
    )
    for suffix in suffixes:
        if text.endswith(suffix):
            return text[: -len(suffix)].strip()
    return text


def airport_city(code, name=None):
    iata = str(code or "").upper().strip()
    if iata in AIRPORT_CITY_BY_CODE:
        return AIRPORT_CITY_BY_CODE[iata]
    guessed = guess_city_from_airport_name(name)
    if guessed:
        return guessed
    return "Unknown"


def format_stop_text(airport_code, airport_name=None, layover_min=None):
    code = str(airport_code or "Connection").upper()
    city = airport_city(code, airport_name)
    return f"{code}, {city}, {layover_duration_text(layover_min)}"


def is_stop_text_current_format(text):
    value = str(text or "").strip()
    if value in ("", "Unknown", "No result", "Unavailable"):
        return True
    return " - " in value


def build_nonstop_itinerary_text(start_time, land_time):
    return f"{start_time} - {land_time}"


def build_one_stop_itinerary_text(start_time, first_land_time, stop_code, layover_min, second_leg_start_time, final_time):
    return (
        f"{start_time} - {first_land_time}, "
        f"{str(stop_code or 'Connection').upper()}, "
        f"{layover_duration_text(layover_min)}, "
        f"{second_leg_start_time} - {final_time}"
    )


def parse_iso_duration_minutes(value):
    if not value:
        return None
    text = str(value)
    match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?$", text)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    mins = int(match.group(2) or 0)
    return hours * 60 + mins


def parse_iso_datetime(value):
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def is_preferred_airline(name):
    if not PREFERRED_AIRLINES:
        return False
    text = (name or "").lower()
    return any(token in text for token in PREFERRED_AIRLINES)


def option_sort_key(option):
    preferred_rank = 0 if is_preferred_airline(option.get("airline")) else 1
    price = option.get("price")
    duration = option.get("duration")
    return (
        preferred_rank,
        price if price is not None else 10**9,
        duration if duration is not None else 10**9,
    )


def db_connect():
    return sqlite3.connect(CACHE_DB_PATH)


def init_cache_db():
    with CACHE_LOCK:
        conn = db_connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leg_quotes (
                  origin TEXT NOT NULL,
                  destination TEXT NOT NULL,
                  depart_date TEXT NOT NULL,
                  price INTEGER,
                  duration INTEGER,
                  stop_text TEXT,
                  airline TEXT,
                  provider TEXT,
                  fetched_at INTEGER NOT NULL,
                  PRIMARY KEY (origin, destination, depart_date)
                )
                """
            )
            # Migrate older cache DBs that were created before newer columns existed.
            existing_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(leg_quotes)").fetchall()
            }
            required_cols = {
                "price": "INTEGER",
                "duration": "INTEGER",
                "stop_text": "TEXT",
                "airline": "TEXT",
                "provider": "TEXT",
                "fetched_at": "INTEGER NOT NULL DEFAULT 0",
            }
            for col_name, col_type in required_cols.items():
                if col_name not in existing_cols:
                    conn.execute(
                        f"ALTER TABLE leg_quotes ADD COLUMN {col_name} {col_type}"
                    )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_leg_quotes_fetched_at
                ON leg_quotes (fetched_at)
                """
            )
            conn.commit()
        finally:
            conn.close()


def cache_get_leg(leg):
    now_ts = int(time.time())
    with CACHE_LOCK:
        conn = db_connect()
        try:
            row = conn.execute(
                """
                SELECT price, duration, stop_text, airline, provider, fetched_at
                FROM leg_quotes
                WHERE origin = ? AND destination = ? AND depart_date = ?
                """,
                (leg[0], leg[1], leg[2]),
            ).fetchone()
        finally:
            conn.close()

    if not row:
        return None

    price, duration, stop_text, airline, provider, fetched_at = row
    if now_ts - int(fetched_at) > CACHE_TTL_SEC:
        return None
    if not is_stop_text_current_format(stop_text):
        return None

    quote = {
        "price": price,
        "duration": duration,
        "stop_text": stop_text or "Unknown",
        "airline": airline or "Unknown",
        "provider": provider or "unknown",
    }
    if price is None:
        stop = str(stop_text or "").strip().lower()
        if stop in ("no result",):
            quote["error_code"] = "no_results"
            quote["error_message"] = "No matching flights returned (cached)"
        elif stop in ("unavailable",):
            quote["error_code"] = "provider_error"
            quote["error_message"] = "Provider request failed previously (cached)"
        else:
            quote["error_code"] = "unpriced"
            quote["error_message"] = "Quote is missing price (cached)"
    return quote


def cache_set_leg(leg, quote):
    with CACHE_LOCK:
        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO leg_quotes
                  (origin, destination, depart_date, price, duration, stop_text, airline, provider, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(origin, destination, depart_date)
                DO UPDATE SET
                  price = excluded.price,
                  duration = excluded.duration,
                  stop_text = excluded.stop_text,
                  airline = excluded.airline,
                  provider = excluded.provider,
                  fetched_at = excluded.fetched_at
                """,
                (
                    leg[0],
                    leg[1],
                    leg[2],
                    quote.get("price"),
                    quote.get("duration"),
                    quote.get("stop_text"),
                    quote.get("airline", "Unknown"),
                    quote.get("provider", "unknown"),
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def cache_clear_all():
    with CACHE_LOCK:
        conn = db_connect()
        try:
            deleted = conn.execute("DELETE FROM leg_quotes").rowcount
            conn.commit()
            return int(deleted or 0)
        finally:
            conn.close()


def cache_clear_expired():
    cutoff = int(time.time()) - CACHE_TTL_SEC
    with CACHE_LOCK:
        conn = db_connect()
        try:
            deleted = conn.execute(
                "DELETE FROM leg_quotes WHERE fetched_at < ?",
                (cutoff,),
            ).rowcount
            conn.commit()
            return int(deleted or 0)
        finally:
            conn.close()


def auth_header_token(headers):
    bearer = headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return headers.get("X-Admin-Token", "").strip()


def is_admin_authorized(headers):
    if not ADMIN_TOKEN:
        return True
    return auth_header_token(headers) == ADMIN_TOKEN


def http_json(method, url, headers=None, body=None, timeout=35):
    req = Request(url, data=body, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            raw = ""
        if raw:
            raise HTTPError(exc.url, exc.code, f"{exc.reason} | {raw[:220]}", exc.hdrs, exc.fp)
        raise


def http_post_json(url, payload, headers=None, timeout=35):
    body = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        merged.update(headers)
    return http_json("POST", url, headers=merged, body=body, timeout=timeout)


def error_quote(provider, code, message, stop_text="Unavailable"):
    return {
        "price": None,
        "duration": None,
        "stop_text": stop_text,
        "airline": "Unknown",
        "provider": provider,
        "error_code": code,
        "error_message": str(message or "").strip() or "Unknown error",
    }


# ---------- SerpApi provider ----------

def extract_airline_from_serp_item(flight_item):
    names = []
    for seg in flight_item.get("flights") or []:
        name = seg.get("airline") or seg.get("airline_name")
        if name and name not in names:
            names.append(str(name))

    if not names:
        fallback = flight_item.get("airline") or flight_item.get("airline_name")
        if fallback:
            names.append(str(fallback))

    if not names:
        return "Unknown"
    if len(names) == 1:
        return names[0]
    return " / ".join(names[:2])


def best_option_from_serp(data):
    options = []
    for key in ("best_flights", "other_flights"):
        for item in data.get(key, []) or []:
            price = parse_price(item.get("price"))
            duration = item.get("total_duration")
            flights = item.get("flights") or []
            layovers = item.get("layovers") or []
            airline = extract_airline_from_serp_item(item)

            if duration is None and flights:
                duration = sum(int(seg.get("duration", 0)) for seg in flights)

            if flights:
                start_time = compact_time_text((flights[0].get("departure_airport") or {}).get("time"))
                final_time = compact_time_text((flights[-1].get("arrival_airport") or {}).get("time"))
            else:
                start_time = "Unknown"
                final_time = "Unknown"

            if len(flights) <= 1:
                stop_text = build_nonstop_itinerary_text(start_time, final_time)
            elif len(flights) == 2:
                first_arrival = flights[0].get("arrival_airport", {})
                first_land_time = compact_time_text(first_arrival.get("time"))
                first_layover = layovers[0] if layovers else {}
                layover_min = first_layover.get("duration")
                stop_code = first_arrival.get("id") or first_layover.get("id")
                second_leg_start_time = compact_time_text(
                    (flights[1].get("departure_airport") or {}).get("time")
                )
                stop_text = build_one_stop_itinerary_text(
                    start_time,
                    first_land_time,
                    stop_code,
                    layover_min,
                    second_leg_start_time,
                    final_time,
                )
            else:
                first_arrival = flights[0].get("arrival_airport", {})
                first_land_time = compact_time_text(first_arrival.get("time"))
                first_layover = layovers[0] if layovers else {}
                layover_min = first_layover.get("duration")
                stop_code = first_arrival.get("id") or first_layover.get("id")
                second_leg_start_time = compact_time_text(
                    (flights[1].get("departure_airport") or {}).get("time")
                )
                stop_text = (
                    build_one_stop_itinerary_text(
                        start_time,
                        first_land_time,
                        stop_code,
                        layover_min,
                        second_leg_start_time,
                        final_time,
                    )
                    + " (multi-stop)"
                )

            if price is None:
                continue

            options.append(
                {
                    "price": price,
                    "duration": int(duration) if duration is not None else 0,
                    "stop_text": stop_text,
                    "airline": airline,
                    "provider": "serpapi",
                }
            )

    if not options:
        return None

    options.sort(key=option_sort_key)
    return options[0]


def build_serpapi_flights_url(origin, destination, date):
    params = {
        "engine": "google_flights",
        "api_key": SERPAPI_KEY,
        "hl": "en",
        "gl": "us",
        "currency": "USD",
        "type": "2",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": date,
        "deep_search": "false",
    }
    return f"{SERPAPI_BASE}?{urlencode(params)}"


def quote_from_serp_data(data, provider_name):
    provider_error = data.get("error")
    if provider_error:
        text = str(provider_error)
        if "rate limit" in text.lower() or "too many requests" in text.lower() or "run out of searches" in text.lower():
            return error_quote(provider_name, "rate_limited", text)
        if "auth" in text.lower() or "unauthorized" in text.lower() or "forbidden" in text.lower():
            return error_quote(provider_name, "auth_error", text)
        return error_quote(provider_name, "provider_error", text)

    option = best_option_from_serp(data)
    if option is None:
        return error_quote(provider_name, "no_results", "No matching flights returned", stop_text="No result")
    option["provider"] = provider_name
    return option


def fetch_one_way_leg_serpapi(origin, destination, date):
    if not SERPAPI_KEY:
        raise RuntimeError("SERPAPI_KEY is not set")

    url = build_serpapi_flights_url(origin, destination, date)
    try:
        data = http_json(
            "GET",
            url,
            headers={"Accept": "application/json", "User-Agent": "flight-planner/1.0"},
        )
    except HTTPError as exc:
        if exc.code == 429:
            return error_quote("serpapi", "rate_limited", "SerpApi rate limit reached")
        if exc.code in (401, 403):
            return error_quote("serpapi", "auth_error", "SerpApi authorization failed")
        return error_quote("serpapi", f"http_{exc.code}", f"SerpApi HTTP {exc.code}: {exc.reason}")
    except URLError as exc:
        return error_quote("serpapi", "network_error", f"SerpApi network error: {exc.reason}")
    except Exception as exc:
        return error_quote("serpapi", "provider_error", f"SerpApi request failed: {exc}")

    return quote_from_serp_data(data, "serpapi")


def fetch_one_way_leg_scraperapi(origin, destination, date):
    if not (SCRAPERAPI_KEY and SERPAPI_KEY):
        raise RuntimeError("SCRAPERAPI_KEY and SERPAPI_KEY are required")
    serp_url = build_serpapi_flights_url(origin, destination, date)
    params = {"api_key": SCRAPERAPI_KEY, "url": serp_url}
    if SCRAPERAPI_PREMIUM:
        params["premium"] = "true"
    if SCRAPERAPI_ULTRA_PREMIUM:
        params["ultra_premium"] = "true"
    proxy_url = "http://api.scraperapi.com/?" + urlencode(params)
    try:
        data = http_json(
            "GET",
            proxy_url,
            headers={"Accept": "application/json", "User-Agent": "flight-planner/1.0"},
            timeout=max(5, SCRAPERAPI_TIMEOUT_SEC),
        )
    except HTTPError as exc:
        reason = str(exc.reason or "")
        lower_reason = reason.lower()
        if "no request data available for" in lower_reason:
            return error_quote(
                "scraperapi",
                "unsupported_target",
                "ScraperAPI cannot proxy this SerpAPI endpoint for the request.",
            )
        if "protected domains may require" in lower_reason:
            return error_quote(
                "scraperapi",
                "unsupported_target",
                "ScraperAPI target requires premium/ultra_premium access or different target URL.",
            )
        if exc.code == 429:
            return error_quote("scraperapi", "rate_limited", f"ScraperAPI rate limit: {exc.reason}")
        if exc.code in (401, 403):
            return error_quote("scraperapi", "auth_error", f"ScraperAPI auth failed: {exc.reason}")
        return error_quote("scraperapi", f"http_{exc.code}", f"ScraperAPI HTTP {exc.code}: {exc.reason}")
    except URLError as exc:
        return error_quote("scraperapi", "network_error", f"ScraperAPI network error: {exc.reason}")
    except Exception as exc:
        text = str(exc or "")
        if "No request data available for" in text:
            return error_quote(
                "scraperapi",
                "unsupported_target",
                "ScraperAPI cannot proxy this SerpAPI endpoint for the request.",
            )
        if "Protected domains may require" in text:
            return error_quote(
                "scraperapi",
                "unsupported_target",
                "ScraperAPI target requires premium/ultra_premium access or different target URL.",
            )
        return error_quote("scraperapi", "provider_error", f"ScraperAPI request failed: {exc}")
    return quote_from_serp_data(data, "scraperapi")


def fetch_one_way_leg_browserless(origin, destination, date):
    if not (BROWSERLESS_TOKEN and SERPAPI_KEY):
        raise RuntimeError("BROWSERLESS_TOKEN and SERPAPI_KEY are required")
    serp_url = build_serpapi_flights_url(origin, destination, date)
    url = f"{BROWSERLESS_BASE.rstrip('/')}/content?token={BROWSERLESS_TOKEN}"
    try:
        data = http_post_json(url, {"url": serp_url}, timeout=35)
    except HTTPError as exc:
        if exc.code in (401, 403):
            return error_quote("browserless", "auth_error", f"Browserless auth failed: {exc.reason}")
        if exc.code == 429:
            return error_quote("browserless", "rate_limited", f"Browserless rate limit: {exc.reason}")
        return error_quote("browserless", f"http_{exc.code}", f"Browserless HTTP {exc.code}: {exc.reason}")
    except URLError as exc:
        return error_quote("browserless", "network_error", f"Browserless network error: {exc.reason}")
    except Exception as exc:
        return error_quote("browserless", "provider_error", f"Browserless request failed: {exc}")
    return quote_from_serp_data(data, "browserless")


def fetch_one_way_leg_rapidapi(origin, destination, date):
    if not (RAPIDAPI_KEY and RAPIDAPI_HOST and RAPIDAPI_FLIGHT_PATH):
        raise RuntimeError("RAPIDAPI_KEY/RAPIDAPI_HOST/RAPIDAPI_FLIGHT_PATH are required")
    params = {
        "engine": "google_flights",
        "hl": "en",
        "gl": "us",
        "currency": "USD",
        "type": "2",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": date,
        "deep_search": "false",
    }
    sep = "&" if "?" in RAPIDAPI_FLIGHT_PATH else "?"
    url = f"https://{RAPIDAPI_HOST}{RAPIDAPI_FLIGHT_PATH}{sep}{urlencode(params)}"
    try:
        data = http_json(
            "GET",
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "flight-planner/1.0",
                "X-RapidAPI-Key": RAPIDAPI_KEY,
                "X-RapidAPI-Host": RAPIDAPI_HOST,
            },
            timeout=35,
        )
    except HTTPError as exc:
        if exc.code in (401, 403):
            return error_quote("rapidapi", "auth_error", f"RapidAPI auth failed: {exc.reason}")
        if exc.code == 429:
            return error_quote("rapidapi", "rate_limited", f"RapidAPI rate limit: {exc.reason}")
        return error_quote("rapidapi", f"http_{exc.code}", f"RapidAPI HTTP {exc.code}: {exc.reason}")
    except URLError as exc:
        return error_quote("rapidapi", "network_error", f"RapidAPI network error: {exc.reason}")
    except Exception as exc:
        return error_quote("rapidapi", "provider_error", f"RapidAPI request failed: {exc}")
    return quote_from_serp_data(data, "rapidapi")


# ---------- Amadeus provider ----------

def amadeus_token_endpoint():
    return f"https://{AMADEUS_HOST}/v1/security/oauth2/token"


def amadeus_flight_offers_endpoint():
    return f"https://{AMADEUS_HOST}/v2/shopping/flight-offers"


def amadeus_get_access_token():
    global AMADEUS_TOKEN, AMADEUS_TOKEN_EXPIRES_AT

    if not AMADEUS_CLIENT_ID or not AMADEUS_CLIENT_SECRET:
        raise RuntimeError("AMADEUS_CLIENT_ID/AMADEUS_CLIENT_SECRET are not set")

    now = int(time.time())
    with TOKEN_LOCK:
        if AMADEUS_TOKEN and now < AMADEUS_TOKEN_EXPIRES_AT - 30:
            return AMADEUS_TOKEN

        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": AMADEUS_CLIENT_ID,
                "client_secret": AMADEUS_CLIENT_SECRET,
            }
        ).encode("utf-8")

        data = http_json(
            "POST",
            amadeus_token_endpoint(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "flight-planner/1.0",
            },
            body=body,
            timeout=20,
        )

        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 1800))
        if not token:
            raise RuntimeError("Failed to obtain Amadeus access token")

        AMADEUS_TOKEN = token
        AMADEUS_TOKEN_EXPIRES_AT = int(time.time()) + expires_in
        return AMADEUS_TOKEN


def best_option_from_amadeus(data):
    offers = data.get("data") or []
    carriers = (data.get("dictionaries") or {}).get("carriers") or {}

    options = []
    for offer in offers:
        price = parse_price(((offer.get("price") or {}).get("grandTotal") or (offer.get("price") or {}).get("total")))
        if price is None:
            continue

        itineraries = offer.get("itineraries") or []
        if not itineraries:
            continue

        itin = itineraries[0]
        duration = parse_iso_duration_minutes(itin.get("duration")) or 0
        segments = itin.get("segments") or []

        carrier_code = ""
        if segments:
            carrier_code = segments[0].get("carrierCode") or ""
        airline = carriers.get(carrier_code, carrier_code or "Unknown")

        if segments:
            start_time = format_clock_from_iso((segments[0].get("departure") or {}).get("at"))
            final_time = format_clock_from_iso((segments[-1].get("arrival") or {}).get("at"))
        else:
            start_time = "Unknown"
            final_time = "Unknown"

        if len(segments) <= 1:
            stop_text = build_nonstop_itinerary_text(start_time, final_time)
        else:
            first_arrival_data = segments[0].get("arrival") or {}
            first_stop_code = first_arrival_data.get("iataCode", "Connection")
            first_land_time = format_clock_from_iso(first_arrival_data.get("at"))
            arr_t = parse_iso_datetime(first_arrival_data.get("at"))
            dep_t = parse_iso_datetime((segments[1].get("departure") or {}).get("at"))
            layover = None
            if arr_t and dep_t and dep_t >= arr_t:
                layover = int((dep_t - arr_t).total_seconds() // 60)

            stop_text = build_one_stop_itinerary_text(
                start_time,
                first_land_time,
                first_stop_code,
                layover,
                format_clock_from_iso((segments[1].get("departure") or {}).get("at")),
                final_time,
            )
            if len(segments) > 2:
                stop_text += " (multi-stop)"

        options.append(
            {
                "price": price,
                "duration": duration,
                "stop_text": stop_text,
                "airline": airline,
                "provider": "amadeus",
            }
        )

    if not options:
        return None

    options.sort(key=option_sort_key)
    return options[0]


def fetch_one_way_leg_amadeus(origin, destination, date):
    token = amadeus_get_access_token()
    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": date,
        "adults": 1,
        "currencyCode": "USD",
        "max": max(1, min(AMADEUS_MAX_RESULTS, 250)),
    }
    url = f"{amadeus_flight_offers_endpoint()}?{urlencode(params)}"

    try:
        data = http_json(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "flight-planner/1.0",
            },
            timeout=35,
        )
    except HTTPError as exc:
        if exc.code in (401, 403):
            return error_quote("amadeus", "auth_error", "Amadeus authorization failed")
        if exc.code == 429:
            return error_quote("amadeus", "rate_limited", "Amadeus rate limit reached")
        return error_quote("amadeus", f"http_{exc.code}", f"Amadeus HTTP {exc.code}")
    except URLError as exc:
        return error_quote("amadeus", "network_error", f"Amadeus network error: {exc.reason}")
    except Exception as exc:
        return error_quote("amadeus", "provider_error", f"Amadeus request failed: {exc}")

    option = best_option_from_amadeus(data)
    if option is None:
        return error_quote("amadeus", "no_results", "No matching flights returned", stop_text="No result")
    return option


def fetch_one_way_leg(origin, destination, date):
    quote, _status = fetch_one_way_leg_with_status(origin, destination, date)
    return quote


def provider_chain_for_leg():
    chain = configured_providers()
    if chain:
        return chain
    # Legacy fallback path when explicit provider selected but missing config.
    primary = active_provider()
    return [primary] if primary != "none" else []


def max_attempts_for_provider(provider):
    if provider == "amadeus":
        return max(1, AMADEUS_RETRY_ATTEMPTS)
    if provider == "serpapi":
        return max(1, SERPAPI_RETRY_ATTEMPTS)
    return max(1, PROVIDER_RETRY_ATTEMPTS)


def run_provider_leg(provider, origin, destination, date):
    if provider == "scraperapi":
        return fetch_one_way_leg_scraperapi(origin, destination, date)
    if provider == "browserless":
        return fetch_one_way_leg_browserless(origin, destination, date)
    if provider == "rapidapi":
        return fetch_one_way_leg_rapidapi(origin, destination, date)
    if provider == "amadeus":
        return fetch_one_way_leg_amadeus(origin, destination, date)
    if provider == "serpapi":
        return fetch_one_way_leg_serpapi(origin, destination, date)
    raise RuntimeError(f"Unsupported provider: {provider}")


def quote_is_retryable(quote):
    code = str((quote or {}).get("error_code") or "").strip().lower()
    if code in ("unsupported_target", "no_results", "unpriced"):
        return False
    return code in ("network_error", "provider_error", "rate_limited", "auth_error") or code.startswith("http_")


def fetch_one_way_leg_with_status(origin, destination, date):
    chain = provider_chain_for_leg()
    execution = []
    last_quote = error_quote(active_provider(), "provider_error", "No provider executed")

    for provider in chain:
        attempts = max_attempts_for_provider(provider)
        step = {
            "provider": provider,
            "attempts": 0,
            "outcome": "not_run",
            "message": "",
            "result_price": None,
            "retries": [],
        }
        start = time.time()
        provider_quote = None

        for attempt in range(1, attempts + 1):
            step["attempts"] = attempt
            try:
                provider_quote = run_provider_leg(provider, origin, destination, date)
            except Exception as exc:
                provider_quote = error_quote(provider, "provider_error", str(exc))

            if provider_quote.get("price") is not None:
                step["outcome"] = "success"
                step["message"] = f"{provider} success on attempt {attempt}"
                step["result_price"] = provider_quote.get("price")
                break

            if attempt < attempts and quote_is_retryable(provider_quote):
                backoff_sec = PROVIDER_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                step["retries"].append(
                    {
                        "attempt": attempt,
                        "reason": provider_quote.get("error_message") or provider_quote.get("error_code") or "provider error",
                        "backoff_sec": round(backoff_sec, 2),
                    }
                )
                if backoff_sec > 0:
                    time.sleep(backoff_sec)
                continue
            break

        if provider_quote is None:
            provider_quote = error_quote(provider, "provider_error", "Unknown provider result")

        if step["outcome"] != "success":
            step["outcome"] = "failed"
            step["message"] = provider_quote.get("error_message") or "Provider returned no result"
        step["duration_ms"] = int((time.time() - start) * 1000)
        execution.append(step)
        last_quote = provider_quote

        if provider_quote.get("price") is not None:
            return provider_quote, execution

    return last_quote, execution


def itinerary_key(item):
    return "|".join(
        [
            item["origin"],
            item["destinationOutbound"],
            item["destinationInbound"],
            item["departDate"],
            item["returnDate"],
            str(item.get("travelers", 1)),
        ]
    )


def combine_airlines(outbound_airline, return_airline):
    out_name = outbound_airline or "Unknown"
    ret_name = return_airline or "Unknown"
    if out_name == ret_name:
        return out_name
    return f"{out_name} / {ret_name}"


def validate_iso_date(text):
    datetime.strptime(text, "%Y-%m-%d")


def process_batch(itineraries):
    cache_clear_expired()
    leg_queries = {}

    for item in itineraries:
        validate_iso_date(item["departDate"])
        validate_iso_date(item["returnDate"])

        out_leg = (item["origin"], item["destinationOutbound"], item["departDate"])
        ret_leg = (item["destinationInbound"], item["origin"], item["returnDate"])

        leg_queries[out_leg] = None
        leg_queries[ret_leg] = None

    results = {}
    leg_execution = {}
    cache_hits = 0
    cache_misses = 0
    to_fetch = []

    for leg in leg_queries.keys():
        cached = cache_get_leg(leg)
        if cached is not None:
            results[leg] = cached
            leg_execution[leg] = [
                {
                    "provider": cached.get("provider", "unknown"),
                    "attempts": 0,
                    "outcome": "cached",
                    "message": "cache hit",
                    "result_price": cached.get("price"),
                    "retries": [],
                    "duration_ms": 0,
                }
            ]
            cache_hits += 1
        else:
            to_fetch.append(leg)
            cache_misses += 1

    if to_fetch:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(fetch_one_way_leg_with_status, leg[0], leg[1], leg[2]): leg
                for leg in to_fetch
            }
            for future in as_completed(futures):
                leg = futures[future]
                try:
                    quote, execution = future.result()
                except Exception:
                    quote = error_quote(active_provider(), "provider_error", "Unhandled provider failure")
                    execution = [
                        {
                            "provider": active_provider(),
                            "attempts": 1,
                            "outcome": "failed",
                            "message": "Unhandled provider failure",
                            "result_price": None,
                            "retries": [],
                            "duration_ms": 0,
                        }
                    ]

                results[leg] = quote
                leg_execution[leg] = execution
                if quote.get("price") is not None:
                    cache_set_leg(leg, quote)

    payload_results = []
    for item in itineraries:
        travelers = max(1, int(item.get("travelers", 1)))
        out_leg = (item["origin"], item["destinationOutbound"], item["departDate"])
        ret_leg = (item["destinationInbound"], item["origin"], item["returnDate"])

        out_quote = results.get(out_leg, {})
        ret_quote = results.get(ret_leg, {})

        out_price = out_quote.get("price")
        ret_price = ret_quote.get("price")

        if out_price is None or ret_price is None:
            total_price = None
        else:
            total_price = (out_price + ret_price) * travelers

        outbound_error_code = out_quote.get("error_code")
        return_error_code = ret_quote.get("error_code")
        outbound_error_message = out_quote.get("error_message")
        return_error_message = ret_quote.get("error_message")

        itinerary_error_code = outbound_error_code or return_error_code
        itinerary_error_message = outbound_error_message or return_error_message

        payload_results.append(
            {
                "key": itinerary_key(item),
                "origin": item["origin"],
                "destinationOutbound": item["destinationOutbound"],
                "destinationInbound": item["destinationInbound"],
                "departDate": item["departDate"],
                "returnDate": item["returnDate"],
                "travelers": travelers,
                "outboundDurationMin": out_quote.get("duration") or 0,
                "returnDurationMin": ret_quote.get("duration") or 0,
                "outboundStopText": out_quote.get("stop_text") or "Unknown",
                "returnStopText": ret_quote.get("stop_text") or "Unknown",
                "outboundAirline": out_quote.get("airline") or "Unknown",
                "returnAirline": ret_quote.get("airline") or "Unknown",
                "airline": combine_airlines(out_quote.get("airline"), ret_quote.get("airline")),
                "totalFlightPrice": total_price,
                "status": "ok" if total_price is not None else "error",
                "outboundErrorCode": outbound_error_code,
                "outboundErrorMessage": outbound_error_message,
                "returnErrorCode": return_error_code,
                "returnErrorMessage": return_error_message,
                "errorCode": itinerary_error_code,
                "errorMessage": itinerary_error_message,
            }
        )

    execution_payload = []
    for leg, steps in leg_execution.items():
        execution_payload.append(
            {
                "origin": leg[0],
                "destination": leg[1],
                "date": leg[2],
                "steps": steps,
            }
        )

    provider_errors = []
    for leg_item in execution_payload:
        for step in leg_item.get("steps", []):
            if step.get("outcome") == "failed":
                provider_errors.append(
                    {
                        "origin": leg_item["origin"],
                        "destination": leg_item["destination"],
                        "date": leg_item["date"],
                        "provider": step.get("provider"),
                        "message": step.get("message"),
                    }
                )

    return {
        "results": payload_results,
        "provider": active_provider(),
        "providers_attempted": provider_chain_for_leg(),
        "cache": {
            "ttl_sec": CACHE_TTL_SEC,
            "hits": cache_hits,
            "misses": cache_misses,
        },
        "provider_errors": provider_errors,
        "execution_status": execution_payload,
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Admin-Token")
        self.send_header("Access-Control-Allow-Methods", "POST,GET,OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self._json(200, {"ok": True})

    def do_GET(self):
        if self.path == "/health":
            providers = configured_providers()
            validation = provider_validation()
            self._json(
                200,
                {
                    "ok": True,
                    "provider": active_provider(),
                    "provider_priority": PROVIDER_PRIORITY,
                    "providers_configured": providers,
                    "provider_validation": validation,
                    "serpapi_key_present": bool(SERPAPI_KEY),
                    "scraperapi_key_present": bool(SCRAPERAPI_KEY),
                    "browserless_token_present": bool(BROWSERLESS_TOKEN),
                    "rapidapi_key_present": bool(RAPIDAPI_KEY),
                    "rapidapi_host_present": bool(RAPIDAPI_HOST),
                    "rapidapi_flight_path_present": bool(RAPIDAPI_FLIGHT_PATH),
                    "amadeus_client_id_present": bool(AMADEUS_CLIENT_ID),
                    "amadeus_client_secret_present": bool(AMADEUS_CLIENT_SECRET),
                    "preferred_airlines": PREFERRED_AIRLINES,
                    "cache_db_path": CACHE_DB_PATH,
                    "cache_ttl_sec": CACHE_TTL_SEC,
                    "admin_token_required": bool(ADMIN_TOKEN),
                },
            )
            return
        if self.path == "/providers/validate":
            self._json(
                200,
                {
                    "ok": True,
                    "provider_priority": PROVIDER_PRIORITY,
                    **provider_validation(),
                },
            )
            return
        self._json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/cache/clear":
            if not is_admin_authorized(self.headers):
                self._json(401, {"error": "Unauthorized"})
                return
            deleted_rows = cache_clear_all()
            self._json(200, {"ok": True, "deleted_rows": deleted_rows})
            return

        if self.path != "/api/flights/search-batch":
            self._json(404, {"error": "Not found"})
            return

        providers = provider_chain_for_leg()
        if not providers:
            self._json(500, {"error": "No configured providers. Set PROVIDER_PRIORITY and provider credentials in api.txt"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length).decode("utf-8")
            payload = json.loads(body)
            itineraries = payload.get("itineraries", [])

            if not isinstance(itineraries, list) or not itineraries:
                self._json(400, {"error": "itineraries must be a non-empty list"})
                return

            started = time.time()
            result = process_batch(itineraries)
            result["elapsed_ms"] = int((time.time() - started) * 1000)
            self._json(200, result)
        except Exception as exc:
            self._json(500, {"error": str(exc)})


def main():
    init_cache_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Server listening at http://{HOST}:{PORT}")
    print(f"Provider: {active_provider()}")
    print(f"Cache DB: {CACHE_DB_PATH} (ttl={CACHE_TTL_SEC}s)")
    server.serve_forever()


if __name__ == "__main__":
    main()
