# The standard interface from another origin (NAL-C5)

Status: protocol and threat model, written before the code and reviewed with it. Cross-origin access stays behind
`NODESTRO_CROSS_ORIGIN` (default off) until the independent review D1, which covers pairing (NAL-C4) and this
profile together. Switching it on for productive use needs the owner's consent. No native client is part of C5.

## Goal

The standard interface (nodestro-app-ui) can run apart from the backend: as plain files on a static host of its own
(`https://apps.example`), or on another computer. It keeps a list of backends (name, address, certificate
fingerprint) and connects to one after the other. Today every backend serves its own interface (C2 to C4) and a
browser on another computer uses that one; nothing of that changes.

Not part of C5: a native client (Tauri) with certificate pinning, LAN discovery, user accounts, operator rights from
another origin, API keys in the browser of another origin.

## Switches and allowed origins

- **Off by default.** Cross-origin access needs `NODESTRO_CROSS_ORIGIN=on` **and** pairing (`NODESTRO_PAIRING=on`,
  NAL-C4), because a device right from a pairing code is the only way to get a token. Only portable packages offer
  it (they have the launcher's pairing and device rights); the desktop and the server profile do not.
- **Allowed origins.** Only origins the operator added explicitly may use the backend:
  `start.sh origins add https://apps.example`, `start.sh origins list`, `start.sh origins remove ORIGIN`. They are
  kept in `data/settings/allowed-origins.json`; the running application reads the file again when it changes.
  An origin is `https://HOST[:PORT]`, or `http://` only for `localhost`, `127.0.0.1` and `[::1]` (a local
  development server); no path, query, fragment, user info or wildcard, never `null`. Removing an origin revokes the
  devices paired from it at once.
- With the switch off, or for an origin that is not allowed, the backend sends no CORS header at all: the browser
  keeps the page of the other origin from reading any answer. A request with an `Origin` that is not allowed is
  refused with `403 cors.origin_not_allowed` (preflight and actual request) on the cross-origin paths below.

## Token profile

### Getting a token: pairing from another origin

`POST /api/app/token` with `{"grant_type": "pairing_code", "code": "K7QD-M2XA", "name": "Laptop"}`:

1. Refused unless both switches are on (`404 token.disabled`), the request has an allowed `Origin` that is not the
   backend's own (`403 cors.origin_not_allowed`), the header `X-Nodestro-App: 1` (a custom header: no simple
   cross-site form can send it, and it forces a preflight), TLS or loopback (`403 pairing.tls_required`).
2. The code is redeemed exactly as for C4 (one use, 5 attempts per code, 20 failures per address and hour, codes
   only as hashes in memory). It creates a C4 device right with fixed scopes from the code, and the device records
   the `origin` it was paired from.
3. The answer carries a short-lived **access token** and the app's configuration (the same object the embedded
   interface reads from `/ui/api/config`, never operator data); it sets the **refresh cookie**.

```json
{"access_token": "nat_…", "token_type": "Bearer", "expires_in": 900,
 "device": {"id": "dev_…", "name": "Laptop", "scopes": ["run", "read"]}, "app": {…}}
```

### Access token

- `nat_` followed by 32 random bytes (URL-safe Base64), valid **15 minutes**, kept only in the memory of the running
  backend (as a SHA-256 hash) and only in the memory of the page (never in `localStorage`, `sessionStorage`, a
  cookie or the address). A restart voids every access token; the page asks for a new one with its refresh cookie.
- Sent as `Authorization: Bearer nat_…` to the app contract's routes (`GET /api/app`, `/api/published/{slug}/…`).
- Bound to its device **and** origin: every use checks that the device still exists, is not revoked or expired,
  that the origin is still allowed, and that the request's `Origin` header is the token's origin.
- The identity is the device's: an app user of this package's app with the scopes of the code, never the operator.
  The interface's own endpoints (`/ui/api/*`, `/ui/control/*`) do not accept tokens at all.
- At most 5 tokens per device (the oldest goes first) and 4,096 in all.

### Refresh cookie

- `nodestro_refresh_<the last 16 characters of the backend id>` = `<device id>.<secret>`; `HttpOnly; Secure;
  SameSite=None; Partitioned; Path=/api/app/token; Max-Age=90 days`. Cookies do not tell ports apart, so the name
  carries the backend: two backends on one computer keep a cookie each. `SameSite=None` is needed because the page and the backend are different
  sites; `Partitioned` (CHIPS) keeps the cookie to the interface's site, so another site that embeds the backend
  gets a different, empty cookie jar. The narrow path keeps it off every other request.
- `POST /api/app/token/refresh` (allowed `Origin` equal to the device's origin, `X-Nodestro-App: 1`, TLS or
  loopback) renews: the secret rotates exactly as for C4 (a secret that rotation already replaced revokes the
  device), the device's 90-day expiry slides, and the answer is the same as for the grant.
- A device paired from another origin cannot renew a session of the embedded interface
  (`/ui/api/device/session`), and a device of the embedded interface cannot get a token: each right stays with
  the place it was paired from.
- Several tabs of one interface renew one after the other under a Web Lock (as for C4), because a secret presented
  twice revokes the device.

### Revocation

- `DELETE /api/app/token` (with the access token): the device is revoked, its tokens are dropped and the refresh
  cookie is cleared. The interface calls it for "Remove this device" and for removing a backend from its list.
