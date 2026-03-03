const DEFAULT_START_AIRPORTS = ["PSC", "SEA", "PDX", "GEG"];
const DEFAULT_DEST_AIRPORTS = ["JFK", "LGA"];
const PRESET_KEY = "flight_scanner_preset_v1";
const CACHE_CLEAR_KEY = "flight_scanner_cache_last_clear_v1";
const CACHE_CLEAR_INTERVAL_MS = 30 * 60 * 1000;

const tableState = {
  allRows: [],
  rows: [],
  sortBy: "totalPrice",
  sortDir: "asc",
  startAirports: [],
  destinationAirports: [],
  apiDiagnostics: null,
};

const costState = {};

const form = document.getElementById("search-form");
const summaryEl = document.getElementById("result-summary");
const resultsBody = document.getElementById("results-body");
const table = document.getElementById("results-table");
const runBtn = document.getElementById("run-btn");
const runTopBtn = document.getElementById("run-search-top-btn");
const costMatrixWrap = document.getElementById("cost-matrix-wrap");

const startAirportsEl = document.getElementById("startAirports");
const destinationAirportsEl = document.getElementById("destinationAirports");

const homeAirportFilterEl = document.getElementById("home-airport-filter");
const costColumnFilterEl = document.getElementById("cost-column-filter");
const maxCostFilterEl = document.getElementById("max-cost-filter");

document.getElementById("apply-filter-btn").addEventListener("click", applyFilters);
document.getElementById("clear-filter-btn").addEventListener("click", clearFilters);
document.getElementById("export-csv-btn").addEventListener("click", exportCsv);
document.getElementById("save-preset-btn").addEventListener("click", savePreset);
document.getElementById("load-preset-btn").addEventListener("click", loadPreset);

startAirportsEl.addEventListener("input", () => {
  renderCostMatrix();
  syncHomeAirportFilterOptions();
});

initializeDefaults();
wireSorting();
renderCostMatrix();
syncHomeAirportFilterOptions();
autoClearCacheIfStale();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  await runSearch();
});

runBtn.addEventListener("click", async () => {
  await runSearch();
});

if (runTopBtn) {
  runTopBtn.addEventListener("click", async () => {
    await runSearch();
  });
}

document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (target.id === "run-btn" || target.id === "run-search-top-btn") {
    event.preventDefault();
    await runSearch();
  }
});

async function runSearch() {
  const input = readForm();
  if (!input) {
    return;
  }

  setBusy(true);
  summaryEl.textContent = "Building itinerary matrix and fetching prices...";

  try {
    const datePairs = buildDatePairs(input.departDate, input.returnDate, 3);
    const searchData = await buildSearchRows({ input, datePairs });
    const filteredRows = pickTopThreePerAirportCombination(searchData.rows);

    tableState.allRows = filteredRows;
    tableState.startAirports = input.startAirports;
    tableState.destinationAirports = input.destinationAirports;
    tableState.apiDiagnostics = searchData.diagnostics;

    syncHomeAirportFilterOptions();
    applyFilters();
  } catch (error) {
    summaryEl.textContent = `Search failed: ${error.message}`;
  } finally {
    setBusy(false);
  }
}
window.__runSearch = runSearch;

function initializeDefaults() {
  const today = new Date();
  const depart = addDays(today, 21);
  const ret = addDays(today, 28);

  document.getElementById("departDate").value = toIsoDate(depart);
  document.getElementById("returnDate").value = toIsoDate(ret);

  if (!startAirportsEl.value.trim()) {
    startAirportsEl.value = DEFAULT_START_AIRPORTS.join(", ");
  }
  if (!destinationAirportsEl.value.trim()) {
    destinationAirportsEl.value = DEFAULT_DEST_AIRPORTS.join(", ");
  }

  const apiBaseEl = document.getElementById("flightApiBase");
  if (apiBaseEl && !apiBaseEl.value) {
    apiBaseEl.value = "http://127.0.0.1:8787";
  }
}

function setBusy(isBusy) {
  runBtn.disabled = isBusy;
  runBtn.textContent = isBusy ? "Running..." : "Run Search";
  if (runTopBtn) {
    runTopBtn.disabled = isBusy;
    runTopBtn.textContent = isBusy ? "Running..." : "Run Search";
  }
}

