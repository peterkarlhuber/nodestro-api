/* The connection to the backend: discovery through the app contract (GET /api/app), the contract version check, the
   form of the app contract, and the connection state the status line shows.

   Kept apart from app.js so that a later step can connect the same interface to another backend (pairing, NAL-C4/C5)
   by giving BackendConnection another base address and another way to sign in. No DOM access here: the module runs
   in the browser (window.NodestroConnection) and in Node for the unit tests (module.exports). */
"use strict";

(function (root) {
  //: The contract versions this interface speaks (the same as manifest.json "contract").
  const CONTRACT = Object.freeze({ name: "nodestro-app", min: 1, max: 1 });

  //: The states of the connection and which state may follow which.
  const TRANSITIONS = Object.freeze({
    disconnected: ["connecting", "disconnected"],
    connecting: ["auth_required", "connected", "degraded", "error", "incompatible", "disconnected"],
    auth_required: ["connecting", "disconnected"],
    connected: ["connecting", "degraded", "auth_required", "disconnected", "incompatible", "error"],
    degraded: ["connecting", "connected", "auth_required", "disconnected", "error"],
    error: ["connecting", "disconnected"],
    incompatible: ["connecting"],
  });
  const STATES = Object.freeze(Object.keys(TRANSITIONS));

  //: Waiting times before the next attempt after the backend could not be reached (milliseconds; the last repeats).
  const BACKOFF = Object.freeze([1000, 2000, 4000, 8000, 15000, 30000]);

  function backoff(attempt) {
    return BACKOFF[Math.min(Math.max(attempt, 0), BACKOFF.length - 1)];
  }

  /** The highest contract version both sides speak, or null.
      `contract` is the "contract" object of GET /api/app ({name, versions, current}). */
  function negotiate(contract) {
    if (!contract || contract.name !== CONTRACT.name || !Array.isArray(contract.versions)) return null;
    const shared = contract.versions.filter((version) => Number.isInteger(version) && version >= CONTRACT.min && version <= CONTRACT.max);
    return shared.length ? Math.max(...shared) : null;
  }

  /** The fields of the app contract's form (form version 1) under the names this interface reads.
      The same mapping as the backend's ui_form_fields, so a backend without the contract (config.fields) and one
      with it give the same form. */
  function fieldsFromForm(form) {
    if (!Array.isArray(form)) return null;
    return form.map((item) => {
      const entry = {
        name: String(item.name),
        label: item.label || String(item.name),
        type: item.type,
        description: item.help || "",
        required: Boolean(item.required),
        default: item.default === undefined ? null : item.default,
        options: Array.isArray(item.options) ? item.options : [],
        accept: Array.isArray(item.accept_formats) ? item.accept_formats : [],
        multiple: Boolean(item.multiple),
        max_files: item.max_files === undefined ? null : item.max_files,
        max_size_mb: item.max_mb === undefined ? null : item.max_mb,
      };
      if (item.type === "file") entry.accept_extensions = Array.isArray(item.accept) ? item.accept : [];
      return entry;
    });
  }

  class ConnectionProblem extends Error {
    constructor(kind, detail) {
      super(kind);
      this.kind = kind; // "network" | "auth" | "incompatible" | "server"
      this.detail = detail || {};
    }
  }

  /** One backend: where it is, what it said about itself, and the state of the connection.

      options.fetch     the fetch function (the browser's, or a stand-in in tests)
      options.base      the backend's address ("" = the origin the interface came from)
      options.onChange  called with (state, connection) after every change */
  class BackendConnection {
    constructor(options) {
      const settings = options || {};
      this.fetch = settings.fetch || (typeof fetch === "function" ? fetch.bind(root) : null);
      this.base = settings.base || "";
      this.onChange = settings.onChange || null;
      this.state = "disconnected";
      this.discovery = null; // the answer of GET /api/app (null: not known yet or a backend without the contract)
      this.contract = null; // the negotiated version (null with a backend without the contract)
      this.legacy = false; // the backend has no app contract (compatibility mode: the form comes from /ui/api/config)
      this.attempts = 0;
      this.detail = {};
    }

    url(path) {
      return this.base + path;
    }

    set(next, detail) {
      if (!TRANSITIONS[next]) throw new Error("unknown state " + next);
      if (next !== this.state && !TRANSITIONS[this.state].includes(next)) {
        throw new Error("no transition " + this.state + " -> " + next);
      }
      this.state = next;
      this.detail = detail || {};
      if (next === "connected" || next === "degraded") this.attempts = 0;
      if (this.onChange) this.onChange(next, this);
    }

    /** Asks the backend what it is (GET /api/app with the session cookie). Resolves with the discovery answer, or
        null for a backend without the contract; rejects with a ConnectionProblem. Does not change the state. */
    async discover() {
      let response;
      try {
        response = await this.fetch(this.url("/api/app"), { credentials: "same-origin", headers: { Accept: "application/json" } });
      } catch (error) {
        throw new ConnectionProblem("network");
      }
      if (response.status === 404) {
        this.legacy = true;
        this.discovery = null;
        this.contract = null;
        return null;
      }
      if (response.status === 401) throw new ConnectionProblem("auth");
      if (!response.ok) throw new ConnectionProblem(response.status >= 500 ? "server" : "network", { status: response.status });
      let body;
      try {
        body = await response.json();
      } catch (error) {
        throw new ConnectionProblem("server", { status: response.status });
      }
      const version = negotiate(body && body.contract);
      if (version === null) {
        throw new ConnectionProblem("incompatible", {
          backend: body && body.contract && Array.isArray(body.contract.versions) ? body.contract.versions : [],
        });
      }
      this.legacy = false;
      this.discovery = body;
      this.contract = version;
      return body;
    }

    /** The backend's name and versions as the "About this app" dialog shows them. */
    describe() {
      const backend = (this.discovery && this.discovery.backend) || {};
      return {
        id: backend.id || "",
        name: backend.name || "",
        version: backend.version || "",
        runtimeVersion: backend.runtime_version || "",
        contract: this.contract,
        legacy: this.legacy,
        capabilities: (this.discovery && this.discovery.capabilities) || [],
      };
    }

    /** Whether the backend serves a capability (a backend without the contract: assumed, as before the contract). */
    can(capability) {
      if (this.legacy || !this.discovery) return true;
      return (this.discovery.capabilities || []).includes(capability);
    }
  }

  const exported = { CONTRACT, STATES, TRANSITIONS, BACKOFF, backoff, negotiate, fieldsFromForm, BackendConnection, ConnectionProblem };
  if (typeof module === "object" && module.exports) module.exports = exported;
  else root.NodestroConnection = exported;
})(typeof window !== "undefined" ? window : globalThis);
