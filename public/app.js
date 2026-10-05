const form = document.querySelector("#search-form");
const identifierInput = document.querySelector("#identifiers");
const inputFile = document.querySelector("#input-file");
const inputCount = document.querySelector("#input-count");
const modeButtons = [...document.querySelectorAll("[data-mode]")];
const searchButton = document.querySelector("#search-button");
const stopButton = document.querySelector("#stop-button");
const output = document.querySelector("#output");
const downloads = document.querySelector("#downloads");
const connectionStatus = document.querySelector("#connection-status");
const aiResult = document.querySelector("#ai-result");
const enableAiButton = document.querySelector("#enable-ai");
const setupConsent = document.querySelector("#setup-consent");
const aiConsent = document.querySelector("#ai-consent");

let mode = "username";
let activeController = null;
let aiEnabled = false;
let verboseOutput = false;

function setStatus(label, state = "ready") {
  connectionStatus.className = "connection-status";
  if (state === "running") connectionStatus.classList.add("is-running");
  if (state === "error") connectionStatus.classList.add("is-error");
  connectionStatus.lastChild.textContent = ` ${label}`;
}

function writeLine(message, kind = "muted", link = null) {
  const line = document.createElement("p");
  line.className = `output-line ${kind}`;
  const prompt = document.createElement("span");
  prompt.className = "prompt";
  prompt.textContent = "$";
  const text = document.createElement("span");
  text.textContent = message;
  line.append(prompt, text);
  if (link) {
    try {
      const url = new URL(link);
      if (["http:", "https:"].includes(url.protocol)) {
        const anchor = document.createElement("a");
        anchor.href = url.href;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        anchor.textContent = "open";
        anchor.setAttribute("aria-label", `Open ${url.hostname} in a new tab`);
        line.append(anchor);
      }
    } catch {
      // Ignore links that are not valid HTTP(S) URLs.
    }
  }
  output.append(line);
  output.scrollTop = output.scrollHeight;
}

function parseIdentifiers() {
  return [...new Set(identifierInput.value.split(/[\r\n,]+/).map((value) => value.trim()).filter(Boolean))];
}

function updateInputCount() {
  const count = parseIdentifiers().length;
  inputCount.textContent = `${count} VALUE${count === 1 ? "" : "S"}`;
}