function readForm() {
  const departDateRaw = document.getElementById("departDate").value;
  const returnDateRaw = document.getElementById("returnDate").value;
  const travelerCount = Number(document.getElementById("travelerCount").value);

  const startAirports = parseAirportList(startAirportsEl.value, DEFAULT_START_AIRPORTS);
  const destinationAirports = parseAirportList(destinationAirportsEl.value, DEFAULT_DEST_AIRPORTS);

  if (!departDateRaw || !returnDateRaw || travelerCount < 1) {
    summaryEl.textContent = "Please complete all required fields.";
    return null;
  }
  if (startAirports.length === 0 || destinationAirports.length === 0) {
    summaryEl.textContent = "Please provide at least one start and one destination airport.";
    return null;
  }

  const departDate = new Date(`${departDateRaw}T00:00:00`);
  const returnDate = new Date(`${returnDateRaw}T00:00:00`);
  if (returnDate <= departDate) {
    summaryEl.textContent = "Return date must be after depart date.";
    return null;
  }

  const costsByAirport = {};
  for (const airport of startAirports) {
    const cost = costState[airport] || { homeToAirportCost: 0, airportToHomeCost: 0, perDayCost: 0 };
    costsByAirport[airport] = {
      homeToAirportCost: Number.isFinite(Number(cost.homeToAirportCost)) ? Number(cost.homeToAirportCost) : 0,
      airportToHomeCost: Number.isFinite(Number(cost.airportToHomeCost)) ? Number(cost.airportToHomeCost) : 0,
      perDayCost: Number.isFinite(Number(cost.perDayCost)) ? Number(cost.perDayCost) : 0,
    };
  }

  return {
    startAirports,
    destinationAirports,
    departDate,
    returnDate,
    travelerCount,
    flightApiBase: document.getElementById("flightApiBase").value.trim(),
    costsByAirport,
  };
}

function parseAirportList(raw, fallback) {
  const values = String(raw || "")
    .split(",")
    .map((part) => part.trim().toUpperCase())
    .filter(Boolean)
    .filter((value, index, arr) => arr.indexOf(value) === index)
    .filter((value) => /^[A-Z0-9]{3,4}$/.test(value));

  return values.length > 0 ? values : [...fallback];
}

function renderCostMatrix() {
  const airports = parseAirportList(startAirportsEl.value, DEFAULT_START_AIRPORTS);

  for (const airport of airports) {
    if (!costState[airport]) {
      costState[airport] = { homeToAirportCost: 0, airportToHomeCost: 0, perDayCost: 0 };
    }
  }

  costMatrixWrap.innerHTML = "";

  const tableEl = document.createElement("table");
  tableEl.className = "cost-matrix";

  const thead = document.createElement("thead");
  thead.innerHTML = `
    <tr>
      <th>Start airport</th>
      <th>Home to airport ($)</th>
      <th>Airport to home ($)</th>
      <th>Per day cost ($)</th>
    </tr>
  `;

  const tbody = document.createElement("tbody");

  for (const airport of airports) {
    const row = document.createElement("tr");

    row.innerHTML = `
      <td><span class="tag">${airport}</span></td>
      <td>
        <input
          type="number"
          min="0"
          step="1"
          value="${Number(costState[airport].homeToAirportCost) || 0}"
          data-airport="${airport}"
          data-cost-kind="homeToAirportCost"
        />
      </td>
      <td>
        <input
          type="number"
          min="0"
          step="1"
          value="${Number(costState[airport].airportToHomeCost) || 0}"
          data-airport="${airport}"
          data-cost-kind="airportToHomeCost"
        />
      </td>
      <td>
        <input
          type="number"
          min="0"
          step="1"
          value="${Number(costState[airport].perDayCost) || 0}"
          data-airport="${airport}"
          data-cost-kind="perDayCost"
        />
      </td>
    `;

    tbody.appendChild(row);
  }

  tableEl.appendChild(thead);
  tableEl.appendChild(tbody);
  costMatrixWrap.appendChild(tableEl);

  costMatrixWrap.querySelectorAll("input[data-airport]").forEach((inputEl) => {
    inputEl.addEventListener("input", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement)) {
        return;
      }
      const airport = target.dataset.airport;
      const costKind = target.dataset.costKind;
      if (!airport || !costKind) {
        return;
      }

      if (!costState[airport]) {
        costState[airport] = { homeToAirportCost: 0, airportToHomeCost: 0, perDayCost: 0 };
      }

      const numeric = Math.max(0, Number(target.value) || 0);
      costState[airport][costKind] = numeric;
      recalculateTotalsFromCostMatrix();
    });
  });
}

