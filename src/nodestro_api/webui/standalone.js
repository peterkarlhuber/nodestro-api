/* The interface on its own origin (NAL-C5, docs/cross-origin.md in nodestro-api): the list of backends, the token
   profile and a reader of run events that can send a token.

   Only the standalone form of the interface (npm run build:standalone, <meta name="nodestro-mode" content="standalone">)
   uses this module. The list of backends keeps names, addresses and fingerprints, never a secret; the access token
   lives only in this module's memory, the refresh cookie is HttpOnly and never visible here. No DOM access: the module
   runs in the browser (window.NodestroStandalone) and in Node for the unit tests (module.exports). */
"use strict";

(function (root) {
  const STORE_KEY = "nodestro.backends";
  const CURRENT_KEY = "nodestro.backend";
  const MAX_BACKENDS = 50;
  //: http:// only for these (the policy's connect-src cannot name [::1]).
  const LOOPBACK = ["localhost", "127.0.0.1"];
  //: Renew the access token this long before it expires (milliseconds).
  const RENEW_EARLY = 60000;
  //: The most an unfinished event may hold (a backend that never ends an event cannot fill the page's memory).
  const MAX_EVENT = 1024 * 1024;

  /** A backend's address in canonical form (https://host[:port][/path], no trailing slash), or null.
      http:// only for loopback, as the backend allows it for origins. */
  function normalizeAddress(text) {
    let parsed;
    try {
      parsed = new URL(String(text || "").trim());
    } catch (error) {
      return null;
    }
    if (parsed.username || parsed.password || parsed.search || parsed.hash) return null;
    if (parsed.protocol === "http:" && !LOOPBACK.includes(parsed.hostname)) return null;
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
    return parsed.origin + parsed.pathname.replace(/\/+$/, "");
  }

  /** A fingerprint as 64 hex digits in upper case (colons and spaces dropped), "" for none, null if malformed. */
  function normalizeFingerprint(text) {
    const bare = String(text || "").replace(/[\s:]/g, "").toUpperCase();
    if (!bare) return "";
    return /^[0-9A-F]{64}$/.test(bare) ? bare : null;
  }

  function randomId() {
    const bytes = new Uint8Array(8);
    (root.crypto || globalThis.crypto).getRandomValues(bytes);
    return "b" + Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  /** The saved backends ({id, name, address, fingerprint}); storage is localStorage or a stand-in. */
  class Backends {
    constructor(storage) {
      this.storage = storage;
    }

    read(key) {
      try { return this.storage ? this.storage.getItem(key) : null; } catch (error) { return null; }
    }

    write(key, value) {
      try {
        if (!this.storage) return;
        if (value === null) this.storage.removeItem(key);
        else this.storage.setItem(key, value);
      } catch (error) { /* private mode: the list lives as long as the page */ }
    }

    list() {
      let items;
      try { items = JSON.parse(this.read(STORE_KEY) || "[]"); } catch (error) { items = []; }
      if (!Array.isArray(items)) return [];
      // Anything malformed (an edited store) is dropped, never trusted.
      return items.filter((item) => item && typeof item.id === "string" && normalizeAddress(item.address) === item.address)
        .map((item) => ({ id: item.id, name: String(item.name || "").slice(0, 80), address: item.address, fingerprint: normalizeFingerprint(item.fingerprint) || "" }));
    }

    save(items) {
      this.write(STORE_KEY, JSON.stringify(items.slice(0, MAX_BACKENDS)));
    }

    /** Adds a backend (or updates the one with the same address); returns it, or null for an invalid address. */
    add(name, address, fingerprint) {
      const canonical = normalizeAddress(address);
      const print = normalizeFingerprint(fingerprint);
      if (!canonical || print === null) return null;
      const items = this.list();
      const found = items.find((item) => item.address === canonical);
      if (found) {
        if (name) found.name = String(name).slice(0, 80);
        if (print) found.fingerprint = print;
        this.save(items);
        return found;
      }
      const item = { id: randomId(), name: String(name || new URL(canonical).host).slice(0, 80), address: canonical, fingerprint: print };
      items.push(item);
      this.save(items);
      return item;
    }

    get(id) {
      return this.list().find((item) => item.id === id) || null;
    }

    update(id, changes) {
      const items = this.list();
      const found = items.find((item) => item.id === id);
      if (!found) return null;
      if (typeof changes.name === "string" && changes.name.trim()) found.name = changes.name.trim().slice(0, 80);
      if (typeof changes.fingerprint === "string") found.fingerprint = normalizeFingerprint(changes.fingerprint) || "";
      this.save(items);
      return found;
    }

    remove(id) {
      this.save(this.list().filter((item) => item.id !== id));
      if (this.current() === id) this.write(CURRENT_KEY, null);
    }

    current() {
      const id = this.read(CURRENT_KEY);
      return id && this.get(id) ? id : null;
    }

    choose(id) {
      this.write(CURRENT_KEY, id);
    }
  }

  class TokenProblem extends Error {
    constructor(status, body) {
      const error = (body && body.error) || {};
      super(error.message || "HTTP " + status);
      this.status = status;
      this.code = error.code || "http." + status;
      this.params = error.params || {};
    }
  }

  /** The token profile with one backend: grant by pairing code, refresh from the cookie, revoke.

      options.fetch     the fetch function
      options.base      the backend's address
      options.locks     navigator.locks (or null: no other tab to wait for)
      options.onToken   called with the answer of every grant and refresh ({access_token, device, app, ...}) */
  class TokenSession {
    constructor(options) {
      this.fetch = options.fetch;
      this.base = options.base;
      this.locks = options.locks || null;
      this.onToken = options.onToken || null;
      this.token = null;
      this.expires = 0;
      this.timer = null;
      this.pending = null;
      this.clock = options.clock || (() => Date.now());
    }

    async call(method, path, body) {
      const init = {
        method,
        credentials: "include",
        headers: { Accept: "application/json", "X-Nodestro-App": "1" },
      };
      if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
      if (method === "DELETE" && this.token) init.headers.Authorization = "Bearer " + this.token;
      const response = await this.fetch(this.base + path, init);
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new TokenProblem(response.status, data);
      return data;
    }

    accept(answer) {
      if (!answer || typeof answer.access_token !== "string") throw new TokenProblem(500, null);
      this.token = answer.access_token;
      this.expires = this.clock() + Math.max(60, Number(answer.expires_in) || 900) * 1000;
      this.schedule();
      if (this.onToken) this.onToken(answer);
      return answer;
    }

    schedule() {
      clearTimeout(this.timer);
      const wait = Math.max(5000, this.expires - this.clock() - RENEW_EARLY);
      this.timer = setTimeout(() => { this.refresh().catch(() => {}); }, wait);
      if (this.timer && typeof this.timer.unref === "function") this.timer.unref();
    }

    /** Pairs this browser with a code; the backend sets the refresh cookie. */
    async grant(code, name) {
      return this.accept(await this.call("POST", "/api/app/token", { grant_type: "pairing_code", code, name }));
    }

    /** A new access token from the refresh cookie. Tabs renew one after the other: a secret presented twice revokes
        the device (the cookie jar is shared, so the waiting tab presents the secret the first one got). */
    refresh() {
      if (this.pending) return this.pending;
      const renew = async () => this.accept(await this.call("POST", "/api/app/token/refresh"));
      const run = this.locks ? this.locks.request("nodestro-token:" + this.base, renew) : renew();
      this.pending = Promise.resolve(run).finally(() => { this.pending = null; });
      return this.pending;
    }

    /** Removes this device on the backend (its tokens and the refresh cookie end) and forgets the token. */
    async revoke() {
      try { if (this.token) await this.call("DELETE", "/api/app/token"); } finally { this.forget(); }
    }

    forget() {
      clearTimeout(this.timer);
      this.token = null;
      this.expires = 0;
    }

    headers() {
      return this.token ? { Authorization: "Bearer " + this.token } : {};
    }
  }

  /** Splits a text/event-stream into events: feed it chunks, it calls emit(type, data, id) per complete event. */
  class EventParser {
    constructor(emit) {
      this.emit = emit;
      this.buffer = "";
      this.lastId = "";
    }

    push(chunk) {
      this.buffer += chunk;
      if (this.buffer.length > MAX_EVENT * 2) this.buffer = "";
      const blocks = this.buffer.split(/\r?\n\r?\n/);
      this.buffer = blocks.pop();
      if (this.buffer.length > MAX_EVENT) this.buffer = "";
      for (const block of blocks) {
        let type = "message";
        const data = [];
        for (const line of block.split(/\r?\n/)) {
          if (!line || line.startsWith(":")) continue;
          const at = line.indexOf(":");
          const field = at < 0 ? line : line.slice(0, at);
          const value = at < 0 ? "" : line.slice(at + 1).replace(/^ /, "");
          if (field === "event") type = value;
          else if (field === "data") data.push(value);
          else if (field === "id") this.lastId = value;
        }
        if (data.length) this.emit(type, data.join("\n"), this.lastId);
      }
    }
  }

  /** The part of EventSource the interface uses, over fetch: it sends the access token, which EventSource cannot.
      Reconnects with Last-Event-ID like EventSource; an answer other than 200 closes it. */
  class TokenEventSource {
    constructor(url, options) {
      this.url = url;
      this.fetch = options.fetch;
      this.headers = options.headers;
      this.listeners = {};
      this.onerror = null;
      this.readyState = 0;
      this.lastId = "";
      this.controller = null;
      this.retry = null;
      this.connect();
    }

    addEventListener(type, listener) {
      (this.listeners[type] = this.listeners[type] || []).push(listener);
    }

    dispatch(type, data) {
      for (const listener of this.listeners[type] || []) listener({ type, data, lastEventId: this.lastId });
    }

    failed() {
      if (this.readyState === 2) return;
      if (this.onerror) this.onerror(new Error("stream"));
    }

    async connect() {
      if (this.readyState === 2) return;
      this.readyState = 0;
      this.controller = new AbortController();
      const headers = { Accept: "text/event-stream", ...this.headers() };
      if (this.lastId) headers["Last-Event-ID"] = this.lastId;
      let response;
      try {
        response = await this.fetch(this.url, { headers, credentials: "omit", signal: this.controller.signal });
      } catch (error) {
        this.failed();
        this.again();
        return;
      }
      if (!response.ok || !response.body) {
        this.readyState = 2;
        if (this.onerror) this.onerror(new Error("HTTP " + response.status));
        return;
      }
      this.readyState = 1;
      const parser = new EventParser((type, data, id) => { this.lastId = id; this.dispatch(type, data); });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          parser.push(decoder.decode(value, { stream: true }));
        }
      } catch (error) { /* aborted or lost */ }
      if (this.readyState === 2) return;
      this.failed();
      this.again();
    }

    again() {
      if (this.readyState === 2) return;
      this.readyState = 0;
      clearTimeout(this.retry);
      this.retry = setTimeout(() => this.connect(), 2000);
    }

    close() {
      this.readyState = 2;
      clearTimeout(this.retry);
      if (this.controller) this.controller.abort();
    }
  }
  TokenEventSource.CONNECTING = 0;
  TokenEventSource.OPEN = 1;
  TokenEventSource.CLOSED = 2;

  const exported = { normalizeAddress, normalizeFingerprint, Backends, TokenSession, TokenProblem, EventParser, TokenEventSource, RENEW_EARLY };
  if (typeof module === "object" && module.exports) module.exports = exported;
  else root.NodestroStandalone = exported;
})(typeof window !== "undefined" ? window : globalThis);
