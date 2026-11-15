/* The web interface of an exported Nodestro workflow: sign-in, the workflow's form, live progress, approvals,
   results and files, the run history and (for the operator of the computer) the settings.

   Everything goes through the application's own API on the same origin. The browser never holds an API key after
   the sign-in: the session is an HttpOnly cookie, and every changing request carries the X-Nodestro-UI header that
   the server checks against cross-site requests. Results are rendered as DOM text, never as HTML. */
"use strict";

(function () {
  const LANGUAGE_NAMES = { de: "Deutsch", en: "English", es: "Español", fr: "Français", "pt-BR": "Português (Brasil)", it: "Italiano" };
  const FINAL = new Set(["succeeded", "failed", "cancelled"]);
  const state = {
    boot: null,
    config: null,
    language: "en",
    strings: {},
    fallback: {},
    run: null,
    events: null,
    poll: null,
    live: "",
    files: {},
    previews: [],
    view: "run",
    output: null,
    manifest: null,
    fields: [],
    freeInput: true,
  };
  const Connection = window.NodestroConnection;
  const Standalone = window.NodestroStandalone;
  //: The interface on its own origin (NAL-C5): a list of backends and the token profile instead of a session cookie.
  const mode = document.querySelector('meta[name="nodestro-mode"]');
  const standalone = Boolean(mode && mode.getAttribute("content") === "standalone" && Standalone);
  let retryTimer = null;
  //: The token session with the current backend (standalone only); the access token lives only in its memory.
  let tokens = null;
  const conn = new Connection.BackendConnection({ onChange: renderConnection, headers: () => (tokens ? tokens.headers() : {}) });
  const backends = standalone ? new Standalone.Backends(localStore()) : null;

  function localStore() {
    try { return window.localStorage; } catch (error) { return null; }
  }

  const $ = (id) => document.getElementById(id);
  const store = {
    get(key) { try { return window.localStorage.getItem("nodestro." + key); } catch (error) { return null; } },
    set(key, value) { try { window.localStorage.setItem("nodestro." + key, value); } catch (error) { /* private mode */ } },
    remove(key) { try { window.localStorage.removeItem("nodestro." + key); } catch (error) { /* private mode */ } },
  };

  // ---- translations ---------------------------------------------------------------------------------------

  function t(key, params) {
    let text = state.strings[key];
    if (text === undefined) text = state.fallback[key];
    if (text === undefined) return key;
    if (params) {
      text = text.replace(/\{(\w+)\}/g, (whole, name) => (params[name] === undefined ? whole : String(params[name])));
    }
    return text;
  }

  async function loadStrings(language) {
    const read = async (code) => {
      const response = await fetch("ui/assets/i18n/" + code + ".json", { credentials: "same-origin" });
      if (!response.ok) throw new Error("i18n " + code);
      return response.json();
    };
    if (!Object.keys(state.fallback).length) state.fallback = await read("en");
    state.strings = language === "en" ? state.fallback : await read(language).catch(() => state.fallback);
    state.language = language;
    document.documentElement.lang = language;
    applyStrings();
  }

  function applyStrings() {
    for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
    for (const node of document.querySelectorAll("[data-i18n-aria]")) node.setAttribute("aria-label", t(node.dataset.i18nAria));
    for (const node of document.querySelectorAll("[data-i18n-title]")) node.title = t(node.dataset.i18nTitle);
    renderConnection(conn.state);
  }

  function formatDate(value) {
    if (!value) return "";
    try {
      return new Intl.DateTimeFormat(state.language, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
    } catch (error) {
      return String(value);
    }
  }

  function formatBytes(bytes) {
    if (bytes === undefined || bytes === null) return "";
    const units = ["B", "KB", "MB", "GB"];
    let size = Number(bytes);
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    const number = new Intl.NumberFormat(state.language, { maximumFractionDigits: unit ? 1 : 0 }).format(size);
    return number + " " + units[unit];
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return "";
    if (seconds < 60) return t("time.seconds", { n: Math.max(0, Math.round(seconds)) });
    const minutes = Math.floor(seconds / 60);
    return t("time.minutes", { m: minutes, s: Math.round(seconds - minutes * 60) });
  }

  // ---- connection ---------------------------------------------------------------------------------------------

  async function loadManifest() {
    try {
      const response = await fetch("ui/assets/manifest.json", { credentials: "same-origin" });
      if (response.ok) state.manifest = await response.json();
    } catch (error) { /* shown as unknown */ }
  }

  /** Discovery, contract check and the app's form: the connection is "connected" only when all of them worked. */
  async function connect() {
    try {
      await conn.discover();
    } catch (problem) {
      if (problem.kind === "incompatible") { showIncompatible(problem.detail.backend || []); return false; }
      if (problem.kind === "auth") { showSignIn(); return false; }
      if (problem.kind === "network") { connectionLost(); return false; }
      conn.set("error", problem.detail);
      return false;
    }
    state.fields = state.config.fields || [];
    if (!conn.legacy) {
      try {
        const info = await api("GET", state.config.api_base);
        const fields = info && info.form_version === 1 ? Connection.fieldsFromForm(info.form) : null;
        if (fields) state.fields = fields;
      } catch (problem) {
        // 401: the sign-in shows; no network: the connection retries; anything else: the form of /ui/api/config.
        if (problem.status === 401 || !(problem instanceof ApiProblem)) return false;
      }
    }
    state.freeInput = !state.fields.length;
    conn.set(conn.can("events.sse") ? "connected" : "degraded");
    return true;
  }

  function showIncompatible(versions) {
    closeEvents();
    conn.set("incompatible", { backend: versions });
    for (const id of ["nav-run", "nav-history", "nav-setup"]) $(id).hidden = true;
    show("loading");
    $("view-loading").querySelector(".spinner").hidden = true;
    $("view-loading").querySelector("p").textContent = incompatibleText();
  }

  function incompatibleText() {
    const backend = (conn.detail.backend || []).join(", ") || "?";
    return t("conn.incompatible", { app: Connection.CONTRACT.max, backend });
  }

  function connectionLost() {
    if (conn.state === "disconnected" || conn.state === "incompatible") return;
    conn.set("disconnected");
    scheduleRetry();
  }

  function scheduleRetry() {
    clearTimeout(retryTimer);
    const wait = Connection.backoff(conn.attempts);
    conn.attempts += 1;
    conn.detail = { retry: Math.round(wait / 1000) };
    renderConnection(conn.state);
    retryTimer = setTimeout(reconnect, wait);
  }

  async function reconnect() {
    clearTimeout(retryTimer);
    if (conn.state !== "disconnected" && conn.state !== "error") return;
    if (standalone) { await reconnectStandalone(); return; }
    if (!state.boot) { conn.set("connecting"); start(); return; }
    conn.set("connecting");
    let boot;
    try {
      boot = await api("GET", "/ui/api/bootstrap");
    } catch (problem) {
      if (conn.state === "connecting") conn.set("error", { status: problem.status });
      return;
    }
    state.boot.tls = boot.tls;
    state.boot.pairing = boot.pairing;
    if (!boot.session) {
      state.boot.session = null;
      if (!(await renewDevice())) { showSignIn(); return; }
    }
    if (!state.config) { await enter(); return; }
    if (!(await connect())) return;
    if (state.run && !FINAL.has(state.run.status)) { closeEvents(); follow(state.run.run_id); }
    else if (state.run) await refreshStatus(state.run.run_id);
  }

  function statusText(name) {
    switch (name) {
      case "connected": {
        const backend = conn.describe().name || (state.boot && state.boot.name) || "Nodestro";
        const session = (state.boot && state.boot.session) || {};
        if (session.device) return t("conn.connected_device", { name: backend, device: session.name || "" });
        return t("conn.connected", { name: backend });
      }
      case "degraded": return t("conn.degraded");
      case "auth_required": return t("conn.auth_required");
      case "connecting": return t("conn.connecting");
      case "disconnected": return t("conn.disconnected");
      case "incompatible": return t("conn.incompatible_short");
      default: return t("conn.error");
    }
  }

  function renderConnection(name) {
    const status = $("status");
    if (!status) return;
    status.dataset.state = name;
    const label = statusText(name);
    $("status-text").textContent = label;
    status.setAttribute("aria-label", t("about.open") + ": " + label);
    const banner = $("connection-banner");
    const shown = name === "disconnected" || name === "error" || name === "incompatible";
    banner.hidden = !shown;
    banner.dataset.state = name;
    if (shown) {
      let text = t("conn.error_long");
      if (name === "incompatible") text = incompatibleText();
      if (name === "disconnected") {
        const running = state.run && !FINAL.has(state.run.status);
        text = t(running ? "conn.lost_running" : "conn.lost");
        if (conn.detail.retry) text += " " + t("conn.retry_in", { n: conn.detail.retry });
      }
      $("connection-text").textContent = text;
      $("connection-retry").hidden = name === "incompatible";
    }
    if ($("about-dialog").open) renderAbout();
  }

  function renderAbout() {
    const info = conn.describe();
    const manifest = state.manifest || {};
    const unknown = t("about.unknown");
    const rows = [
      ["about.ui_version", manifest.version || unknown],
      ["about.contract", info.legacy ? t("about.contract_none") : info.contract ? "v" + info.contract : unknown],
      ["about.backend_name", info.name || unknown],
      ["about.backend_id", info.id || unknown],
      ["about.backend_version", info.version || unknown],
      ["about.runtime_version", info.runtimeVersion || unknown],
      ["about.release", state.config && state.config.version ? "v" + state.config.version : unknown],
      ["about.state", statusText(conn.state)],
      ...(standalone ? [["about.backend_address", conn.base || unknown]] : []),
      ["about.tls", tlsText()],
    ];
    const list = $("about-list");
    list.textContent = "";
    for (const [key, value] of rows) {
      const term = document.createElement("dt");
      term.textContent = t(key);
      const detail = document.createElement("dd");
      detail.textContent = String(value);
      list.append(term, detail);
    }
  }

  /** How the backend serves TLS (NAL-C4), as the about dialog names it. */
  function tlsText() {
    const tls = (state.boot && state.boot.tls) || {};
    const mode = tls.mode || (secureBackend() ? "certificate" : "none");
    const text = t("about.tls_" + mode);
    return mode === "self_signed" && tls.fingerprint ? text + " (" + tls.fingerprint + ")" : text;
  }

  /** The permanent "restricted trust" notice of a self-issued certificate (NAL-C4, decision E-7). */
  function renderTlsNotice() {
    const tls = (state.boot && state.boot.tls) || {};
    const shown = tls.mode === "self_signed" && secureBackend();
    $("tls-notice").hidden = !shown;
    if (shown) $("tls-fingerprint").textContent = tls.fingerprint || t("about.unknown");
  }

  /** The backend is reached over HTTPS (embedded: the page's own address; standalone: the backend's). */
  function secureBackend() {
    return standalone ? conn.base.startsWith("https:") : location.protocol === "https:";
  }

  function setupAbout() {
    const dialog = $("about-dialog");
    $("status").addEventListener("click", () => {
      renderAbout();
      dialog.showModal();
      $("about-close").focus();
    });
    $("about-close").addEventListener("click", () => dialog.close());
    $("connection-retry").addEventListener("click", () => {
      if (conn.state === "connecting") return;
      reconnect();
    });
  }

  // ---- HTTP ---------------------------------------------------------------------------------------------------

  class ApiProblem extends Error {
    constructor(status, body) {
      const error = (body && body.error) || {};
      super(error.message || "HTTP " + status);
      this.status = status;
      this.code = error.code || "http." + status;
      this.params = error.params || {};
      this.details = error;
    }
  }

  async function api(method, path, body, options) {
    const init = { method, credentials: standalone ? "omit" : "same-origin", headers: { Accept: "application/json" } };
    if (standalone) Object.assign(init.headers, tokens ? tokens.headers() : {});
    else if (method !== "GET") init.headers["X-Nodestro-UI"] = "1";
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    let response;
    try {
      response = await fetch(conn.url(path), init);
    } catch (error) {
      connectionLost();
      throw error;
    }
    if (standalone && response.status === 401 && tokens && tokens.token && !(options && options.retried)) {
      // The access token expired or the backend restarted: one renewal from the refresh cookie, then once again.
      let renewed = false;
      try { await tokens.refresh(); renewed = true; } catch (problem) { /* signs in again below */ }
      if (renewed) return api(method, path, body, { ...(options || {}), retried: true });
    }
    if (options && options.raw) {
      if (!response.ok) throw new ApiProblem(response.status, await response.json().catch(() => null));
      return response;
    }
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const signIn = ["/ui/api/session", "/ui/api/bootstrap", "/ui/api/pair", "/ui/api/device/session"];
      if (response.status === 401 && !signIn.includes(path)) showSignIn();
      throw new ApiProblem(response.status, data);
    }
    return data;
  }

  function problemText(problem) {
    const key = "error." + problem.code;
    const known = t(key, problem.params);
    if (known !== key) return known;
    if (problem.status === 0 || problem instanceof TypeError) return t("error.network");
    return problem.message || t("error.unknown");
  }

  let noticeTimer = null;
  function notice(text) {
    const box = $("notice");
    box.textContent = text;
    box.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { box.hidden = true; }, 5000);
  }

  // ---- views --------------------------------------------------------------------------------------------------

  function show(view) {
    state.view = view;
    for (const id of ["loading", "signin", "run", "history", "setup", "backends"]) $("view-" + id).hidden = id !== view;
    for (const button of document.querySelectorAll(".navlink")) {
      if (button.dataset.view === view) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    if (view === "history") loadHistory();
    if (view === "setup") loadSetup();
    if (view === "backends") renderBackends();
  }

  function showSignIn() {
    closeEvents();
    if (conn.state === "incompatible") conn.set("connecting");
    conn.set("auth_required");
    $("signin-title").textContent = (state.boot && state.boot.name) || "Nodestro";
    $("signin-description").textContent = (state.boot && state.boot.description) || "";
    for (const id of ["nav-run", "nav-history", "nav-setup", "sign-out", "remove-device"]) $(id).hidden = true;
    renderPairing();
    show("signin");
    const first = standalone ? (state.pairing && state.pairing.code ? "pair-name" : "pair-code") : state.pairing && state.boot && state.boot.pairing ? "pair-name" : "signin-key";
    setTimeout(() => $(first).focus(), 0);
  }

  async function start() {
    if (standalone) { await startStandalone(); return; }
    setupTheme();
    loadManifest();
    // First of all, so the code leaves the address even when the backend cannot be reached.
    readPairingLink();
    conn.set("connecting");
    try {
      state.boot = await api("GET", "/ui/api/bootstrap");
    } catch (problem) {
      await loadStrings("en").catch(() => {});
      $("view-loading").querySelector("p").textContent = t("error.network");
      if (conn.state === "connecting") conn.set("error");
      return;
    }
    const chosen = store.get("language");
    const language = chosen && LANGUAGE_NAMES[chosen] ? chosen : state.boot.language || "en";
    await loadStrings(language).catch(() => loadStrings("en"));
    setupLanguages();
    document.title = state.boot.name + " · Nodestro";
    $("app-name").textContent = state.boot.name;
    $("app-version").textContent = state.boot.version ? "v" + state.boot.version : "";
    renderTlsNotice();
    if (!state.boot.session && !(await renewDevice())) { showSignIn(); return; }
    await enter();
  }

  // ---- pairing (NAL-C4) ---------------------------------------------------------------------------------------

  /** A pairing link (``/?pair#code=…&fp=…``): the code fills the field and leaves the address (and the history). */
  function readPairingLink() {
    const params = new URLSearchParams(location.hash.slice(1));
    const code = params.get("code");
    if (!code) return;
    state.pairing = { code, fingerprint: (params.get("fp") || "").replace(/[^0-9a-f]/gi, "").toUpperCase() };
    history.replaceState(null, "", location.pathname);
    if (standalone && params.get("backend")) {
      // A link of the launcher (pair --ui). A backend already in the list becomes the current one; a new one is never
      // added by a link alone (anyone can send a link): the person confirms its full address first.
      const address = Standalone.normalizeAddress(params.get("backend"));
      const known = address && backends.list().find((item) => item.address === address);
      if (known) backends.choose(known.id);
      else if (address) state.pairing.newBackend = address;
      else state.pairing.badBackend = true;
    }
  }

  /** The fingerprint of the link is not the one the backend reports (or it reports none): a wrong link or another
   *  certificate. This does not stop an active man in the middle, who serves this script too: only the browser's
   *  certificate dialog does (docs/pairing.md in nodestro-api). */
  function fingerprintMismatch() {
    const expected = (state.pairing && state.pairing.fingerprint) || (standalone && state.backend && state.backend.fingerprint);
    const tls = (state.boot && state.boot.tls) || {};
    if (!expected) return false;
    return !tls.fingerprint || expected !== tls.fingerprint.replace(/:/g, "").toUpperCase();
  }

  /** A paired browser gets a new session from its device cookie (the backend restarted or the session ended). */
  async function renewDevice() {
    if (standalone) return false;
    if (!state.boot || !state.boot.pairing || store.get("paired") !== "1") return false;
    // A replaced secret revokes the device, so tabs of this browser renew one after the other, and a tab that waited
    // first looks for the session another tab already got (their cookies are shared).
    const renew = async () => {
      const boot = await api("GET", "/ui/api/bootstrap").catch(() => null);
      if (boot && boot.session) { state.boot.session = boot.session; return true; }
      try {
        const answer = await api("POST", "/ui/api/device/session", {});
        state.boot.session = { scopes: answer.device.scopes, operator: false, name: answer.device.name, device: true };
        return true;
      } catch (problem) {
        if (problem.status === 401) store.remove("paired");
        return false;
      }
    };
    return navigator.locks ? navigator.locks.request("nodestro-device-renewal", renew) : renew();
  }

  function renderPairing() {
    const form = $("pair-form");
    const on = Boolean(state.boot && state.boot.pairing);
    form.hidden = !on;
    if (!on) return;
    const error = $("pair-error");
    if (state.pairing && state.pairing.code && !$("pair-code").value) $("pair-code").value = state.pairing.code;
    if (fingerprintMismatch()) {
      error.textContent = t("tls.mismatch");
      error.hidden = false;
      form.querySelector("button[type=submit]").disabled = true;
    }
  }

  async function pair(event) {
    event.preventDefault();
    const error = $("pair-error");
    if (fingerprintMismatch()) return;
    error.hidden = true;
    const code = $("pair-code").value.trim();
    if (!code) { error.textContent = t("pair.code_missing"); error.hidden = false; return; }
    if (standalone) { await pairStandalone(code); return; }
    try {
      const answer = await api("POST", "/ui/api/pair", { code, name: $("pair-name").value.trim() });
      $("pair-code").value = "";
      state.pairing = null;
      store.set("paired", "1");
      state.boot.session = { scopes: answer.device.scopes, operator: false, name: answer.device.name, device: true };
      await enter();
    } catch (problem) {
      error.textContent = problemText(problem);
      error.hidden = false;
    }
  }

  async function removeDevice() {
    if (!window.confirm(t("device.remove_confirm"))) return;
    closeEvents();
    if (standalone) { try { await tokens.revoke(); } catch (problem) { /* already gone */ } }
    else { try { await api("DELETE", "/ui/api/device"); } catch (problem) { /* already gone */ } }
    store.remove("paired");
    state.boot.session = null;
    state.run = null;
    notice(t("device.removed"));
    showSignIn();
  }

  async function enter() {
    if (conn.state !== "connecting") conn.set("connecting");
    if (standalone) {
      // The configuration came with the access token (the same object as /ui/api/config, never operator data).
      if (!state.config) { showSignIn(); return; }
    } else {
      try {
        state.config = await api("GET", "/ui/api/config");
      } catch (problem) {
        if (problem.status !== 401) notice(problemText(problem));
        return;
      }
    }
    if (!(await connect())) return;
    const session = state.boot.session || {};
    const scopes = session.scopes || [];
    $("nav-run").hidden = false;
    $("nav-history").hidden = !scopes.includes("read");
    $("nav-setup").hidden = !session.operator;
    // A paired browser signs in again by itself from its device cookie: it ends its access by removing the device.
    $("sign-out").hidden = Boolean(session.device);
    $("remove-device").hidden = !session.device;
    $("workflow-name").textContent = state.config.name;
    $("workflow-description").textContent = state.config.description || "";
    $("start").disabled = !scopes.includes("run");
    renderProviderBanner();
    renderForm();
    const remembered = new URLSearchParams(location.hash.slice(1)).get("run");
    show("run");
    if (remembered) openRun(remembered);
  }

  function setupLanguages() {
    const select = $("language");
    select.textContent = "";
    for (const code of state.boot.languages || Object.keys(LANGUAGE_NAMES)) {
      const option = document.createElement("option");
      option.value = code;
      option.textContent = LANGUAGE_NAMES[code] || code;
      option.lang = code;
      select.appendChild(option);
    }
    select.value = state.language;
    select.onchange = async () => {
      store.set("language", select.value);
      await loadStrings(select.value);
      if (state.config) { renderProviderBanner(); renderForm(true); if (state.run) renderRun(state.run); }
      if (state.view === "history") loadHistory();
      if (state.view === "setup") loadSetup();
    };
  }

  function setupTheme() {
    const order = ["auto", "light", "dark"];
    const apply = (value) => {
      document.documentElement.dataset.theme = value;
      $("theme").setAttribute("aria-label", t("a11y.theme") + ": " + t("theme." + value));
      $("theme").title = t("theme." + value);
    };
    apply(store.get("theme") || "auto");
    $("theme").onclick = () => {
      const next = order[(order.indexOf(document.documentElement.dataset.theme) + 1) % order.length];
      store.set("theme", next);
      apply(next);
    };
  }

  function renderProviderBanner() {
    const missing = (state.config.providers || []).filter((item) => item.required && !item.configured);
    const banner = $("provider-banner");
    banner.hidden = !missing.length;
    if (missing.length) {
      const names = missing.map((item) => item.label).join(", ");
      banner.textContent = t((state.boot.session || {}).operator ? "providers.missing_operator" : "providers.missing", { providers: names });
    }
  }

  // ---- form ---------------------------------------------------------------------------------------------------

  function renderForm(keepValues) {
    const previous = keepValues ? collectRaw() : null;
    const container = $("fields");
    container.textContent = "";
    state.files = {};
    const fields = state.fields;
    if (state.freeInput) {
      container.appendChild(textField({ name: "input", label: t("run.free_input"), type: "textarea", required: false, default: state.config.default_input }));
      container.appendChild(fileField({ name: "files", label: t("run.documents"), type: "file", multiple: true, accept_extensions: [], max_files: 20, max_size_mb: state.config.max_upload_mb }));
    }
    for (const field of fields) {
      container.appendChild(field.type === "file" ? fileField(field) : textField(field));
    }
    if (previous) {
      for (const [name, value] of Object.entries(previous)) {
        const input = document.getElementById("field-" + name);
        if (!input) continue;
        if (input.type === "checkbox") input.checked = value; else input.value = value;
      }
    }
  }

  function fieldShell(field, inputId) {
    const wrap = document.createElement("div");
    wrap.className = "field";
    wrap.dataset.name = field.name;
    if (field.type !== "boolean") {
      const label = document.createElement(field.type === "file" ? "span" : "label");
      label.className = "label";
      if (field.type !== "file") label.htmlFor = inputId;
      else label.id = inputId + "-label";
      label.textContent = field.label || field.name;
      if (field.required) {
        const star = document.createElement("span");
        star.className = "required";
        star.textContent = "*";
        star.setAttribute("aria-hidden", "true");
        label.appendChild(star);
      }
      wrap.appendChild(label);
    }
    if (field.description) {
      const description = document.createElement("p");
      description.className = "description";
      description.id = inputId + "-description";
      description.textContent = field.description;
      wrap.appendChild(description);
    }
    return wrap;
  }

  function textField(field) {
    const id = "field-" + field.name;
    const wrap = fieldShell(field, id);
    let input;
    if (field.type === "textarea") {
      input = document.createElement("textarea");
      input.maxLength = field.name === "input" ? 200000 : 20000;
    } else if (field.type === "select") {
      input = document.createElement("select");
      if (!field.required || field.default === null || field.default === undefined) {
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = t("form.choose");
        input.appendChild(empty);
      }
      for (const option of field.options || []) {
        if (!String(option).trim()) continue;
        const item = document.createElement("option");
        item.value = option;
        item.textContent = option;
        input.appendChild(item);
      }
    } else if (field.type === "boolean") {
      const label = document.createElement("label");
      label.className = "switch";
      input = document.createElement("input");
      input.type = "checkbox";
      input.id = id;
      input.name = field.name;
      input.checked = field.default === true;
      label.appendChild(input);
      const text = document.createElement("span");
      text.textContent = field.label || field.name;
      label.appendChild(text);
      wrap.insertBefore(label, wrap.firstChild);
      if (field.description) input.setAttribute("aria-describedby", id + "-description");
      return wrap;
    } else {
      input = document.createElement("input");
      input.type = field.type === "number" ? "number" : field.type === "date" ? "date" : "text";
      if (field.type === "number") input.step = "any";
      if (field.type === "text") input.maxLength = 20000;
    }
    input.id = id;
    input.name = field.name;
    if (field.required) { input.required = true; input.setAttribute("aria-required", "true"); }
    if (field.default !== null && field.default !== undefined && field.type !== "boolean") input.value = field.default;
    if (field.description) input.setAttribute("aria-describedby", id + "-description");
    wrap.appendChild(input);
    return wrap;
  }

  function fileField(field) {
    const id = "field-" + field.name;
    const wrap = fieldShell(field, id);
    state.files[field.name] = [];
    const drop = document.createElement("label");
    drop.className = "drop";
    drop.htmlFor = id;
    const input = document.createElement("input");
    input.type = "file";
    input.id = id;
    input.name = field.name;
    input.multiple = field.multiple !== false;
    // The contract sends extensions with their dot (".pdf"); older backends sent them without.
    const extensions = (field.accept_extensions || []).map((ext) => "." + String(ext).replace(/^\./, ""));
    if (extensions.length) input.accept = extensions.join(",");
    input.setAttribute("aria-labelledby", id + "-label");
    if (field.description) input.setAttribute("aria-describedby", id + "-description");
    const text = document.createElement("p");
    const limits = [];
    // Groups by name (text, images ...), single formats by their extension; the full list is in the title.
    const kinds = (field.accept || []).map((item) => {
      const key = "accept." + String(item).toLowerCase().replace(/^\./, "");
      const named = t(key);
      return named === key ? String(item).replace(/^\./, "").toUpperCase() : named;
    });
    limits.push(kinds.length ? kinds.join(", ") : t("accept.any"));
    if (extensions.length) drop.title = extensions.join(" ");
    if (field.max_size_mb) limits.push(t("form.max_size", { size: field.max_size_mb + " MB" }));
    if (field.max_files && input.multiple) limits.push(t("form.max_files", { n: field.max_files }));
    text.textContent = t("form.drop") + (limits.length ? " · " + limits.join(" · ") : "");
    drop.appendChild(input);
    drop.appendChild(text);
    wrap.appendChild(drop);
    const list = document.createElement("ul");
    list.className = "chosen";
    list.setAttribute("aria-live", "polite");
    wrap.appendChild(list);
    const add = (chosen) => {
      const problems = [];
      for (const file of chosen) {
        const extension = (file.name.split(".").pop() || "").toLowerCase();
        if (extensions.length && !extensions.includes(extension)) { problems.push(t("form.type_refused", { name: file.name })); continue; }
        if (field.max_size_mb && file.size > field.max_size_mb * 1024 * 1024) { problems.push(t("form.too_large", { name: file.name, size: field.max_size_mb + " MB" })); continue; }
        if (!input.multiple) state.files[field.name] = [];
        if (field.max_files && state.files[field.name].length >= field.max_files) { problems.push(t("form.too_many", { n: field.max_files })); break; }
        state.files[field.name].push(file);
      }
      drawChosen(field.name, list);
      setFieldError(wrap, problems.join(" "));
    };
    input.onchange = () => { add(Array.from(input.files || [])); input.value = ""; };
    drop.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (event) => { event.preventDefault(); drop.classList.remove("over"); add(Array.from(event.dataTransfer.files || [])); });
    return wrap;
  }

  function drawChosen(name, list) {
    for (const url of state.previews) URL.revokeObjectURL(url);
    state.previews = [];
    list.textContent = "";
    state.files[name].forEach((file, index) => {
      const item = document.createElement("li");
      if (/^image\/(png|jpeg|gif|webp)$/.test(file.type)) {
        const image = document.createElement("img");
        const url = URL.createObjectURL(file);
        state.previews.push(url);
        image.src = url;
        image.alt = "";
        item.appendChild(image);
      }
      const label = document.createElement("span");
      label.className = "name";
      label.textContent = file.name;
      const size = document.createElement("span");
      size.className = "size";
      size.textContent = formatBytes(file.size);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", t("form.remove", { name: file.name }));
      remove.onclick = () => { state.files[name].splice(index, 1); drawChosen(name, list); };
      item.append(label, size, remove);
      list.appendChild(item);
    });
  }

  function setFieldError(wrap, message) {
    let error = wrap.querySelector(".field-error");
    const input = wrap.querySelector("input, select, textarea");
    if (!message) {
      if (error) error.remove();
      if (input) input.removeAttribute("aria-invalid");
      return;
    }
    if (!error) {
      error = document.createElement("p");
      error.className = "field-error";
      error.id = "error-" + wrap.dataset.name;
      wrap.appendChild(error);
    }
    error.textContent = message;
    if (input) {
      input.setAttribute("aria-invalid", "true");
      const described = (input.getAttribute("aria-describedby") || "").split(" ").filter(Boolean);
      if (!described.includes(error.id)) described.push(error.id);
      input.setAttribute("aria-describedby", described.join(" "));
    }
  }

  function collectRaw() {
    const values = {};
    for (const input of $("fields").querySelectorAll("input:not([type=file]), select, textarea")) {
      values[input.name] = input.type === "checkbox" ? input.checked : input.value;
    }
    return values;
  }

  function validate() {
    let first = null;
    const fields = state.freeInput ? [] : state.fields;
    const values = {};
    for (const field of fields) {
      const wrap = $("fields").querySelector('.field[data-name="' + CSS.escape(field.name) + '"]');
      let problem = "";
      if (field.type === "file") {
        if (field.required && !state.files[field.name].length) problem = t("form.required");
      } else {
        const input = document.getElementById("field-" + field.name);
        if (field.type === "boolean") values[field.name] = input.checked;
        else {
          const raw = input.value.trim();
          if (!raw) { if (field.required) problem = t("form.required"); }
          else if (field.type === "number") {
            const number = Number(raw);
            if (!Number.isFinite(number)) problem = t("form.number");
            else values[field.name] = number;
          } else values[field.name] = field.type === "textarea" ? input.value : raw;
        }
      }
      setFieldError(wrap, problem);
      if (problem && !first) first = wrap.querySelector("input, select, textarea");
    }
    if (first) first.focus();
    return first ? null : values;
  }

  async function submit(event) {
    event.preventDefault();
    $("form-error").hidden = true;
    const values = validate();
    if (values === null) return;
    const body = new FormData();
    if (state.freeInput) {
      body.append("input", $("field-input").value);
      for (const file of state.files.files || []) body.append("files", file, file.name);
    } else {
      body.append("values", JSON.stringify(values));
      for (const [name, files] of Object.entries(state.files)) for (const file of files) body.append(name, file, file.name);
    }
    const button = $("start");
    button.disabled = true;
    button.textContent = t("run.starting");
    try {
      const answer = await api("POST", state.config.api_base + "/runs", body);
      await openRun(answer.run_id);
      $("run-panel").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    } catch (problem) {
      const box = $("form-error");
      box.textContent = problemText(problem);
      box.hidden = false;
      for (const item of (problem.details && problem.details.params && problem.details.params.errors) || []) {
        const wrap = $("fields").querySelector('.field[data-name="' + CSS.escape(String(item.field || "")) + '"]');
        if (wrap) setFieldError(wrap, item.message || "");
      }
    } finally {
      button.disabled = false;
      button.textContent = t("run.start");
    }
  }

  // ---- runs ---------------------------------------------------------------------------------------------------

  function closeEvents() {
    if (state.events) { state.events.close(); state.events = null; }
    clearTimeout(state.poll);
    state.poll = null;
  }

  async function openRun(runId) {
    closeEvents();
    state.live = "";
    history.replaceState(null, "", "#run=" + encodeURIComponent(runId));
    show("run");
    let run;
    try {
      run = await api("GET", state.config.api_base + "/runs/" + encodeURIComponent(runId));
    } catch (problem) {
      notice(problemText(problem));
      return;
    }
    state.run = run;
    renderRun(run);
    if (FINAL.has(run.status)) await loadResult(run);
    else follow(run.run_id);
  }

  function follow(runId) {
    const url = conn.url(state.config.api_base) + "/runs/" + encodeURIComponent(runId) + "/events?text=1";
    // EventSource cannot send a token: the standalone interface reads the stream with fetch.
    const Source = standalone ? Standalone.TokenEventSource : EventSource;
    const source = standalone
      ? new Source(url, { fetch: window.fetch.bind(window), headers: () => (tokens ? tokens.headers() : {}) })
      : new EventSource(url);
    state.events = source;
    let failures = 0;
    source.addEventListener("status", (event) => {
      failures = 0;
      if (conn.state === "degraded" && conn.detail.live === false) conn.set("connected");
      state.run = JSON.parse(event.data);
      renderRun(state.run);
    });
    source.addEventListener("text", (event) => {
      const data = JSON.parse(event.data);
      state.live += data.delta || "";
      if (state.live.length > 200000) state.live = state.live.slice(-200000);
      $("live").hidden = false;
      $("live-text").textContent = state.live;
    });
    const refresh = () => refreshStatus(runId);
    for (const kind of ["node_started", "node_finished", "approval_requested", "approval_decided", "run_paused", "run_cancelling", "loop_iteration"]) {
      source.addEventListener(kind, refresh);
    }
    source.addEventListener("end", async () => {
      closeEvents();
      await refreshStatus(runId);
      await loadResult(state.run);
    });
    source.onerror = () => {
      failures += 1;
      // The first error: one status request tells a lost connection (banner, retries) from a stream that reconnects.
      if (failures === 1) refreshStatus(runId);
      if (source.readyState === Source.CLOSED || failures > 5) {
        // The browser gave up reconnecting (e.g. the session ended): poll instead.
        closeEvents();
        if (conn.state === "connected") conn.set("degraded", { live: false });
        poll(runId);
      }
    };
  }

  function poll(runId) {
    state.poll = setTimeout(async () => {
      const run = await refreshStatus(runId);
      if (run && FINAL.has(run.status)) await loadResult(run);
      else if (run) poll(runId);
    }, 3000);
  }

  let refreshing = null;
  async function refreshStatus(runId) {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        const run = await api("GET", state.config.api_base + "/runs/" + encodeURIComponent(runId));
        if (state.run && state.run.run_id !== run.run_id) return run;
        state.run = run;
        renderRun(run);
        return run;
      } catch (problem) {
        return null;
      } finally {
        setTimeout(() => { refreshing = null; }, 0);
      }
    })();
    return refreshing;
  }

  function renderRun(run) {
    $("run-empty").hidden = true;
    $("run-body").hidden = false;
    const badge = $("run-status");
    badge.hidden = false;
    badge.className = "badge " + run.status;
    badge.textContent = t("status." + run.status);
    const meta = [formatDate(run.created_at)];
    if (run.duration_seconds !== null && run.duration_seconds !== undefined) meta.push(formatDuration(run.duration_seconds));
    const usage = run.usage || {};
    if (usage.total_tokens) meta.push(t("run.tokens", { n: new Intl.NumberFormat(state.language).format(usage.total_tokens) }));
    if (usage.estimated_cost_usd) meta.push("≈ $" + Number(usage.estimated_cost_usd).toFixed(4));
    $("run-meta").textContent = meta.filter(Boolean).join(" · ");
    const steps = $("steps");
    steps.textContent = "";
    for (const step of run.steps || []) {
      const item = $("tpl-step").content.firstElementChild.cloneNode(true);
      item.classList.add(step.status);
      item.querySelector(".label").textContent = step.label;
      item.querySelector(".state").textContent = t("step." + step.status);
      steps.appendChild(item);
    }
    renderApprovals(run);
    const error = $("run-error");
    error.hidden = !run.error;
    if (run.error) error.textContent = problemText(new ApiProblem(0, { error: run.error }));
    const scopes = (state.boot.session || {}).scopes || [];
    $("cancel").hidden = FINAL.has(run.status) || !scopes.includes("run");
    $("retry").hidden = !(run.status === "failed" || run.status === "cancelled") || !scopes.includes("run");
    if (!FINAL.has(run.status)) { $("output").textContent = ""; $("files").textContent = ""; $("copy").hidden = true; }
  }

  function renderApprovals(run) {
    const container = $("approvals");
    container.textContent = "";
    const scopes = (state.boot.session || {}).scopes || [];
    for (const approval of run.approvals || []) {
      const box = document.createElement("section");
      box.className = "approval";
      box.setAttribute("aria-label", t("approval.title"));
      const title = document.createElement("h3");
      title.textContent = t("approval.title") + ": " + approval.step;
      box.appendChild(title);
      if (approval.instructions) {
        box.appendChild(window.NodestroMarkdown.render(approval.instructions));
      }
      if ((approval.review || []).length) {
        const list = document.createElement("dl");
        for (const item of approval.review) {
          const term = document.createElement("dt");
          term.textContent = item.label;
          const value = document.createElement("dd");
          value.textContent = typeof item.value === "string" ? item.value : JSON.stringify(item.value, null, 2);
          list.append(term, value);
        }
        box.appendChild(list);
      }
      if (!scopes.includes("approve")) {
        const note = document.createElement("p");
        note.className = "hint";
        note.textContent = t("approval.no_scope");
        box.appendChild(note);
        container.appendChild(box);
        continue;
      }
      const commentId = "comment-" + approval.approval_id;
      let comment = null;
      if (approval.allow_comment !== false) {
        const label = document.createElement("label");
        label.htmlFor = commentId;
        label.textContent = approval.require_comment_on_reject ? t("approval.comment_required_reject") : t("approval.comment");
        comment = document.createElement("textarea");
        comment.id = commentId;
        comment.maxLength = 4000;
        comment.rows = 3;
        box.append(label, comment);
      }
      const error = document.createElement("p");
      error.className = "error";
      error.setAttribute("role", "alert");
      error.hidden = true;
      const actions = document.createElement("div");
      actions.className = "actions";
      const decide = async (decision) => {
        const text = comment ? comment.value.trim() : "";
        if (decision === "rejected" && approval.require_comment_on_reject && !text) {
          error.textContent = t("approval.comment_missing");
          error.hidden = false;
          if (comment) comment.focus();
          return;
        }
        for (const button of actions.querySelectorAll("button")) button.disabled = true;
        try {
          await api("POST", state.config.api_base + "/runs/" + encodeURIComponent(run.run_id) + "/approve", { approval_id: approval.approval_id, decision, comment: text });
          notice(t(decision === "approved" ? "approval.approved" : "approval.rejected"));
          await refreshStatus(run.run_id);
          if (!state.events) follow(run.run_id);
        } catch (problem) {
          error.textContent = problemText(problem);
          error.hidden = false;
          for (const button of actions.querySelectorAll("button")) button.disabled = false;
        }
      };
      const approve = document.createElement("button");
      approve.type = "button";
      approve.className = "primary";
      approve.textContent = t("approval.approve");
      approve.onclick = () => decide("approved");
      const reject = document.createElement("button");
      reject.type = "button";
      reject.className = "danger-ghost";
      reject.textContent = approval.on_reject === "stop" ? t("approval.reject_stop") : t("approval.reject");
      reject.onclick = () => decide("rejected");
      actions.append(approve, reject);
      box.append(error, actions);
      container.appendChild(box);
    }
  }

  async function loadResult(run) {
    if (!run) return;
    let result;
    try {
      result = await api("GET", state.config.api_base + "/runs/" + encodeURIComponent(run.run_id) + "/result");
    } catch (problem) {
      notice(problemText(problem));
      return;
    }
    renderOutput(result.output);
    renderFiles(run.run_id, result.files || []);
    $("live").hidden = true;
  }

  function renderOutput(output) {
    const container = $("output");
    container.textContent = "";
    const copy = $("copy");
    copy.hidden = output === null || output === undefined || output === "";
    if (copy.hidden) return;
    const heading = document.createElement("h3");
    heading.textContent = t("run.output");
    container.appendChild(heading);
    if (typeof output === "string") {
      container.appendChild(window.NodestroMarkdown.render(output));
      copy.onclick = () => copyText(output);
      return;
    }
    container.appendChild(structured(output));
    const raw = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = t("run.json");
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(output, null, 2);
    raw.append(summary, pre);
    container.appendChild(raw);
    copy.onclick = () => copyText(JSON.stringify(output, null, 2));
  }

  function structured(value) {
    if (Array.isArray(value) && value.length && value.every((item) => item && typeof item === "object" && !Array.isArray(item))) {
      const columns = Array.from(new Set(value.flatMap((item) => Object.keys(item)))).slice(0, 12);
      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      const table = document.createElement("table");
      const head = table.createTHead().insertRow();
      for (const column of columns) { const th = document.createElement("th"); th.scope = "col"; th.textContent = column; head.appendChild(th); }
      const body = table.createTBody();
      for (const item of value.slice(0, 500)) {
        const row = body.insertRow();
        for (const column of columns) row.insertCell().appendChild(cellValue(item[column]));
      }
      wrap.appendChild(table);
      return wrap;
    }
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      const table = document.createElement("table");
      table.className = "kv";
      const body = table.createTBody();
      for (const [key, item] of Object.entries(value)) {
        const row = body.insertRow();
        const th = document.createElement("th");
        th.scope = "row";
        th.textContent = key;
        row.appendChild(th);
        row.insertCell().appendChild(cellValue(item));
      }
      wrap.appendChild(table);
      return wrap;
    }
    return cellValue(value);
  }

  function cellValue(value) {
    if (value === null || value === undefined) return document.createTextNode("–");
    if (typeof value === "boolean") return document.createTextNode(value ? t("common.yes") : t("common.no"));
    if (typeof value === "number") return document.createTextNode(new Intl.NumberFormat(state.language).format(value));
    if (typeof value === "string") return value.includes("\n") || /[*#`|]/.test(value) ? window.NodestroMarkdown.render(value) : document.createTextNode(value);
    if (Array.isArray(value) && value.every((item) => typeof item !== "object" || item === null)) return document.createTextNode(value.join(", "));
    return structured(value);
  }

  function renderFiles(runId, files) {
    const container = $("files");
    container.textContent = "";
    if (!files.length) return;
    const heading = document.createElement("h3");
    heading.textContent = t("run.files");
    const list = document.createElement("ul");
    list.className = "files";
    for (const file of files) {
      const item = document.createElement("li");
      const icon = document.createElement("span");
      icon.className = "icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = (file.format || "").slice(0, 4);
      const info = document.createElement("div");
      info.className = "info";
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = file.name;
      const detail = document.createElement("div");
      detail.className = "detail";
      detail.textContent = [String(file.format || "").toUpperCase(), formatBytes(file.size)].filter(Boolean).join(" · ");
      info.append(name, detail);
      const url = state.config.api_base + "/runs/" + encodeURIComponent(runId) + "/files/" + encodeURIComponent(file.id);
      const download = document.createElement("button");
      download.type = "button";
      download.className = "ghost";
      download.textContent = t("run.download");
      download.setAttribute("aria-label", t("run.download_file", { name: file.name }));
      if (file.error_code) {
        download.disabled = true;
        detail.textContent += " · " + t("run.file_failed");
      }
      download.onclick = () => saveFile(url, file.name);
      item.append(icon, info, download);
      if (/^image\/(png|jpeg|gif|webp)/.test(file.media_type || "") && !file.error_code) {
        const image = document.createElement("img");
        image.className = "preview";
        image.alt = file.name;
        item.appendChild(image);
        api("GET", url, undefined, { raw: true }).then((response) => response.blob()).then((blob) => {
          image.src = URL.createObjectURL(blob);
        }).catch(() => image.remove());
      }
      list.appendChild(item);
    }
    container.append(heading, list);
  }

  async function saveFile(url, name) {
    try {
      const response = await api("GET", url, undefined, { raw: true });
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = name;
      document.body.appendChild(link);
      link.click();
      setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
    } catch (problem) {
      notice(problemText(problem));
    }
  }

  async function copyText(text, done) {
    try {
      await navigator.clipboard.writeText(text);
      notice(done || t("run.copied"));
    } catch (error) {
      notice(t("run.copy_failed"));
    }
  }

  async function cancelRun() {
    if (!state.run || !window.confirm(t("run.cancel_confirm"))) return;
    try {
      await api("POST", state.config.api_base + "/runs/" + encodeURIComponent(state.run.run_id) + "/cancel", {});
      notice(t("run.cancelling"));
      await refreshStatus(state.run.run_id);
    } catch (problem) {
      notice(problemText(problem));
    }
  }

  async function retryRun() {
    if (!state.run) return;
    try {
      const answer = await api("POST", state.config.api_base + "/runs/" + encodeURIComponent(state.run.run_id) + "/resume", { restart: true });
      await openRun(answer.run_id);
    } catch (problem) {
      notice(problemText(problem));
    }
  }

  // ---- history ------------------------------------------------------------------------------------------------

  async function loadHistory() {
    const rows = $("history-rows");
    rows.textContent = "";
    let runs = [];
    try {
      runs = await api("GET", state.config.api_base + "/runs?limit=50");
    } catch (problem) {
      notice(problemText(problem));
    }
    $("history-empty").hidden = runs.length > 0;
    for (const run of runs) {
      const row = rows.insertRow();
      row.tabIndex = 0;
      row.insertCell().textContent = formatDate(run.created_at);
      const badge = document.createElement("span");
      badge.className = "badge " + run.status;
      badge.textContent = t("status." + run.status);
      row.insertCell().appendChild(badge);
      row.insertCell().textContent = formatDuration(run.duration_seconds);
      const cell = row.insertCell();
      const open = document.createElement("button");
      open.type = "button";
      open.className = "ghost";
      open.textContent = t("history.open");
      open.onclick = (event) => { event.stopPropagation(); openRun(run.run_id); };
      cell.appendChild(open);
      row.onclick = () => openRun(run.run_id);
      row.onkeydown = (event) => { if (event.key === "Enter") openRun(run.run_id); };
    }
  }

  // ---- settings (operator) ------------------------------------------------------------------------------------

  async function loadSetup() {
    let setup;
    try {
      setup = await api("GET", "/ui/api/setup");
    } catch (problem) {
      notice(problemText(problem));
      return;
    }
    renderProviders(setup.providers || []);
    renderOutputDir(setup.output_dir || {});
    renderLocalModels(setup.local_models || {});
    const docs = $("api-docs");
    docs.hidden = !setup.api_docs;
    $("api-openapi").href = state.config.api_base + "/openapi.json";
  }

  function renderProviders(providers) {
    const container = $("providers");
    container.textContent = "";
    if (!providers.length) {
      const none = document.createElement("p");
      none.textContent = t("setup.no_providers");
      container.appendChild(none);
      return;
    }
    for (const provider of providers) {
      const box = document.createElement("div");
      box.className = "provider";
      const name = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = provider.label;
      const detail = document.createElement("div");
      detail.className = "hint";
      detail.textContent = provider.source === "environment"
        ? t("setup.from_environment", { variables: (provider.variables || []).join(", ") })
        : provider.source === "settings" ? t("setup.from_settings") : provider.required ? t("setup.needs_key") : t("setup.optional_key");
      name.append(title, detail);
      const badge = document.createElement("span");
      badge.className = "badge " + (provider.configured ? "succeeded" : provider.required ? "failed" : "");
      badge.textContent = provider.configured ? t("setup.configured") : t("setup.missing");
      box.append(name, badge);
      if (provider.editable) box.appendChild(providerForm(provider));
      container.appendChild(box);
    }
  }

  function providerForm(provider) {
    const form = document.createElement("form");
    form.noValidate = true;
    const inputs = {};
    const addInput = (key, labelText, type, value) => {
      const id = "provider-" + provider.id + "-" + key;
      const label = document.createElement("label");
      label.htmlFor = id;
      label.textContent = labelText;
      const input = document.createElement("input");
      input.id = id;
      input.type = type;
      input.autocomplete = "off";
      input.spellcheck = false;
      if (value) input.value = value;
      inputs[key] = input;
      form.append(label, input);
    };
    if (provider.auth === "aws") addInput("region", t("setup.region"), "text", "");
    else {
      if (provider.base_url !== null && provider.base_url !== undefined) addInput("base_url", t("setup.base_url"), "text", provider.base_url || "");
      addInput("api_key", provider.configured ? t("setup.new_key") : t("setup.key"), "password", "");
    }
    const error = document.createElement("p");
    error.className = "error";
    error.setAttribute("role", "alert");
    error.hidden = true;
    const row = document.createElement("div");
    row.className = "row";
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "primary";
    save.textContent = t("setup.save");
    row.appendChild(save);
    if (provider.source === "settings") {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger-ghost";
      remove.textContent = t("setup.remove");
      remove.onclick = async () => {
        if (!window.confirm(t("setup.remove_confirm", { provider: provider.label }))) return;
        try {
          await api("DELETE", "/ui/api/setup/providers/" + encodeURIComponent(provider.id));
          notice(t("setup.removed"));
          await refreshConfig();
          loadSetup();
        } catch (problem) { error.textContent = problemText(problem); error.hidden = false; }
      };
      row.appendChild(remove);
    }
    form.append(error, row);
    form.onsubmit = async (event) => {
      event.preventDefault();
      const values = {};
      for (const [key, input] of Object.entries(inputs)) if (input.value.trim()) values[key] = input.value.trim();
      save.disabled = true;
      try {
        await api("PUT", "/ui/api/setup/providers/" + encodeURIComponent(provider.id), values);
        for (const input of Object.values(inputs)) if (input.type === "password") input.value = "";
        notice(t("setup.saved"));
        await refreshConfig();
        loadSetup();
      } catch (problem) {
        error.textContent = problemText(problem);
        error.hidden = false;
      } finally {
        save.disabled = false;
      }
    };
    return form;
  }

  async function refreshConfig() {
    try { state.config = await api("GET", "/ui/api/config"); renderProviderBanner(); } catch (problem) { /* shown elsewhere */ }
  }

  // ---- result folder --------------------------------------------------------------------------------------------

  const FOLDER_ICON = "M3 6.5A2.5 2.5 0 0 1 5.5 4h4.1l2 2h6.9A2.5 2.5 0 0 1 21 8.5v9a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z";

  function icon(pathData, className) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    if (className) svg.setAttribute("class", className);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathData);
    svg.appendChild(path);
    return svg;
  }

  function lastSegment(path) {
    const parts = String(path || "").split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : String(path || "");
  }

  // A path that may break after each separator (and anywhere in a very long name, see .path-text).
  function fillPath(node, path) {
    node.textContent = "";
    let piece = "";
    for (const char of String(path || "")) {
      piece += char;
      if (char === "/" || char === "\\") {
        node.appendChild(document.createTextNode(piece));
        node.appendChild(document.createElement("wbr"));
        piece = "";
      }
    }
    if (piece) node.appendChild(document.createTextNode(piece));
    node.title = path || "";
    return node;
  }

  function button(className, text, onClick) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = className;
    item.textContent = text;
    if (onClick) item.addEventListener("click", onClick);
    return item;
  }

  function hintLine(text) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = text;
    return hint;
  }

  function renderOutputDir(output) {
    state.output = output;
    const container = $("output-dir");
    container.textContent = "";
    const source = output.source || "settings";
    const badge = $("output-source");
    badge.hidden = !output.enabled && source !== "chosen";
    badge.className = "badge " + (source === "chosen" ? "running" : source === "environment" ? "awaiting_approval" : "");
    badge.textContent = t("setup.output_source_" + source);
    if (output.enabled) {
      const intro = document.createElement("p");
      intro.className = "muted";
      intro.textContent = t("setup.output_on");
      const box = document.createElement("div");
      box.className = "folder-box";
      box.appendChild(icon(FOLDER_ICON, "folder-icon"));
      const text = document.createElement("div");
      text.className = "folder-text";
      const name = document.createElement("strong");
      name.className = "folder-name";
      name.textContent = lastSegment(output.path);
      name.title = output.path || "";
      const full = document.createElement("code");
      full.className = "folder-full";
      full.id = "output-path";
      full.dir = "ltr";
      fillPath(full, output.path);
      text.append(name, full);
      box.appendChild(text);
      container.append(intro, box);
    } else {
      const off = document.createElement("p");
      off.textContent = t("setup.output_off");
      container.appendChild(off);
    }
    if (source === "chosen" && output.default_path) {
      const line = document.createElement("p");
      line.className = "hint default-line";
      const label = document.createElement("span");
      label.textContent = t("setup.output_default_label") + " ";
      const value = document.createElement("code");
      value.className = "path-text";
      value.dir = "ltr";
      line.append(label, fillPath(value, output.default_path));
      container.appendChild(line);
    }
    if (output.warning) {
      const warning = document.createElement("div");
      warning.className = "banner warning";
      warning.textContent = t("setup.output_fallback", { problem: output.warning });
      container.appendChild(warning);
    }
    const row = document.createElement("div");
    row.className = "actions folder-actions";
    if (output.can_change) {
      row.appendChild(button("primary", t(output.enabled ? "setup.output_change" : "setup.output_choose"), () => openFolderDialog()));
      row.lastChild.id = "output-change";
    }
    if (output.can_open) {
      row.appendChild(button("ghost", t("setup.output_open"), async () => {
        try { await api("POST", "/ui/api/setup/output/open"); notice(t("setup.output_opened")); } catch (problem) { notice(problemText(problem)); }
      }));
      row.lastChild.id = "output-open";
    }
    if (output.enabled && output.path) {
      row.appendChild(button("ghost", t("setup.output_copy"), () => copyText(output.path, t("setup.output_copied"))));
      row.lastChild.id = "output-copy";
    }
    if (output.can_change && source === "chosen") {
      row.appendChild(button("ghost", t("setup.output_reset"), resetOutputDir));
      row.lastChild.id = "output-reset";
    }
    if (row.childNodes.length) container.appendChild(row);
    if (source === "environment") container.appendChild(hintLine(t("setup.output_hint_environment")));
    else if (output.can_change) container.appendChild(hintLine(t("setup.output_hint")));
    else container.appendChild(hintLine(t("setup.output_hint_file")));
  }

  async function resetOutputDir() {
    const output = state.output || {};
    if (!window.confirm(t("setup.output_reset_confirm", { path: output.default_path || "-" }))) return;
    try {
      renderOutputDir(await api("DELETE", "/ui/api/setup/output"));
      notice(t("setup.output_reset_done"));
    } catch (problem) { notice(problemText(problem)); }
  }

  const folder = { view: null, returnFocus: null };

  function folderError(text) {
    const box = $("folder-error");
    box.textContent = text || "";
    box.hidden = !text;
  }

  async function browseFolder(path) {
    folderError("");
    const list = $("folder-list");
    list.setAttribute("aria-busy", "true");
    try {
      const query = path ? "?path=" + encodeURIComponent(path) : "";
      folder.view = await api("GET", "/ui/api/setup/output/browse" + query);
      renderFolderView();
    } catch (problem) {
      folderError(problemText(problem));
    } finally {
      list.removeAttribute("aria-busy");
    }
  }

  function placeLabel(place) {
    if (place.kind === "drive" || place.kind === "media") return place.path;
    return t("folder.place_" + place.kind);
  }

  function renderFolderView() {
    const view = folder.view;
    $("folder-path").value = view.path;
    $("folder-up").disabled = !view.parent;
    const places = $("folder-places");
    places.textContent = "";
    for (const place of view.places || []) {
      const chip = button("chip", placeLabel(place), () => browseFolder(place.path));
      chip.title = place.path;
      if (place.path === view.path) chip.setAttribute("aria-current", "true");
      places.appendChild(chip);
    }
    const list = $("folder-list");
    list.textContent = "";
    for (const entry of view.folders) {
      const item = document.createElement("li");
      const open = document.createElement("button");
      open.type = "button";
      open.className = "folder-item";
      open.title = entry.path;
      open.appendChild(icon(FOLDER_ICON, "folder-icon"));
      const label = document.createElement("span");
      label.className = "folder-label";
      label.textContent = entry.name;
      open.appendChild(label);
      if (!entry.usable) {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = t("folder.protected");
        open.appendChild(tag);
      }
      open.addEventListener("click", () => browseFolder(entry.path));
      item.appendChild(open);
      list.appendChild(item);
    }
    if (!view.folders.length) {
      const empty = document.createElement("li");
      empty.className = "folder-empty";
      empty.textContent = t("folder.empty");
      list.appendChild(empty);
    }
    $("folder-count").textContent = view.folders.length === 1 ? t("folder.count_one") : t("folder.count", { n: view.folders.length });
    const note = $("folder-note");
    note.hidden = true;
    if (!view.usable) { note.textContent = t("folder.protected_here"); note.hidden = false; }
    else if (!view.writable) { note.textContent = t("folder.not_writable_here"); note.hidden = false; }
    else if (view.truncated) { note.textContent = t("folder.truncated", { n: view.folders.length }); note.hidden = false; }
    $("folder-use").disabled = !view.usable;
  }

  function openFolderDialog() {
    const dialog = $("folder-dialog");
    folder.returnFocus = document.activeElement;
    folderError("");
    $("folder-new-row").hidden = true;
    $("folder-list").textContent = "";
    $("folder-use").disabled = false;
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    browseFolder((state.output && state.output.path) || "").then(() => $("folder-path").focus());
  }

  function closeFolderDialog() {
    const dialog = $("folder-dialog");
    if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
  }

  async function useFolder(event) {
    event.preventDefault();
    const typed = $("folder-path").value.trim();
    if (!typed) { folderError(t("error.output.invalid")); return; }
    const choose = (create) => api("PUT", "/ui/api/setup/output", { path: typed, create });
    $("folder-use").disabled = true;
    try {
      let result;
      try {
        result = await choose(false);
      } catch (problem) {
        if (problem.code !== "output.missing") throw problem;
        if (!window.confirm(t("folder.create_confirm", { path: problem.params.path || typed }))) return;
        result = await choose(true);
      }
      closeFolderDialog();
      renderOutputDir(result);
      notice(t("setup.output_saved"));
    } catch (problem) {
      folderError(problemText(problem));
    } finally {
      $("folder-use").disabled = false;
    }
  }

  async function createSubfolder() {
    const name = $("folder-new-name").value.trim();
    if (!name) { folderError(t("error.output.bad_name")); return; }
    try {
      folder.view = await api("POST", "/ui/api/setup/output/folders", { parent: folder.view ? folder.view.path : $("folder-path").value, name });
      $("folder-new-name").value = "";
      $("folder-new-row").hidden = true;
      renderFolderView();
      $("folder-path").focus();
    } catch (problem) { folderError(problemText(problem)); }
  }

  function setupFolderDialog() {
    const dialog = $("folder-dialog");
    $("folder-form").addEventListener("submit", useFolder);
    $("folder-close").addEventListener("click", closeFolderDialog);
    $("folder-cancel").addEventListener("click", closeFolderDialog);
    $("folder-go").addEventListener("click", () => browseFolder($("folder-path").value.trim()));
    $("folder-up").addEventListener("click", () => { if (folder.view && folder.view.parent) browseFolder(folder.view.parent); });
    $("folder-new").addEventListener("click", () => {
      $("folder-new-row").hidden = false;
      $("folder-new-name").focus();
    });
    $("folder-new-create").addEventListener("click", createSubfolder);
    $("folder-new-name").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); createSubfolder(); } });
    $("folder-path").addEventListener("keydown", (event) => {
      // Enter in the path field goes there; "Use this folder" stays a deliberate click.
      if (event.key === "Enter") { event.preventDefault(); browseFolder($("folder-path").value.trim()); }
    });
    dialog.addEventListener("close", () => { if (folder.returnFocus && folder.returnFocus.focus) folder.returnFocus.focus(); });
  }

  function renderLocalModels(info) {
    const container = $("local-models");
    container.textContent = "";
    const models = info.models || [];
    if (!models.length) {
      const none = document.createElement("p");
      none.textContent = t("setup.no_local_models");
      container.appendChild(none);
      return;
    }
    const status = document.createElement("p");
    status.textContent = info.running ? t("setup.model_server_running") : t("setup.model_server_stopped");
    container.appendChild(status);
    const list = document.createElement("ul");
    for (const model of models) {
      const item = document.createElement("li");
      item.textContent = typeof model === "string" ? model : [model.name, formatBytes(model.size)].filter(Boolean).join(" · ");
      list.appendChild(item);
    }
    container.appendChild(list);
    if (info.offline) {
      const offline = document.createElement("p");
      offline.className = "hint";
      offline.textContent = t("setup.offline");
      container.appendChild(offline);
    }
    for (const warning of info.warnings || []) {
      const box = document.createElement("div");
      box.className = "banner warning";
      const parts = String(warning).split(":");
      box.textContent = parts[0] === "memory"
        ? t("setup.memory_warning", { needed: formatBytes(Number(parts[1])), ram: formatBytes(Number(parts[2])) })
        : warning;
      container.appendChild(box);
    }
  }

  // ---- sign-in ------------------------------------------------------------------------------------------------

  async function signIn(event) {
    event.preventDefault();
    const key = $("signin-key").value.trim();
    const error = $("signin-error");
    error.hidden = true;
    if (!key) { error.textContent = t("auth.key_missing"); error.hidden = false; return; }
    try {
      const session = await api("POST", "/ui/api/session", { api_key: key });
      $("signin-key").value = "";
      state.boot.session = session;
      await enter();
    } catch (problem) {
      error.textContent = problem.status === 401 ? t("auth.key_wrong") : problemText(problem);
      error.hidden = false;
    }
  }

  async function signOut() {
    try { await api("DELETE", "/ui/api/session"); } catch (problem) { /* already gone */ }
    state.boot.session = null;
    state.run = null;
    history.replaceState(null, "", "/");
    showSignIn();
  }

  // ---- the interface on its own origin (NAL-C5) ----------------------------------------------------------------

  async function startStandalone() {
    setupTheme();
    loadManifest();
    // First of all, so the code leaves the address even when no backend can be reached.
    readPairingLink();
    const chosen = store.get("language");
    const browser = (navigator.language || "en").split("-")[0];
    const language = chosen && LANGUAGE_NAMES[chosen] ? chosen : LANGUAGE_NAMES[navigator.language] ? navigator.language : LANGUAGE_NAMES[browser] ? browser : "en";
    await loadStrings(language).catch(() => loadStrings("en"));
    state.boot = { name: "Nodestro", description: "", version: "", languages: Object.keys(LANGUAGE_NAMES), session: null, pairing: true, tls: {} };
    setupLanguages();
    document.body.classList.add("standalone");
    $("nav-backends").hidden = false;
    $("signin-form").hidden = true;
    // Set here, not in the page: the page names no address of any other site.
    $("backend-address").placeholder = "https://server:8800";
    $("pair-or-key").hidden = true;
    $("operator-hint").hidden = true;
    const current = backends.current();
    if (state.pairing && state.pairing.newBackend) {
      conn.set("disconnected");
      showBackends();
      $("backend-address").value = state.pairing.newBackend;
      if (state.pairing.fingerprint) $("backend-fingerprint").value = state.pairing.fingerprint;
      const confirm = $("backend-link");
      confirm.textContent = t("backends.link_confirm", { address: state.pairing.newBackend });
      confirm.hidden = false;
      $("backend-address").focus();
      return;
    }
    if (!current) {
      conn.set("disconnected");
      showBackends(state.pairing && state.pairing.badBackend ? t("backends.address_invalid") : "");
      return;
    }
    await openBackend(current);
  }

  /** Connects to a saved backend: discovery and contract, then a token from the refresh cookie or the sign-in. */
  async function openBackend(id) {
    const backend = backends.get(id);
    if (!backend) { showBackends(); return; }
    backends.choose(id);
    closeEvents();
    clearTimeout(retryTimer);
    if (tokens) tokens.forget();
    state.backend = backend;
    state.config = null;
    state.run = null;
    state.boot = { ...state.boot, name: backend.name, description: "", version: "", session: null, tls: {} };
    conn.base = backend.address;
    conn.discovery = null;
    tokens = new Standalone.TokenSession({ fetch: window.fetch.bind(window), base: backend.address, locks: navigator.locks || null, onToken: tokenArrived });
    if (conn.state !== "connecting") conn.set("connecting");
    $("app-name").textContent = backend.name;
    $("app-version").textContent = "";
    document.title = backend.name + " · Nodestro";
    let found;
    try {
      found = await conn.discover();
    } catch (problem) {
      if (problem.kind === "incompatible") { showIncompatible(problem.detail.backend || []); return; }
      conn.set("error", problem.detail);
      showBackends(t(problem.kind === "network" ? "backends.unreachable" : "conn.error_long", { address: backend.address }), backend.address);
      return;
    }
    const methods = (found && found.auth && found.auth.methods) || [];
    if (!found || !methods.includes("token")) {
      conn.set("error");
      showBackends(t("backends.no_token", { address: backend.address }));
      return;
    }
    state.boot.tls = found.tls || {};
    if (found.backend && found.backend.name && backend.name === new URL(backend.address).host) {
      state.backend = backends.update(id, { name: found.backend.name }) || backend;
      $("app-name").textContent = state.backend.name;
    }
    renderTlsNotice();
    if (fingerprintMismatch()) { showSignIn(); return; }
    try {
      await tokens.refresh();
    } catch (problem) {
      if (problem instanceof Standalone.TokenProblem) { showSignIn(); return; }
      conn.set("error");
      showBackends(t("backends.unreachable", { address: backend.address }), backend.address);
      return;
    }
    await enter();
  }

  /** Every new access token brings the app's configuration and the device (grant and refresh). */
  function tokenArrived(answer) {
    const device = answer.device || {};
    state.config = answer.app || state.config;
    state.boot.session = { scopes: device.scopes || [], operator: false, name: device.name || "", device: true };
    if (state.config) {
      state.boot.name = state.config.name || state.boot.name;
      state.boot.description = state.config.description || "";
      state.boot.version = state.config.version || "";
      $("app-name").textContent = state.boot.name;
      $("app-version").textContent = state.boot.version ? "v" + state.boot.version : "";
      document.title = state.boot.name + " · Nodestro";
    }
    // Trust on first pairing: the fingerprint of a self-issued certificate is kept and compared from then on.
    const tls = state.boot.tls || {};
    if (state.backend && !state.backend.fingerprint && tls.mode === "self_signed" && tls.fingerprint) {
      state.backend = backends.update(state.backend.id, { fingerprint: tls.fingerprint }) || state.backend;
    }
  }

  async function pairStandalone(code) {
    const error = $("pair-error");
    try {
      await tokens.grant(code, $("pair-name").value.trim());
      $("pair-code").value = "";
      state.pairing = null;
      await enter();
    } catch (problem) {
      error.textContent = problem instanceof TypeError ? t("backends.unreachable", { address: conn.base }) : problemText(problem);
      error.hidden = false;
    }
  }

  async function reconnectStandalone() {
    if (!state.backend) { showBackends(); return; }
    conn.set("connecting");
    try {
      await tokens.refresh();
    } catch (problem) {
      if (problem instanceof Standalone.TokenProblem) { showSignIn(); return; }
      conn.set("disconnected");
      scheduleRetry();
      return;
    }
    if (!(await connect())) return;
    if (state.run && !FINAL.has(state.run.status)) { closeEvents(); follow(state.run.run_id); }
    else if (state.run) await refreshStatus(state.run.run_id);
  }

  function showBackends(message, address) {
    closeEvents();
    for (const id of ["nav-run", "nav-history", "nav-setup", "sign-out", "remove-device"]) $(id).hidden = true;
    const error = $("backend-error");
    error.textContent = message || "";
    error.hidden = !message;
    const open = $("backend-open");
    open.hidden = !address;
    if (address) open.href = address + "/api/app";
    show("backends");
  }

  function renderBackends() {
    const list = $("backend-list");
    list.textContent = "";
    const items = backends.list();
    $("backend-none").hidden = items.length > 0;
    const current = backends.current();
    for (const item of items) {
      const row = document.createElement("li");
      row.className = "backend";
      const text = document.createElement("div");
      text.className = "backend-text";
      const name = document.createElement("strong");
      name.textContent = item.name;
      const address = document.createElement("span");
      address.className = "muted mono";
      address.textContent = item.address;
      text.append(name, address);
      if (item.fingerprint) {
        const print = document.createElement("span");
        print.className = "hint mono";
        print.textContent = t("backends.fingerprint_saved", { fingerprint: item.fingerprint.match(/../g).join(":") });
        text.appendChild(print);
      }
      if (item.id === current) {
        const mark = document.createElement("span");
        mark.className = "hint";
        mark.textContent = t("backends.current");
        text.appendChild(mark);
      }
      const actions = document.createElement("div");
      actions.className = "backend-actions";
      actions.append(
        button("primary small", t("backends.connect"), () => openBackend(item.id)),
        button("ghost small", t("backends.rename"), () => {
          const next = window.prompt(t("backends.rename_prompt"), item.name);
          if (next && next.trim()) { backends.update(item.id, { name: next }); renderBackends(); }
        }),
        button("ghost small", t("backends.remove"), async () => {
          if (!window.confirm(t("backends.remove_confirm", { name: item.name }))) return;
          // The device on that backend is removed as well: with the current token, or with one from its refresh cookie.
          const session = item.id === current && tokens ? tokens : new Standalone.TokenSession({ fetch: window.fetch.bind(window), base: item.address, locks: navigator.locks || null });
          try {
            if (!session.token) await session.refresh();
            await session.revoke();
          } catch (problem) { /* not paired there, or not reachable: nothing more to remove */ }
          session.forget();
          backends.remove(item.id);
          if (item.id === current) { state.backend = null; state.config = null; if (tokens) tokens.forget(); }
          renderBackends();
        }),
      );
      for (const node of actions.children) node.setAttribute("aria-label", node.textContent + ": " + item.name);
      row.append(text, actions);
      list.appendChild(row);
    }
  }

  async function addBackend(event) {
    event.preventDefault();
    const error = $("backend-error");
    error.hidden = true;
    $("backend-open").hidden = true;
    $("backend-link").hidden = true;
    if (state.pairing) state.pairing.newBackend = null;
    const address = Standalone.normalizeAddress($("backend-address").value);
    if (!address) { error.textContent = t("backends.address_invalid"); error.hidden = false; return; }
    if (Standalone.normalizeFingerprint($("backend-fingerprint").value) === null) {
      error.textContent = t("backends.fingerprint_invalid");
      error.hidden = false;
      return;
    }
    const added = backends.add($("backend-name").value.trim(), address, $("backend-fingerprint").value);
    $("backend-form").reset();
    await openBackend(added.id);
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (standalone) $("backend-form").addEventListener("submit", addBackend);
    $("signin-form").addEventListener("submit", signIn);
    $("pair-form").addEventListener("submit", pair);
    $("remove-device").addEventListener("click", removeDevice);
    $("run-form").addEventListener("submit", submit);
    $("run-form").addEventListener("reset", () => setTimeout(() => renderForm(false), 0));
    $("sign-out").addEventListener("click", signOut);
    $("cancel").addEventListener("click", cancelRun);
    $("retry").addEventListener("click", retryRun);
    setupFolderDialog();
    setupAbout();
    window.addEventListener("offline", connectionLost);
    window.addEventListener("online", () => { if (conn.state === "disconnected") reconnect(); });
    for (const button of document.querySelectorAll(".navlink")) button.addEventListener("click", () => show(button.dataset.view));
    start();
  });
})();