function recalculateTotalsFromCostMatrix() {
  if (!Array.isArray(tableState.allRows) || tableState.allRows.length === 0) {
    return;
  }

  for (const row of tableState.allRows) {
    const costs = costState[row.homeAirport] || { homeToAirportCost: 0, airportToHomeCost: 0, perDayCost: 0 };
    const homeToAirportCost = Number(costs.homeToAirportCost) || 0;
    const airportToHomeCost = Number(costs.airportToHomeCost) || 0;
    const perDayCost = Number(costs.perDayCost) || 0;
    const tripDays =
      Number(row.tripDays) ||
      Math.max(1, Math.ceil((new Date(row.returnDate) - new Date(row.departDate)) / (24 * 60 * 60 * 1000)));

    row.homeToAirportCost = homeToAirportCost;
    row.airportToHomeCost = airportToHomeCost;
    row.perDayCost = perDayCost;
    row.tripDays = tripDays;
    row.totalPrice = row.totalFlightPrice + homeToAirportCost + airportToHomeCost + perDayCost * tripDays;
  }

  applyFilters();
}

function syncHomeAirportFilterOptions() {
  const airports = parseAirportList(startAirportsEl.value, DEFAULT_START_AIRPORTS);
  const previous = homeAirportFilterEl.value;

  homeAirportFilterEl.innerHTML = '<option value="ALL">All</option>';
  for (const airport of airports) {
    const option = document.createElement("option");
    option.value = airport;
    option.textContent = airport;
    homeAirportFilterEl.appendChild(option);
  }

  homeAirportFilterEl.value = airports.includes(previous) ? previous : "ALL";
}

function buildDatePairs(baseDepart, baseReturn, windowDays) {
  const pairs = [];
  for (let departOffset = -windowDays; departOffset <= windowDays; departOffset += 1) {
    for (let returnOffset = -windowDays; returnOffset <= windowDays; returnOffset += 1) {
      const departDate = addDays(baseDepart, departOffset);
      const returnDate = addDays(baseReturn, returnOffset);
      if (returnDate <= departDate) {
        continue;
      }
      pairs.push({ departDate, returnDate });
    }
  }
  return pairs;
}

async function buildSearchRows({ input, datePairs }) {
  const itineraries = [];

  for (const homeAirport of input.startAirports) {
    for (const outDest of input.destinationAirports) {
      for (const retDest of input.destinationAirports) {
        for (const pair of datePairs) {
          itineraries.push({
            homeAirport,
            outDest,
            retDest,
            departDate: pair.departDate,
            returnDate: pair.returnDate,
            travelerCount: input.travelerCount,
          });
        }
      }
    }
  }

  const quoteResponse = await fetchBatchQuotes(itineraries, input.flightApiBase);
  const flightQuotes = quoteResponse.quotes;

  const rows = [];
  for (const itinerary of itineraries) {
    const key = itineraryKey(itinerary);
    const flight = flightQuotes.get(key);
    if (!flight) {
      continue;
    }

    const airline = String(flight.airline || "").trim();
    if (!airline || airline.toLowerCase() === "unknown" || airline.toLowerCase() === "undefined") {
      continue;
    }

    const tripDays = Math.max(1, Math.ceil((itinerary.returnDate - itinerary.departDate) / (24 * 60 * 60 * 1000)));
    const costs = input.costsByAirport[itinerary.homeAirport] || { homeToAirportCost: 0, airportToHomeCost: 0, perDayCost: 0 };
    const homeToAirportCost = Number(costs.homeToAirportCost) || 0;
    const airportToHomeCost = Number(costs.airportToHomeCost) || 0;
    const perDayCost = Number(costs.perDayCost) || 0;

    rows.push({
      departDate: itinerary.departDate,
      returnDate: itinerary.returnDate,
      homeAirport: itinerary.homeAirport,
      outboundDestinationAirport: itinerary.outDest,
      returnDestinationAirport: itinerary.retDest,
      outboundDurationMin: flight.outboundDurationMin,
      returnDurationMin: flight.returnDurationMin,
      outboundStopText: flight.outboundStopText,
      returnStopText: flight.returnStopText,
      airline,
      tripDays,
      homeToAirportCost,
      airportToHomeCost,
      perDayCost,
      totalFlightPrice: flight.totalFlightPrice,
      totalPrice: flight.totalFlightPrice + homeToAirportCost + airportToHomeCost + perDayCost * tripDays,
      flightSource: flight.source,
    });
  }

  return {
    rows,
    diagnostics: quoteResponse.diagnostics,
  };
}