function updateMode(nextMode) {
  mode = nextMode;
  for (const button of modeButtons) {
    const active = button.dataset.mode === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  const isUsername = mode === "username";
  document.querySelector("#identifier-label").textContent = isUsername ? "USERNAME / DISPLAY NAME" : "EMAIL ADDRESS";
  identifierInput.placeholder = isUsername ? "jane.doe\njanedoe" : "jane@example.com";
  document.querySelector("#permute-option").hidden = !isUsername;
  document.querySelector("#permute-all-option").hidden = !isUsername;
  if (!isUsername) {
    document.querySelector("#permute").checked = false;
    document.querySelector("#permute-all").checked = false;
  }
}

function decodeBase64(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function finishArtifact(filename, chunks) {
  const extension = filename.split(".").pop()?.toLowerCase();
  const contentType = {
    csv: "text/csv;charset=utf-8",
    json: "application/json",
    pdf: "application/pdf",
    zip: "application/zip",
  }[extension] || "application/octet-stream";
  const blob = new Blob(chunks, { type: contentType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.className = "download-link";
  anchor.href = url;
  anchor.download = filename;
  anchor.textContent = `↓ ${filename}`;
  anchor.addEventListener("click", () => {
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }, { once: true });
  downloads.append(anchor);
}

function showAIResult(event) {
  aiResult.hidden = false;
  aiResult.replaceChildren();
  const heading = document.createElement("h3");
  heading.textContent = event.status === "complete" ? "AI SUMMARY" : "AI STATUS";
  aiResult.append(heading);
  if (event.status !== "complete") {
    const message = document.createElement("p");
    message.textContent = event.message || event.reason || "AI analysis was skipped.";
    aiResult.append(message);
    return;
  }
  const result = event.result;
  for (const [label, value] of [
    ["Summary", result.summary],
    ["Profile type", result.categorization],
  ]) {
    if (typeof value === "string" && value.trim()) {
      const paragraph = document.createElement("p");
      paragraph.textContent = `${label}: ${value}`;
      aiResult.append(paragraph);
    }
  }
  for (const [label, values] of [
    ["Insights", result.insights],
    ["Risk flags", result.risk_flags],
    ["Tags", result.tags],
  ]) {
    if (!Array.isArray(values) || !values.length) continue;
    const section = document.createElement("p");
    section.textContent = `${label}: ${values.map(String).join(" · ")}`;
    aiResult.append(section);
  }
}

function renderProgress(result) {
  if (!result || typeof result !== "object") return;
  const status = result.status;
  if (status === "NOT-FOUND" && !verboseOutput) return;
  if (status === "NONE" && !verboseOutput) return;
  const kind = status === "FOUND" ? "found" : status === "ERROR" ? "error" : "muted";
  const message = `[${status || "RESULT"}] ${result.name || "Unknown site"}${result.category ? ` · ${result.category}` : ""}`;
  writeLine(message, kind, result.url);
  if (status !== "FOUND" || !Array.isArray(result.metadata)) return;
  for (const item of result.metadata) {
    if (!item || typeof item.name !== "string") continue;
    const value = Array.isArray(item.value) ? item.value.map(String).join(", ") : String(item.value ?? "");
    writeLine(`  ${item.name}: ${value}`, "muted");
  }
}

async function consumeEvents(response) {
  if (!response.ok || !response.body) {
    let message = "The search request failed.";
    try {
      const contentType = response.headers.get("Content-Type") || "";
      if (!contentType.includes("application/json")) throw new Error();
      const body = await response.json();
      if (typeof body.error === "string") message = body.error;
    } catch {
      message = `Search API returned HTTP ${response.status}. Use vercel dev for local API testing.`;
    }
    throw new Error(message);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const artifacts = new Map();
  let buffer = "";
  let sawComplete = false;

  const processEvent = (event) => {
    switch (event.type) {
      case "start":
        writeLine(`Starting ${event.searches} ${event.mode} search${event.searches === 1 ? "" : "es"}.`, "muted");
        break;
      case "search":
        writeLine(`Search ${event.index}/${event.total}.`, "muted");
        break;
      case "progress":
        if (Number.isInteger(event.completed) && Number.isInteger(event.total)) {
          setStatus(`${event.completed}/${event.total} SITES`, "running");
        }
        renderProgress(event.result);
        break;
      case "ai":
        showAIResult(event);
        break;
      case "artifact-start":
        artifacts.set(event.filename, []);
        break;
      case "artifact-chunk": {
        const chunks = artifacts.get(event.filename);
        if (chunks) chunks.push(decodeBase64(event.data));
        break;
      }
      case "artifact-end": {
        const chunks = artifacts.get(event.filename);
        if (chunks) {
          finishArtifact(event.filename, chunks);
          artifacts.delete(event.filename);
        }
        break;
      }
      case "complete":
        sawComplete = true;
        setStatus(event.partial ? "PARTIAL" : "COMPLETE", event.partial ? "error" : "ready");
        writeLine(
          event.partial
            ? `Time budget reached · ${event.completedSearches}/${event.totalSearches} searches completed. Results above are partial.`
            : `Complete · ${event.completedSearches} search${event.completedSearches === 1 ? "" : "es"} · ${event.durationSeconds}s.`,
          event.partial ? "error" : "found",
        );
        break;
      case "error":
        sawComplete = true;
        setStatus("ERROR", "error");
        writeLine(event.message || "The search failed.", "failed");
        break;
      default:
        break;
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline = buffer.indexOf("\n");
    while (newline >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) processEvent(JSON.parse(line));
      newline = buffer.indexOf("\n");
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) processEvent(JSON.parse(buffer));

  if (!sawComplete) {
    setStatus("CONNECTION CLOSED", "error");
    writeLine("The connection closed before the search finished. Results above may be partial.", "error");
  }
}

async function handleSearch(event) {
  event.preventDefault();
  if (activeController) return;
  const identifiers = parseIdentifiers();
  if (!identifiers.length) {
    identifierInput.setCustomValidity("Enter at least one search value.");
    identifierInput.reportValidity();
    return;
  }
  identifierInput.setCustomValidity("");
  if (identifiers.length > 25) {
    writeLine("A maximum of 25 values can be searched at once.", "failed");
    setStatus("INPUT ERROR", "error");
    return;
  }
  if (aiConsent.checked && !aiEnabled) {
    writeLine("Enable AI for this browser before requesting analysis.", "failed");
    setStatus("AI NOT ENABLED", "error");
    return;
  }

  output.replaceChildren();
  downloads.replaceChildren();
  aiResult.hidden = true;
  aiResult.replaceChildren();
  verboseOutput = document.querySelector("#verbose").checked;
  activeController = new AbortController();
  searchButton.disabled = true;
  stopButton.disabled = false;
  setStatus("CONNECTING", "running");

  const request = {
    mode,
    identifiers,
    permute: document.querySelector("#permute").checked,
    permuteAll: document.querySelector("#permute-all").checked,
    excludeNsfw: document.querySelector("#exclude-nsfw").checked,
    dump: document.querySelector("#dump").checked,
    csv: document.querySelector("#export-csv").checked,
    json: document.querySelector("#export-json").checked,
    pdf: document.querySelector("#export-pdf").checked,
    verbose: verboseOutput,
    noUpdate: document.querySelector("#no-update").checked,
    aiConsent: aiConsent.checked,
    timeout: Number(document.querySelector("#timeout").value),
    concurrency: Number(document.querySelector("#concurrency").value),
    filter: document.querySelector("#filter").value.trim(),
    proxy: document.querySelector("#proxy").value.trim(),
  };

  try {
    const response = await fetch("/api/search", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal: activeController.signal,
    });
    await consumeEvents(response);
  } catch (error) {
    if (error.name === "AbortError") {
      setStatus("STOPPED", "error");
      writeLine("Search stopped. Results already received are shown above.", "error");
    } else {
      setStatus("ERROR", "error");
      writeLine(error.message || "The search could not be completed.", "failed");
    }
  } finally {
    activeController = null;
    searchButton.disabled = false;
    stopButton.disabled = true;
  }
}

async function enableAI() {
  if (!setupConsent.checked) {
    writeLine("Confirm AI key setup consent before continuing.", "error");
    document.querySelector("#setup-consent").focus();
    return;
  }
  enableAiButton.disabled = true;
  enableAiButton.textContent = "CONTACTING AI SERVICE";
  try {
    const response = await fetch("/api/ai-key", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ consent: true }),
    });
    const contentType = response.headers.get("Content-Type") || "";
    const body = contentType.includes("application/json")
      ? await response.json().catch(() => null)
      : null;
    if (!response.ok || body?.enabled !== true) {
      throw new Error(body?.error || "Could not enable AI. Use vercel dev locally and configure API_URL for AI.");
    }
    aiEnabled = true;
    document.querySelector("#ai-state").textContent = "AI key is stored in a secure browser cookie.";
    enableAiButton.textContent = "AI ENABLED";
    writeLine("AI key enabled for this browser.", "found");
  } catch (error) {
    document.querySelector("#ai-state").textContent = error.message || "AI could not be enabled.";
    enableAiButton.textContent = "ENABLE AI";
    enableAiButton.disabled = false;
    writeLine(error.message || "Could not enable AI right now.", "failed");
  }
}

for (const button of modeButtons) {
  button.addEventListener("click", () => updateMode(button.dataset.mode));
}
identifierInput.addEventListener("input", updateInputCount);
inputFile.addEventListener("change", async () => {
  const file = inputFile.files?.[0];
  if (!file) return;
  if (file.size > 1024 * 1024) {
    writeLine("Input files must be 1 MB or smaller.", "failed");
    inputFile.value = "";
    return;
  }
  try {
    identifierInput.value = await file.text();
    updateInputCount();
  } catch {
    writeLine("Could not read that text file.", "failed");
  }
});
form.addEventListener("submit", handleSearch);
identifierInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) form.requestSubmit();
});
stopButton.addEventListener("click", () => activeController?.abort());
enableAiButton.addEventListener("click", enableAI);
setupConsent.addEventListener("change", () => {
  enableAiButton.disabled = !setupConsent.checked || aiEnabled;
});

fetch("/api/ai-key", { credentials: "same-origin" })
  .then((response) => {
    if (!response.ok) throw new Error("AI status is unavailable.");
    return response.json();
  })
  .then((body) => {
    aiEnabled = body?.enabled === true;
    if (aiEnabled) {
      document.querySelector("#ai-state").textContent = "AI key is stored in a secure browser cookie.";
      enableAiButton.textContent = "AI ENABLED";
      enableAiButton.disabled = true;
    } else {
      enableAiButton.disabled = !setupConsent.checked;
    }
  })
  .catch(() => {
    document.querySelector("#ai-state").textContent = "AI status is unavailable. Run the app with vercel dev to test its API.";
    enableAiButton.disabled = !setupConsent.checked;
  });

updateInputCount();