- On the backend's computer: `start.sh devices revoke ID|--all` (devices from another origin are listed with their
  origin) and `start.sh origins remove ORIGIN`. Revocation drops the device's access tokens at once.

## CORS

The CORS layer answers only while the switch is on and only on these paths:

| Path | Methods | Credentials (cookies) |
| --- | --- | --- |
| `/api/app` | `GET` | no |
| `/api/published/{slug}` and below (the package's app) | `GET`, `POST` | no |
| `/api/app/token`, `/api/app/token/refresh` | `POST`, `DELETE` | yes (only the refresh cookie lives there) |

- A request whose `Origin` is the backend's own (`scheme://host` of the request) is not cross-origin and passes
  unchanged. Every other path never gets a CORS header.
- Preflight (`OPTIONS` with `Access-Control-Request-Method`): `204` with `Access-Control-Allow-Origin` = the exact
  origin (never `*`), `Vary: Origin`, the methods above, `Access-Control-Allow-Headers: Authorization, Content-Type,
  Accept, Idempotency-Key, Last-Event-ID, X-Nodestro-App`, `Access-Control-Max-Age: 600`, and
  `Access-Control-Allow-Credentials: true` only on the token paths. A preflight from an origin that is not allowed
  gets `403` without CORS headers.
- Actual requests get the same origin, `Vary: Origin` and `Access-Control-Expose-Headers: Nodestro-App-Contract,
  Retry-After, Content-Disposition`.
- On the app's paths, a request from an allowed origin with a credential must use an access token: an API key sent
  from another origin is refused with `401 auth.token_required` (a key must never live in a page's script).
  Discovery without a credential stays possible, so the interface can check the contract and the TLS fingerprint
  before it pairs.

## The interface on its own

nodestro-app-ui builds a second form of itself, `npm run build:standalone` (`dist/standalone/`): the same files,
with `<meta name="nodestro-mode" content="standalone">` and a Content Security Policy in a meta element. The embedded
form is unchanged. In standalone mode the interface:

- keeps a list of backends in `localStorage` (name, address, fingerprint; never a secret or token) and switches
  between them; adding one checks the contract, that the backend offers `token`, and shows the fingerprint of a
  self-issued certificate with the "restricted trust" mark of C4;
- pairs with a code (no API key field), keeps the access token in memory and renews it a minute before it expires,
  and after a reload from the refresh cookie;
- reads run events with `fetch` (an `EventSource` cannot send a token) and downloads files through `fetch`;
- applies `default-src 'none'`, scripts and styles only from its own origin, `connect-src` only `https:` and
  `http://localhost` or `http://127.0.0.1`, and Trusted Types (`require-trusted-types-for 'script'; trusted-types 'none'`): the interface writes
  no HTML from strings at all. The embedded interface's policy gets the same Trusted Types directives.

The static host must add the headers a meta element cannot set: `Content-Security-Policy: frame-ancestors 'none'`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and HSTS (the README of nodestro-app-ui has an
example). Browsers that block partitioned third-party cookies (Safari in some settings) keep no refresh cookie: the
interface then works until the page is closed and pairs again afterwards.

## Threats and measures

| Threat | Measure |
| --- | --- |
| A foreign page reads data of the backend | CORS only for allowed origins, exact match, never `*`; no CORS on `/ui/*` and other paths |
| Cross-site request forgery | the refresh cookie is the only credential cookies carry, only on the token paths; those need `X-Nodestro-App` (preflight) and an allowed `Origin` equal to the device's |
| XSS in the interface steals a token | access token only in memory, 15 minutes; refresh cookie HttpOnly; strict CSP, Trusted Types, no HTML from strings; tokens bound to the origin; revocation |
| A stolen refresh cookie | HttpOnly, Secure, Partitioned, narrow path; rotation with reuse detection revokes the device; 90-day sliding expiry; revocation |
| A stolen access token used elsewhere | bound to origin and device; at most 15 minutes; revoked with the device. A non-browser client can forge `Origin`: the token's lifetime and revocation are the limit |
| Escalation | fixed scopes from the code, never operator rights; `/ui/*` never accepts tokens; API keys never accepted cross-origin |
| A removed origin keeps access | every token use and refresh checks the allow list; removing an origin revokes its devices |
| Guessing a code from another origin | the C4 limits apply unchanged (one use, 5 per code, 20 per address and hour) |
| Downgrade or a wrong backend | contract check; the fingerprint of a self-issued certificate is shown and compared with the one stored for the backend. That comparison uses the fingerprint the backend reports, so it catches a wrong address or a changed certificate, not an active man in the middle: only the browser's certificate dialog does (the interface links to the backend's address for it) |
| A link that adds a foreign backend | a pairing link with a backend not yet in the list is never added by itself: the person confirms the full address first |
| DNS rebinding | the existing host check; the allowed origins are exact |

Residual risks: a successful XSS in the interface can use the access token and the refresh cookie while the page is
open, like any single-page application; the CSP, Trusted Types and short lifetimes limit this, and revocation ends
it. A person who adds a backend with a self-issued certificate without comparing the fingerprint can be attacked in
the same network (as for C4). The interface's static host is trusted with the interface's code. Cross-origin access
is off by default and stays so until D1.
