(() => {
  "use strict";

  const PLUGIN_ID = "openhop.nomad";
  const API = "/api/plugins/settings";

  const defaults = {
    meshcore_host: "127.0.0.1",
    meshcore_port: 5050,
    nomad_url: "http://nomad_admin:8080",
    nomad_model: "qwen2.5:3b-instruct",
    nomad_collection: null,
    nomad_timeout_seconds: 120,
    one_shot: false,
    max_concurrent_requests: 1,
    busy_wait_seconds: 5,
    max_pending_requests: 1,
    max_requests_per_sender: 2,
    max_requests_global: 4,
    rate_limit_window_seconds: 60,
    allowed_sender_prefixes: [],
    reply_chunk_delay_seconds: 2,
    max_reply_chunks: 4,
    max_chunk_bytes: 80,
    max_prompt_bytes: 1000,
    radio_prompt_enabled: true,
    radio_prompt_template: "You are a local AI assistant running through Project NOMAD, answering over MeshCore radio. Use relevant knowledge-base material supplied with the request. Only claim a specific guide, document, or file is available when that material confirms it.\nGive the direct answer first in one short paragraph, ideally under 250 characters.\nUse plain text only: no Markdown, bold, italics, headings, tables, or numbered lists.\nOmit introductions, repeated questions, and filler. For procedures, give only the essential steps in short sentences.\nPrefer common words and simple punctuation. Do not sacrifice accuracy or essential safety details to shorten the answer.\nDo not invent names, sources, URLs, or access instructions. If unsure, say so briefly or ask one short clarifying question.\n\nUser question:\n{question}",
    duplicate_ttl_seconds: 600,
    log_level: "INFO"
  };

  const fieldIds = [
    "meshcore_host", "meshcore_port", "nomad_url", "nomad_model", "nomad_collection",
    "nomad_timeout_seconds", "one_shot", "max_concurrent_requests", "busy_wait_seconds",
    "max_reply_chunks", "max_chunk_bytes", "max_prompt_bytes", "radio_prompt_enabled",
    "radio_prompt_template",
    "duplicate_ttl_seconds", "log_level", "max_pending_requests", "max_requests_per_sender", "max_requests_global", "rate_limit_window_seconds", "allowed_sender_prefixes", "reply_chunk_delay_seconds"
  ];

  const $ = (id) => document.getElementById(id);

  let currentConfig = { ...defaults };
  let formDirty = false;

  function setNotice(message, kind = "") {
    const notice = $("notice");
    notice.textContent = message || "";
    notice.className = `notice ${kind}`.trim();
  }

  function setStatus(label, kind = "neutral") {
    const status = $("global-status");
    status.className = `status-pill ${kind}`;
    status.innerHTML = `<span class="status-dot"></span>${label}`;
  }

  function repeaterJwt() {
    try {
      return window.localStorage.getItem("pymc_jwt_token") || "";
    } catch (_) {
      return "";
    }
  }

  async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    const token = repeaterJwt();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(url, { ...options, headers });
    if (response.status === 401) {
      throw new Error(
        token
          ? "Your openHop dashboard session has expired. Log in again and reopen NOMAD Bridge."
          : "Authentication is required. Open NOMAD Bridge from the logged-in openHop dashboard."
      );
    }
    return response;
  }

  let companionBusy = false;
  let companionReady = false;
  let companionEndpoint = null;
  let advertBusy = false;
  function advertButtons(disabled) {
    $("advert-zero").disabled = disabled;
    $("advert-flood").disabled = disabled;
  }
  async function advertRuntime() {
    const response = await apiFetch(`/api/plugins/runtime?id=${encodeURIComponent(PLUGIN_ID)}`);
    if (!response.ok) throw new Error("Advert controls unavailable: running plugin runtime API is required.");
    const runtime = (await response.json()).runtime;
    const state = runtime?.advert;
    if (!state || !state.connected || !Number.isFinite(state.updated_at) || typeof state.token !== "string" || !/^[a-f0-9]{64}$/.test(state.token) || Math.abs(Date.now() / 1000 - state.updated_at) > (state.result?.status === "pending" ? 20 : 5)) {
      throw new Error("Advert controls unavailable: plugin stopped, disconnected, or runtime stale.");
    }
    return { ...state, otherPending: runtime.companion?.result?.status === "pending" };
  }
  async function refreshAdverts() {
    if (advertBusy) return;
    try {
      const state = await advertRuntime();
      if (advertBusy) return;
      advertButtons(companionBusy || state.otherPending || state.result?.status === "pending");
      if (!$("advert-status").dataset.result) $("advert-status").textContent = `Running Companion: ${state.endpoint}`;
    } catch (error) {
      if (advertBusy) return;
      advertButtons(true);
      if (!$("advert-status").dataset.result) $("advert-status").textContent = error.message;
    }
  }
  async function sendAdvert(mode) {
    if (advertBusy || companionBusy) return;
    advertBusy = true;
    advertButtons(true);
    $("advert-status").dataset.result = "true";
    $("advert-status").textContent = "Submitting manual advert…";
    try {
      const config = await fetchConfig();
      const state = await advertRuntime();
      if (state.otherPending || state.result?.status === "pending") throw new Error("Another Companion action is pending; wait for its result.");
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
      const response = await apiFetch(API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: PLUGIN_ID, restart: false,
          config: { ...stripRuntime(config), advert_request: { id, mode, token: state.token } } })
      });
      if (!response.ok) throw new Error("Advert submission not confirmed. Do not automatically retry.");
      $("advert-status").textContent = "Waiting for Companion acceptance…";
      const deadline = Date.now() + 20000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 500));
        const result = (await advertRuntime()).result;
        if (result?.id !== id || result.status === "pending") continue;
        $("advert-status").textContent = result.status === "accepted"
          ? `${mode} advert accepted by Companion. RF delivery is not confirmed.`
          : `Advert ${result.status}. RF delivery is not confirmed; do not automatically retry.`;
        return;
      }
      throw new Error("Advert acceptance unknown or request expired. Do not automatically retry.");
    } catch (error) {
      $("advert-status").textContent = `${error.message} RF delivery is not confirmed.`;
    } finally {
      advertBusy = false;
      await refreshAdverts();
    }
  }
  $("advert-zero").addEventListener("click", () => sendAdvert("zero-hop"));
  $("advert-flood").addEventListener("click", () => sendAdvert("flood"));
  setInterval(refreshAdverts, 2000);
  refreshAdverts();

  function companionButtons(disabled) {
    $("companion-read").disabled = disabled;
    $("companion-apply").disabled = disabled || !companionReady;
    ["companion-auto-add", "companion-overwrite", "companion-path-bytes"].forEach(id => {
      $(id).disabled = disabled || !companionReady;
    });
  }
  async function companionRuntime() {
    const response = await apiFetch(`/api/plugins/runtime?id=${encodeURIComponent(PLUGIN_ID)}`);
    if (!response.ok) throw new Error("Companion controls unavailable: running plugin runtime API is required.");
    const runtime = (await response.json()).runtime;
    const state = runtime?.companion;
    if (!state || !state.connected || !Number.isFinite(state.updated_at) || typeof state.token !== "string" || !/^[a-f0-9]{64}$/.test(state.token) || Math.abs(Date.now() / 1000 - state.updated_at) > (state.result?.status === "pending" ? 20 : 5)) {
      throw new Error("Companion controls unavailable: stopped, disconnected, or runtime stale.");
    }
    return { ...state, otherPending: runtime.advert?.result?.status === "pending" };
  }
  async function refreshCompanion() {
    if (companionBusy) return;
    try {
      const state = await companionRuntime();
      if (companionBusy) return;
      if (companionEndpoint !== state.endpoint || !state.result) companionReady = false;
      companionButtons(advertBusy || state.otherPending || state.result?.status === "pending");
      if (!$("companion-status").dataset.result) $("companion-status").textContent = `Running Companion: ${state.endpoint}. Read current settings first.`;
    } catch (error) {
      if (companionBusy) return;
      companionReady = false;
      companionButtons(true);
      if (!$("companion-status").dataset.result) $("companion-status").textContent = error.message;
    }
  }
  function populateCompanion(values) {
    if (!values || !["all", "none", "selected"].includes(values.auto_add) || typeof values.overwrite_oldest !== "boolean" || ![1, 2, 3].includes(values.path_hash_bytes)) {
      throw new Error("Companion returned incomplete settings; read again.");
    }
    $("companion-auto-add").options[0].textContent = values.auto_add === "selected" ? "Selected (keep unchanged)" : "Keep current mode";
    $("companion-auto-add").value = values.auto_add === "selected" ? "" : values.auto_add;
    $("companion-overwrite").checked = values.overwrite_oldest;
    $("companion-path-bytes").value = String(values.path_hash_bytes);
  }
  async function companionAction(apply) {
    if (companionBusy || advertBusy || (apply && !companionReady)) return;
    const patch = {};
    if (apply) {
      const auto = $("companion-auto-add").value;
      if (auto) patch.auto_add = auto;
      patch.overwrite_oldest = $("companion-overwrite").checked;
      patch.path_hash_bytes = Number($("companion-path-bytes").value);
    }
    companionBusy = true;
    companionButtons(true);
    advertButtons(true);
    $("companion-status").dataset.result = "true";
    $("companion-status").textContent = "Submitting Companion request…";
    try {
      const config = await fetchConfig(); // preserve fresh saved fields, never unsaved form values
      const state = await companionRuntime();
      if (state.otherPending || state.result?.status === "pending") throw new Error("Another Companion action is pending; wait for its result.");
      if (apply && (companionEndpoint !== state.endpoint || !state.result)) throw new Error("Companion changed; read settings again first.");
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
      const response = await apiFetch(API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: PLUGIN_ID, restart: false,
          config: { ...stripRuntime(config), companion_request: { id, token: state.token, patch } } })
      });
      if (!response.ok) throw new Error("Submission not confirmed. Do not automatically retry.");
      companionReady = false;
      $("companion-status").textContent = "Waiting for Companion read-back…";
      const deadline = Date.now() + 20000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 500));
        const current = await companionRuntime();
        const result = current.result;
        if (result?.id !== id || result.status === "pending") continue;
        if (result.status !== "verified") throw new Error(`Companion ${result.status}. Changes may be partial; read again before retrying.`);
        populateCompanion(result.values);
        companionEndpoint = current.endpoint;
        companionReady = true;
        $("companion-status").textContent = `Settings read back from Companion: ${current.endpoint}${apply ? "; requested changes verified." : "."}`;
        return;
      }
      throw new Error("Companion outcome unknown or request expired. Read again; do not automatically retry.");
    } catch (error) {
      companionReady = false;
      $("companion-status").textContent = error.message;
    } finally {
      companionBusy = false;
      await refreshCompanion();
      await refreshAdverts();
    }
  }
  $("companion-read").addEventListener("click", () => companionAction(false));
  $("companion-apply").addEventListener("click", () => companionAction(true));
  setInterval(refreshCompanion, 2000);
  refreshCompanion();

  function configFromResponse(payload) {
    if (payload && typeof payload === "object" && payload.config && typeof payload.config === "object") {
      return payload.config;
    }
    return payload && typeof payload === "object" ? payload : {};
  }

  function stripRuntime(config) {
    const clean = { ...config };
    delete clean._runtime;
    delete clean.advert_request;
    delete clean.companion_request;
    delete clean.contacts_request;
    return clean;
  }

  function supportedConfig(config) {
    const clean = stripRuntime(config);
    // Match runtime defaults for legacy configs without rewriting anything on load.
    if (!("max_pending_requests" in clean)) {
      clean.max_pending_requests = Math.max(1, Number(clean.max_concurrent_requests ?? defaults.max_concurrent_requests));
    }
    if (!("max_requests_global" in clean)) {
      clean.max_requests_global = Math.max(4, Number(clean.max_requests_per_sender ?? defaults.max_requests_per_sender));
    }
    return clean;
  }

  async function fetchConfig() {
    const response = await apiFetch(`${API}?id=${encodeURIComponent(PLUGIN_ID)}`);
    if (!response.ok) throw new Error(`NOMAD plugin data load failed (HTTP ${response.status})`);
    return configFromResponse(await response.json());
  }

  async function postConfig(config, { restart = true } = {}) {
    const response = await apiFetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: PLUGIN_ID, config: supportedConfig(config), restart })
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Save failed (HTTP ${response.status})${text ? `: ${text}` : ""}`);
    }
    currentConfig = { ...defaults, ...supportedConfig(config) };
    return response;
  }

  function setValue(id, value) {
    const el = $(id);
    if (!el) return;
    if (id === "allowed_sender_prefixes") el.value = Array.isArray(value) ? value.join("\n") : (value || "");
    else if (el.type === "checkbox") el.checked = Boolean(value);
    else el.value = value === null || value === undefined ? "" : String(value);
  }

  function populateSettings(config) {
    const cfg = { ...defaults, ...supportedConfig(config) };
    fieldIds.forEach((id) => setValue(id, cfg[id]));
    formDirty = false;
    renderOverview(cfg);
  }

  function valueNumber(id) {
    const raw = $(id).value.trim();
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error(`${id.replaceAll("_", " ")} must be a number.`);
    return value;
  }

  function validateUrl(rawUrl) {
    const url = String(rawUrl || "").trim();
    if (!url) throw new Error("NOMAD URL is required.");
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_) {
      throw new Error("NOMAD URL must be a valid http:// or https:// URL.");
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error("NOMAD URL must use http:// or https://.");
    }
    return parsed.toString().replace(/\/$/, "");
  }

  function buildConfig() {
    const meshcorePort = valueNumber("meshcore_port");
    const timeout = valueNumber("nomad_timeout_seconds");
    const maxConcurrent = valueNumber("max_concurrent_requests");
    const busyWait = valueNumber("busy_wait_seconds");
    const maxReplyChunks = valueNumber("max_reply_chunks");
    const maxChunkBytes = valueNumber("max_chunk_bytes");
    const maxPromptBytes = valueNumber("max_prompt_bytes");
    const duplicateTtl = valueNumber("duplicate_ttl_seconds");

    if (meshcorePort < 1 || meshcorePort > 65535) throw new Error("Companion port must be between 1 and 65535.");
    if (timeout < 1 || timeout > 600) throw new Error("Timeout must be between 1 and 600 seconds.");
    if (maxConcurrent < 1 || maxConcurrent > 32) throw new Error("Max concurrent requests must be between 1 and 32.");
    if (busyWait < 0 || busyWait > 120) throw new Error("Busy wait must be between 0 and 120 seconds.");
    if (maxReplyChunks < 1 || maxReplyChunks > 32) throw new Error("Max reply chunks must be between 1 and 32.");
    if (maxChunkBytes < 40 || maxChunkBytes > 1024) throw new Error("Max chunk bytes must be between 40 and 1024.");
    if (maxPromptBytes < 128 || maxPromptBytes > 8192) throw new Error("Max prompt bytes must be between 128 and 8192.");
    if (duplicateTtl < 60 || duplicateTtl > 86400) throw new Error("Duplicate TTL must be between 60 and 86400 seconds.");

    const meshcoreHost = $("meshcore_host").value.trim();
    const nomadModel = $("nomad_model").value.trim();
    const radioPromptTemplate = $("radio_prompt_template").value;
    if (!meshcoreHost) throw new Error("Companion host is required.");
    if (!nomadModel) throw new Error("NOMAD model is required.");
    if (!radioPromptTemplate.trim()) throw new Error("Radio prompt template is required.");
    if (!radioPromptTemplate.includes("{question}")) {
      throw new Error("Radio prompt template must include {question}.");
    }

    const maxPending = valueNumber("max_pending_requests");
    const perSender = valueNumber("max_requests_per_sender");
    const globalLimit = valueNumber("max_requests_global");
    const windowSeconds = valueNumber("rate_limit_window_seconds");
    const delay = valueNumber("reply_chunk_delay_seconds");
    if (!Number.isInteger(maxPending) || maxPending < maxConcurrent) throw new Error("Max pending requests must be an integer at least as large as max concurrent requests.");
    if (!Number.isInteger(perSender) || !Number.isInteger(globalLimit) || perSender < 1 || globalLimit < perSender) throw new Error("Request limits must be positive integers; global must be at least per sender.");
    if (windowSeconds <= 0 || delay < 0 || delay > 60) throw new Error("Rate window must be positive and reply delay must be between 0 and 60 seconds.");
    const prefixes = $("allowed_sender_prefixes").value.split(/[\n,]/).map(v => v.trim()).filter(Boolean);
    if (prefixes.some(v => !/^[0-9a-fA-F]{12}$/.test(v))) throw new Error("Each sender prefix must be exactly 12 hexadecimal characters.");
    const remainingTemplate = radioPromptTemplate.replaceAll("{{", "").replaceAll("}}", "").replaceAll("{question}", "");
    if (/[{}]/.test(remainingTemplate)) throw new Error("Only {question} substitution and doubled literal braces are allowed.");
    const collectionRaw = $("nomad_collection").value.trim();

    return {
      ...supportedConfig(stripRuntime(currentConfig)),
      meshcore_host: meshcoreHost,
      meshcore_port: meshcorePort,
      nomad_url: validateUrl($("nomad_url").value),
      nomad_model: nomadModel,
      nomad_collection: collectionRaw ? collectionRaw : null,
      nomad_timeout_seconds: timeout,
      one_shot: $("one_shot").checked,
      max_pending_requests: maxPending,
      max_requests_per_sender: perSender,
      max_requests_global: globalLimit,
      rate_limit_window_seconds: windowSeconds,
      allowed_sender_prefixes: prefixes,
      reply_chunk_delay_seconds: delay,
      max_concurrent_requests: maxConcurrent,
      busy_wait_seconds: busyWait,
      max_reply_chunks: maxReplyChunks,
      max_chunk_bytes: maxChunkBytes,
      max_prompt_bytes: maxPromptBytes,
      radio_prompt_enabled: $("radio_prompt_enabled").checked,
      radio_prompt_template: radioPromptTemplate,
      duplicate_ttl_seconds: duplicateTtl,
      log_level: $("log_level").value
    };
  }

  function renderOverview(config) {
    const companionHost = config.meshcore_host || defaults.meshcore_host;
    const companionPort = config.meshcore_port || defaults.meshcore_port;
    $("overview-meshcore").textContent = `${companionHost}:${companionPort}`;
    $("overview-nomad").textContent = config.nomad_url || defaults.nomad_url;
    $("overview-model").textContent = config.nomad_model || defaults.nomad_model;
  }

  async function loadConfig(populate = false) {
    try {
      setStatus("Loading", "neutral");
      const config = await fetchConfig();
      currentConfig = { ...defaults, ...supportedConfig(stripRuntime(config)) };
      if (populate || !formDirty) populateSettings(currentConfig);
      setStatus("Ready", "good");
      if ($("notice").classList.contains("error")) setNotice("");
    } catch (error) {
      setStatus("Error", "bad");
      setNotice(error instanceof Error ? error.message : String(error), "error");
    }
  }

  fieldIds.forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", () => {
      formDirty = true;
      setStatus("Unsaved changes", "warn");
    });
    el.addEventListener("change", () => {
      formDirty = true;
      setStatus("Unsaved changes", "warn");
    });
  });

  $("reload-settings").addEventListener("click", () => {
    formDirty = false;
    loadConfig(true);
    setNotice("Reloaded configuration from plugin settings.", "ok");
  });

  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const config = buildConfig();
      setStatus("Saving", "neutral");
      setNotice("Saving settings and restarting the NOMAD Bridge plugin...");
      await postConfig(config, { restart: true });
      formDirty = false;
      setStatus("Saved", "good");
      setNotice("Saved. The NOMAD Bridge plugin is restarting with the new configuration.", "ok");
      setTimeout(() => loadConfig(true).catch(() => {}), 1500);
      setTimeout(() => loadConfig(true).catch(() => {}), 3500);
    } catch (error) {
      setStatus("Error", "bad");
      setNotice(error instanceof Error ? error.message : String(error), "error");
    }
  });

  const helpButtons = [...document.querySelectorAll(".help-button")];
  function closeHelp() {
    helpButtons.forEach(button => {
      button.setAttribute("aria-expanded", "false");
      $(button.getAttribute("aria-controls")).hidden = true;
    });
  }
  helpButtons.forEach(button => button.addEventListener("click", () => {
    const open = button.getAttribute("aria-expanded") !== "true";
    closeHelp();
    button.setAttribute("aria-expanded", String(open));
    $(button.getAttribute("aria-controls")).hidden = !open;
  }));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeHelp();
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".help-button, .field-help")) closeHelp();
  });
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  function selectTab(tab, focus = false) {
    closeHelp();
    tabs.forEach(item => {
      const active = item === tab;
      item.setAttribute("aria-selected", String(active));
      item.tabIndex = active ? 0 : -1;
      $(item.getAttribute("aria-controls")).hidden = !active;
    });
    if (focus) tab.focus();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(tab));
    tab.addEventListener("keydown", event => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      selectTab(tabs[next], true);
    });
  });
  // Native validation must reveal an invalid field on a hidden tab.
  $("settings-form").addEventListener("invalid", event => {
    const panel = event.target.closest('[role="tabpanel"]');
    if (panel) selectTab($(panel.getAttribute("aria-labelledby")));
  }, true);

  // ── Contacts address book ──
  let contactsBusy = false;
  let contactsFavorites = new Set();
  let contactsRaw = [];
  let contactsTotal = 0;
  let contactsSearch = "";
  let contactsFilter = "all";
  let contactsTypeFilter = "all";

  // openhop_core.companion.constants ADV_TYPE_* values (not advert payload flags).
  const ADV_TYPES = { 0: "Unknown", 1: "Companion", 2: "Repeater", 3: "Room Server", 4: "Sensor" };
  function advTypeName(t) { return ADV_TYPES[t] ?? `Type ${t}`; }

  function formatTimestamp(ts) {
    if (!ts) return "never";
    const d = new Date(ts * 1000);
    const now = Date.now();
    const diff = now - d.getTime();
    if (diff < 60000) return "just now";
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function pathLabel(encoded) {
    if (encoded < 0 || encoded === 255) return "unknown path";
    // Bits 6–7 encode hash width; bits 0–5 encode hop count.
    const hops = encoded & 0x3f;
    const width = (encoded >> 6) + 1;
    if (width > 3 || hops * width > 64) return "invalid path";
    if (hops === 0) return "direct";
    return `${hops} hop${hops > 1 ? "s" : ""}`;
  }

  async function contactsRuntime() {
    const response = await apiFetch(`/api/plugins/runtime?id=${encodeURIComponent(PLUGIN_ID)}`);
    if (!response.ok) throw new Error("Contacts unavailable: running plugin runtime API is required.");
    const runtime = (await response.json()).runtime;
    const state = runtime?.contacts;
    if (!state || !state.connected || !Number.isFinite(state.updated_at) || typeof state.token !== "string" || !/^[a-f0-9]{64}$/.test(state.token) || Math.abs(Date.now() / 1000 - state.updated_at) > (state.result?.status === "pending" ? 20 : 5)) {
      throw new Error("Contacts unavailable: plugin stopped, disconnected, or runtime stale.");
    }
    // Sync favorites from runtime
    if (Array.isArray(state.favorites)) {
      contactsFavorites = new Set(state.favorites);
    }
    return { ...state, otherPending: runtime.advert?.result?.status === "pending" || runtime.companion?.result?.status === "pending" };
  }

  async function refreshContactsButton() {
    if (contactsBusy) return;
    try {
      const state = await contactsRuntime();
      if (contactsBusy) return;
      $("contacts-refresh").disabled = state.otherPending || state.result?.status === "pending";
      if (!$("contacts-status").dataset.result) $("contacts-status").textContent = `Running Companion: ${state.endpoint}. Click Refresh to load contacts.`;
    } catch (error) {
      if (contactsBusy) return;
      $("contacts-refresh").disabled = true;
      if (!$("contacts-status").dataset.result) $("contacts-status").textContent = error.message;
    }
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function applyFilters(contacts) {
    let filtered = [...contacts];

    // Search by name or public key prefix
    if (contactsSearch) {
      const q = contactsSearch.toLowerCase();
      filtered = filtered.filter(c =>
        (c.name && c.name.toLowerCase().includes(q)) ||
        c.public_key.toLowerCase().includes(q)
      );
    }

    // Favorites filter
    if (contactsFilter === "favorites") {
      filtered = filtered.filter(c => contactsFavorites.has(c.public_key));
    }

    // Type filter
    if (contactsTypeFilter !== "all") {
      const t = Number(contactsTypeFilter);
      filtered = filtered.filter(c => c.adv_type === t);
    }

    return filtered;
  }

  function renderContacts(contacts, total) {
    contactsRaw = contacts || [];
    contactsTotal = total || 0;

    const filtered = applyFilters(contactsRaw);
    const list = $("contacts-list");
    list.innerHTML = "";

    if (filtered.length === 0) {
      if (contactsRaw.length === 0) {
        list.innerHTML = '<div class="contacts-empty">No contacts found on this Companion.</div>';
      } else {
        list.innerHTML = '<div class="contacts-empty">No contacts match your current filters.</div>';
      }
      $("contacts-count").textContent = "";
      return;
    }

    // Sort: favorites first, then by name, then by last_advert descending
    const sorted = [...filtered].sort((a, b) => {
      const af = contactsFavorites.has(a.public_key) ? 0 : 1;
      const bf = contactsFavorites.has(b.public_key) ? 0 : 1;
      if (af !== bf) return af - bf;
      if (a.name && !b.name) return -1;
      if (!a.name && b.name) return 1;
      if (a.name && b.name) { const c = a.name.localeCompare(b.name); if (c !== 0) return c; }
      return (b.last_advert || 0) - (a.last_advert || 0);
    });

    for (const c of sorted) {
      const isFav = contactsFavorites.has(c.public_key);
      const row = document.createElement("div");
      row.className = `contact-row${isFav ? " favorite" : ""}`;
      const prefix = c.public_key.substring(0, 12);
      row.innerHTML = `
        <div class="contact-info">
          <div class="contact-name">${c.name ? escapeHtml(c.name) : ""}</div>
          <div class="contact-meta">
            <span>${prefix}</span>
            <span class="contact-type-badge">${advTypeName(c.adv_type)}</span>
            <span>${pathLabel(c.out_path_len)}</span>
            <span>last advert: ${formatTimestamp(c.last_advert)}</span>
          </div>
        </div>
        <div class="contact-actions">
          <button type="button" class="fav-btn${isFav ? " fav-active" : ""}" data-key="${c.public_key}" title="${isFav ? "Remove from favorites" : "Add to favorites"}">${isFav ? "★" : "☆"}</button>
          <button type="button" class="remove-btn" data-key="${c.public_key}" title="Remove contact">Remove</button>
        </div>`;
      list.appendChild(row);
    }

    const showCount = filtered.length;
    const totalCount = contactsTotal;
    const filterNote = contactsSearch || contactsFilter !== "all" || contactsTypeFilter !== "all" ? ` (showing ${showCount} of ${totalCount})` : "";
    $("contacts-count").textContent = `${showCount} contact${showCount !== 1 ? "s" : ""}${filterNote} (${totalCount} total capacity used)`;

    // Bind buttons
    list.querySelectorAll(".fav-btn").forEach(btn => btn.addEventListener("click", () => toggleFavorite(btn.dataset.key)));
    list.querySelectorAll(".remove-btn").forEach(btn => btn.addEventListener("click", () => removeContact(btn.dataset.key)));
  }

  async function contactsAction(action, extra = {}) {
    if (contactsBusy) return;
    contactsBusy = true;
    $("contacts-refresh").disabled = true;
    $("contacts-status").dataset.result = "true";
    $("contacts-status").textContent = action === "list" ? "Loading contacts…" : `${action} in progress…`;
    try {
      const config = await fetchConfig();
      const state = await contactsRuntime();
      if (state.otherPending || state.result?.status === "pending") throw new Error("Another action is pending; wait for its result.");
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
      const response = await apiFetch(API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: PLUGIN_ID, restart: false,
          config: { ...stripRuntime(config), contacts_request: { id, action, token: state.token, ...extra } } })
      });
      if (!response.ok) throw new Error("Request not confirmed. Do not automatically retry.");

      if (action === "favorite" || action === "unfavorite") {
        // These are instant (no Companion command) — poll until runtime reflects the change
        const isFav = action === "favorite";
        const pubkey = extra.public_key;
        let synced = false;
        for (let attempt = 0; attempt < 10; attempt++) {
          await new Promise(resolve => setTimeout(resolve, 500));
          try {
            const state = await contactsRuntime();
            if (state.result?.id !== id || state.result.status !== "ok") {
              if (state.result?.id === id && state.result.status !== "pending") {
                throw new Error(`Favorite change failed: ${state.result.status}.`);
              }
              continue;
            }
            if (!Array.isArray(state.favorites)) continue;
            if (state.favorites.includes(pubkey) === isFav) {
              synced = true;
              break;
            }
          } catch (error) {
            if (error.message.startsWith("Favorite change failed:")) throw error;
            // Runtime temporarily unavailable; keep polling.
          }
        }
        if (!synced) throw new Error("Favorite change not confirmed. Refresh contacts before retrying.");
        $("contacts-status").textContent = isFav ? "Added to favorites." : "Removed from favorites.";
        // Re-render with existing contacts and updated favorites — no network call needed
        renderContacts(contactsRaw, contactsTotal);
        return;
      }

      $("contacts-status").textContent = "Waiting for Companion response…";
      const deadline = Date.now() + 25000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 600));
        const current = await contactsRuntime();
        const result = current.result;
        if (result?.id !== id || result.status === "pending") continue;

        if (action === "list") {
          if (result.status === "ok" && Array.isArray(result.contacts)) {
            renderContacts(result.contacts, result.total || 0);
            $("contacts-status").textContent = `Contacts loaded from Companion: ${current.endpoint}.`;
          } else {
            $("contacts-status").textContent = `Failed to load contacts: ${result.status}.`;
          }
        } else if (action === "remove") {
          if (result.status === "ok") {
            $("contacts-status").textContent = "Contact removed. Refreshing list…";
            await refreshContactsList();
            if (result.favorite_cleanup === "error") {
              $("contacts-status").textContent =
                "Contact removed, but favorites could not be saved. Check storage before retrying.";
            }
          } else {
            $("contacts-status").textContent = `Remove failed: ${result.status}.`;
          }
        }
        return;
      }
      throw new Error("Response unknown or request expired. Do not automatically retry.");
    } catch (error) {
      $("contacts-status").textContent = error.message;
    } finally {
      contactsBusy = false;
      await refreshContactsButton();
    }
  }

  async function refreshContactsList() {
    // Re-fetch contacts and re-render with current filters
    try {
      const config = await fetchConfig();
      const state = await contactsRuntime();
      if (state.result?.contacts && Array.isArray(state.result.contacts)) {
        renderContacts(state.result.contacts, state.result.total || 0);
      } else {
        // Need to fetch fresh data
        const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
        const response = await apiFetch(API, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: PLUGIN_ID, restart: false,
            config: { ...stripRuntime(config), contacts_request: { id, action: "list", token: state.token } } })
        });
        if (!response.ok) return;
        // Wait for the result to appear in runtime
        const deadline = Date.now() + 25000;
        while (Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 600));
          const current = await contactsRuntime();
          if (current.result?.id === id && current.result.status === "ok" && Array.isArray(current.result.contacts)) {
            renderContacts(current.result.contacts, current.result.total || 0);
            $("contacts-status").textContent = `Contacts refreshed from Companion: ${current.endpoint}.`;
            return;
          }
        }
      }
    } catch (_) {
      // Silently fail — refreshContactsButton will show the error
    }
  }

  async function toggleFavorite(pubkey) {
    const isFav = contactsFavorites.has(pubkey);
    await contactsAction(isFav ? "unfavorite" : "favorite", { public_key: pubkey });
  }

  async function removeContact(pubkey) {
    if (!confirm("Remove this contact from the Companion? This cannot be undone.")) return;
    await contactsAction("remove", { public_key: pubkey });
  }

  // Search input — live filter, persists across refreshes
  $("contacts-search").addEventListener("input", (e) => {
    contactsSearch = e.target.value.trim();
    renderContacts(contactsRaw, contactsTotal);
  });

  // Favorites filter — persists across refreshes
  $("contacts-filter").addEventListener("change", (e) => {
    contactsFilter = e.target.value;
    renderContacts(contactsRaw, contactsTotal);
  });

  // Type filter — persists across refreshes
  $("contacts-type-filter").addEventListener("change", (e) => {
    contactsTypeFilter = e.target.value;
    renderContacts(contactsRaw, contactsTotal);
  });

  $("contacts-refresh").addEventListener("click", () => contactsAction("list"));
  setInterval(refreshContactsButton, 3000);
  refreshContactsButton();

  loadConfig(true);
})();