async function fetchBatchQuotes(itineraries, flightApiBase) {
  const quotes = new Map();
  const diagnostics = {
    total: itineraries.length,
    priced: 0,
    errorCounts: {},
  };

  if (!flightApiBase) {
    return { quotes, diagnostics };
  }

  const endpoint = `${flightApiBase.replace(/\/$/, "")}/api/flights/search-batch`;
  const payload = {
    itineraries: itineraries.map((i) => ({
      origin: i.homeAirport,
      destinationOutbound: i.outDest,
      destinationInbound: i.retDest,
      departDate: toIsoDate(i.departDate),
      returnDate: toIsoDate(i.returnDate),
      travelers: i.travelerCount,
    })),
  };

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      diagnostics.errorCounts.http_error = itineraries.length;
      return { quotes, diagnostics };
    }

    const body = await response.json();
    if (!Array.isArray(body.results)) {
      diagnostics.errorCounts.invalid_response = itineraries.length;
      return { quotes, diagnostics };
    }

    for (let index = 0; index < body.results.length; index += 1) {
      const item = body.results[index];
      if (!item) {
        continue;
      }

      const key =
        item.key ||
        itineraryKey({
          homeAirport: item.origin || payload.itineraries[index]?.origin,
          outDest: item.destinationOutbound || payload.itineraries[index]?.destinationOutbound,
          retDest: item.destinationInbound || payload.itineraries[index]?.destinationInbound,
          departDate: new Date(`${item.departDate || payload.itineraries[index]?.departDate}T00:00:00`),
          returnDate: new Date(`${item.returnDate || payload.itineraries[index]?.returnDate}T00:00:00`),
          travelerCount: Number(item.travelers || payload.itineraries[index]?.travelers || 1),
        });

      if (!key) {
        continue;
      }

      const totalFlightPrice = Number(item.totalFlightPrice);
      if (!Number.isFinite(totalFlightPrice) || totalFlightPrice <= 0) {
        const errCode =
          String(
            item.errorCode ||
              item.outboundErrorCode ||
              item.returnErrorCode ||
              (item.status === "error" ? "provider_error" : "unpriced")
          ).trim() || "provider_error";
        diagnostics.errorCounts[errCode] = (diagnostics.errorCounts[errCode] || 0) + 1;
        continue;
      }

      const outboundAirline = String(item.outboundAirline || "Unknown");
      const returnAirline = String(item.returnAirline || "Unknown");
      const airline = item.airline
        ? String(item.airline)
        : outboundAirline === returnAirline
          ? outboundAirline
          : `${outboundAirline} / ${returnAirline}`;

      quotes.set(key, {
        outboundDurationMin: Number(item.outboundDurationMin) || 0,
        returnDurationMin: Number(item.returnDurationMin) || 0,
        outboundStopText: String(item.outboundStopText || "Unknown"),
        returnStopText: String(item.returnStopText || "Unknown"),
        airline,
        totalFlightPrice,
        source: "live-api",
      });
      diagnostics.priced += 1;
    }
  } catch {
    diagnostics.errorCounts.network_error = itineraries.length;
    return { quotes, diagnostics };
  }

  return { quotes, diagnostics };
}

function itineraryKey(itinerary) {
  return [
    itinerary.homeAirport,
    itinerary.outDest,
    itinerary.retDest,
    toIsoDate(itinerary.departDate),
    toIsoDate(itinerary.returnDate),
    itinerary.travelerCount,
  ].join("|");
}

