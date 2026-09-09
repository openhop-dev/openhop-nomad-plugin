(() => {
  "use strict";

  const PLUGIN_ID = "openhop.nomad";
  const API = "/api/plugins/settings";

  const defaults = {
    meshcore_host: "127.0.0.1",
    meshcore_port: 5001,
    nomad_url: "http://127.0.0.1:8080",
    nomad_model: "qwen2.5:3b-instruct",
    nomad_collection: null,
    nomad_timeout_seconds: 120,
    one_shot: true,
    max_concurrent_requests: 2,
    busy_wait_seconds: 5,
    max_reply_chunks: 4,
    max_chunk_bytes: 145,
    max_prompt_bytes: 1000,
    radio_prompt_enabled: true,
    radio_prompt_template:
      "You are answering a question received over a low-bandwidth MeshCore radio network.\n"
      + "Give the most useful answer first.\n"
      + "Be concise.\n"
      + "Use plain text.\n"
      + "Do not use Markdown tables.\n"
      + "Avoid unnecessary introductions.\n"
      + "Aim for fewer than 400 characters when practical.\n\n"
      + "User question:\n{question}",
    duplicate_ttl_seconds: 600,
    log_level: "INFO"
  };

  const fieldIds = [
    "meshcore_host", "meshcore_port", "nomad_url", "nomad_model", "nomad_collection",
    "nomad_timeout_seconds", "one_shot", "max_concurrent_requests", "busy_wait_seconds",
    "max_reply_chunks", "max_chunk_bytes", "max_prompt_bytes", "radio_prompt_enabled",
    "radio_prompt_template",
    "duplicate_ttl_seconds", "log_level"
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

  function configFromResponse(payload) {
    if (payload && typeof payload === "object" && payload.config && typeof payload.config === "object") {
      return payload.config;
    }
    return payload && typeof payload === "object" ? payload : {};
  }

  function stripRuntime(config) {
    const clean = { ...config };
    delete clean._runtime;
    return clean;
  }

  function supportedConfig(config) {
    const out = {};
    Object.keys(defaults).forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(config, key)) out[key] = config[key];
    });
    return out;
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
    if (el.type === "checkbox") el.checked = Boolean(value);
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
    if (maxChunkBytes < 64 || maxChunkBytes > 1024) throw new Error("Max chunk bytes must be between 64 and 1024.");
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

  loadConfig(true);
})();