function pickTopThreePerAirportCombination(rows) {
  const groups = new Map();

  for (const row of rows) {
    const key = `${row.homeAirport}|${row.outboundDestinationAirport}|${row.returnDestinationAirport}`;
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(row);
  }

  const selected = [];
  for (const list of groups.values()) {
    list.sort((a, b) => a.totalPrice - b.totalPrice);
    selected.push(...list.slice(0, 3));
  }

  return selected;
}

function applyFilters() {
  const airportFilter = homeAirportFilterEl.value;
  const costColumn = costColumnFilterEl.value;
  const maxCostValue = maxCostFilterEl.value ? Number(maxCostFilterEl.value) : null;

  tableState.rows = tableState.allRows.filter((row) => {
    if (airportFilter !== "ALL" && row.homeAirport !== airportFilter) {
      return false;
    }

    if (maxCostValue !== null) {
      const costValue = row[costColumn];
      if (costValue === null || costValue > maxCostValue) {
        return false;
      }
    }

    return true;
  });

  renderRows();

  const starts = tableState.startAirports.join("/") || "-";
  const destinations = tableState.destinationAirports.join("/") || "-";
  let diagText = "";
  if (tableState.apiDiagnostics) {
    const errors = tableState.apiDiagnostics.errorCounts || {};
    const totalErrors = Object.values(errors).reduce((sum, v) => sum + Number(v || 0), 0);
    if (totalErrors > 0) {
      const topEntry = Object.entries(errors).sort((a, b) => Number(b[1]) - Number(a[1]))[0];
      if (topEntry) {
        diagText = ` Provider issues: ${totalErrors} (${topEntry[0]}=${topEntry[1]}).`;
      }
    }
  }
  summaryEl.textContent = `${tableState.rows.length} rows shown (${tableState.allRows.length} total). Starts: ${starts}. Destinations: ${destinations}.${diagText}`;
}

async function autoClearCacheIfStale() {
  const now = Date.now();
  const last = Number(localStorage.getItem(CACHE_CLEAR_KEY) || 0);
  if (last > 0 && now - last < CACHE_CLEAR_INTERVAL_MS) {
    return;
  }

  const apiBase = document.getElementById("flightApiBase").value.trim();
  if (!apiBase) {
    localStorage.setItem(CACHE_CLEAR_KEY, String(now));
    return;
  }

  try {
    const endpoint = `${apiBase.replace(/\/$/, "")}/cache/clear`;
    await fetch(endpoint, {
      method: "POST",
      headers: {
        Accept: "application/json",
      },
    });
  } catch {
    // Ignore: stale clear is best-effort and should not block UI startup.
  } finally {
    localStorage.setItem(CACHE_CLEAR_KEY, String(now));
  }
}

function clearFilters() {
  homeAirportFilterEl.value = "ALL";
  costColumnFilterEl.value = "totalPrice";
  maxCostFilterEl.value = "";
  applyFilters();
}

function exportCsv() {
  if (tableState.rows.length === 0) {
    summaryEl.textContent = "No rows available to export.";
    return;
  }

  const headers = [
    "Fly in date",
    "Start airport",
    "Fly out airport",
    "Outbound duration min",
    "Outbound itinerary",
    "Return date",
    "Return fly in airport",
    "Return duration min",
    "Return itinerary",
    "Airline",
    "Flight price",
    "Total price",
    "Source",
  ];

  const lines = [headers.join(",")];

  for (const row of tableState.rows) {
    lines.push(
      [
        toIsoDate(row.departDate),
        row.homeAirport,
        row.outboundDestinationAirport,
        row.outboundDurationMin,
        csvSafe(row.outboundStopText),
        toIsoDate(row.returnDate),
        row.returnDestinationAirport,
        row.returnDurationMin,
        csvSafe(row.returnStopText),
        csvSafe(row.airline),
        row.totalFlightPrice,
        row.totalPrice,
        row.flightSource,
      ].join(",")
    );
  }

  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `flight-scanner-${Date.now()}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function csvSafe(text) {
  const raw = String(text ?? "");
  if (raw.includes(",") || raw.includes('"')) {
    return `"${raw.replace(/"/g, '""')}"`;
  }
  return raw;
}

function savePreset() {
  const data = {
    startAirports: startAirportsEl.value,
    destinationAirports: destinationAirportsEl.value,
    departDate: document.getElementById("departDate").value,
    returnDate: document.getElementById("returnDate").value,
    travelerCount: document.getElementById("travelerCount").value,
    flightApiBase: document.getElementById("flightApiBase").value,
    costsByAirport: costState,
  };

  localStorage.setItem(PRESET_KEY, JSON.stringify(data));
  summaryEl.textContent = "Preset saved locally.";
}

function loadPreset() {
  const raw = localStorage.getItem(PRESET_KEY);
  if (!raw) {
    summaryEl.textContent = "No preset found.";
    return;
  }

  try {
    const data = JSON.parse(raw);

    const fields = [
      "startAirports",
      "destinationAirports",
      "departDate",
      "returnDate",
      "travelerCount",
      "flightApiBase",
    ];

    for (const id of fields) {
      const field = document.getElementById(id);
      if (field && data[id] !== undefined) {
        field.value = data[id];
      }
    }

    if (data.costsByAirport && typeof data.costsByAirport === "object") {
      for (const [airport, values] of Object.entries(data.costsByAirport)) {
        const legacyOneTime = Number(values.oneTimeCost) || 0;
        costState[airport] = {
          homeToAirportCost: Number(values.homeToAirportCost) || legacyOneTime,
          airportToHomeCost: Number(values.airportToHomeCost) || 0,
          perDayCost: Number(values.perDayCost) || 0,
        };
      }
    }

    renderCostMatrix();
    syncHomeAirportFilterOptions();
    summaryEl.textContent = "Preset loaded.";
  } catch {
    summaryEl.textContent = "Preset is invalid and could not be loaded.";
  }
}

function wireSorting() {
  const headers = Array.from(table.querySelectorAll("th[data-sort]"));
  headers.forEach((header) => {
    header.addEventListener("click", () => {
      const sortBy = header.dataset.sort;
      if (!sortBy) {
        return;
      }

      if (tableState.sortBy === sortBy) {
        tableState.sortDir = tableState.sortDir === "asc" ? "desc" : "asc";
      } else {
        tableState.sortBy = sortBy;
        tableState.sortDir = "asc";
      }

      renderRows();
    });
  });
}

function renderRows() {
  const rows = [...tableState.rows].sort((a, b) => compareRows(a, b, tableState.sortBy, tableState.sortDir));

  resultsBody.innerHTML = "";

  for (const row of rows) {
    const tr = document.createElement("tr");
    const sourceClass = row.flightSource === "live-api" ? "live" : "sim";

    tr.innerHTML = `
      <td>${fmtDate(row.departDate)}</td>
      <td><span class="tag">${row.homeAirport}</span></td>
      <td>${row.outboundDestinationAirport}</td>
      <td class="duration">${formatMinutes(row.outboundDurationMin)}</td>
      <td>${row.outboundStopText}</td>
      <td>${fmtDate(row.returnDate)}</td>
      <td>${row.returnDestinationAirport}</td>
      <td class="duration">${formatMinutes(row.returnDurationMin)}</td>
      <td>${row.returnStopText}</td>
      <td>${row.airline}</td>
      <td class="money">${formatMoney(row.totalFlightPrice)}</td>
      <td class="money">${formatMoney(row.totalPrice)}</td>
      <td class="${sourceClass}">${row.flightSource}</td>
    `;

    resultsBody.appendChild(tr);
  }
}

function compareRows(a, b, key, direction) {
  const left = a[key];
  const right = b[key];

  let result = 0;
  if (left === null && right === null) {
    result = 0;
  } else if (left === null) {
    result = 1;
  } else if (right === null) {
    result = -1;
  } else if (left instanceof Date && right instanceof Date) {
    result = left - right;
  } else if (typeof left === "number" && typeof right === "number") {
    result = left - right;
  } else {
    result = String(left).localeCompare(String(right));
  }

  return direction === "asc" ? result : -result;
}

function addDays(date, amount) {
  const next = new Date(date.getTime());
  next.setDate(next.getDate() + amount);
  return next;
}

function toIsoDate(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function fmtDate(date) {
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatMinutes(totalMin) {
  const hours = Math.floor(totalMin / 60);
  const mins = totalMin % 60;
  return `${hours}h ${mins}m`;
}

function formatMoney(value) {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}
