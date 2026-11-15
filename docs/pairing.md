# Pairing a browser with a portable package (NAL-C4)

Status: protocol and threat model, written before the code and reviewed with it. Pairing stays behind
`NODESTRO_PAIRING` (default off) until the independent review D1 (decision E-7, 2026-11-15). Switching it on for
productive use needs the owner's consent.

## Goal

A portable package runs on computer A; a person opens its standard interface in a browser on computer B. Today B can
only sign in with an API key typed or pasted from A. Pairing replaces that with a short one-time code shown on A, and
gives B a revocable device right instead of a key.

Not part of C4: LAN discovery (mDNS), cross-origin clients (NAL-C5), native clients, PAKE, user accounts (server
profile), pairing for operator rights.

## Transport

Pairing needs TLS. The launcher serves the network in one of three ways, and the discovery `GET /api/app` names it
in `tls.mode`:

| `tls.mode` | When | Trust |
| --- | --- | --- |
| `certificate` | `server.tls_cert` and `server.tls_key` are configured (the operator's own certificate) | normal browser trust |
| `proxy` | `server.behind_proxy` (a TLS proxy in front) | the proxy's certificate |
| `self_signed` | `server.tls_self_signed` is on and neither of the above is configured | restricted: a person compares the SHA-256 fingerprint |
| `none` | loopback only | not reachable from the network |

Self-signed mode: on the first network start the launcher creates an ECDSA P-256 key and a certificate valid for
397 days, with the host name, `localhost` and the computer's IP addresses as subject alternative names, in
`data/secrets/tls/` (key 0600, Windows ACL for the current user). It is renewed 30 days before it expires and when the
addresses change. The fingerprint is the SHA-256 of the certificate's DER encoding, shown as 32 hex pairs. It needs
the `cryptography` library, which a package contains where a wheel for its platform exists; elsewhere the launcher
says that only an own certificate or a proxy is possible.

The interface marks a self-signed connection permanently as "restricted trust (self-issued certificate)" and shows
the fingerprint the backend reports. JavaScript cannot read the certificate the browser actually used, so the
comparison is the person's: the launcher prints the fingerprint on A, the browser's certificate dialog shows it on B.
The interface explains where to find it per browser.

A pairing request over plain HTTP is refused with `pairing.tls_required` unless it comes from a loopback address
(the request's own socket peer, never a forwarded header unless `behind_proxy`).

## Pairing codes

`start.sh pair [--name NAME] [--scopes run,read,approve] [--ttl MINUTES]` on A (the application must be running; the
launcher asks it over the existing local control channel):

- 8 characters of Crockford Base32 (40 bits) from `secrets`, shown as two groups of four (`K7QD-M2XA`). Input is
  case-insensitive; `-`, spaces, `I`/`L` (as 1) and `O` (as 0) are accepted.
- Valid for 10 minutes (`--ttl` 1 to 10), for one use. At most 5 wrong attempts per code, then the code is void.
- At most 20 failed pairing attempts per hour per client address, then `429 pairing.too_many_attempts`. At most 5
  open codes at a time. A wrong code counts as an attempt against every open code (an attacker does not know which
  code is open).
- Only the SHA-256 of the code is kept, in memory of the running process; comparison in constant time. A restart
  voids open codes. The code is never logged.
- Scopes are a subset of `run`, `read`, `approve` (default `run,read`). A code never grants operator rights: the
  settings endpoints (`/ui/api/setup/*`) and `/ui/control/*` stay reachable only through the local launch link.

The launcher prints the code, its validity, the TLS fingerprint, a firewall hint and the link
`https://HOST:PORT/?pair#code=CODE&fp=FINGERPRINT`. The code sits in the fragment, so it never reaches a server log.
The launcher prints no QR code: the package has no QR library, and the link and the short code are enough to type.

## Devices

`POST /ui/api/pair` with `{"code": "...", "name": "..."}` (same-origin header as every interface request):

1. Refused unless `NODESTRO_PAIRING=on` (`404 pairing.disabled`), over TLS or loopback (`403 pairing.tls_required`),
   within the attempt limits (`429`).
2. The code is looked up by hash, checked for expiry and use, and consumed.
3. A device right is created: `id` (`dev_…`), name (from the request, else the code's name, at most 60 characters),
   scopes and the publication of the code, `created_at`, `last_seen`, `expires_at` (90 days, sliding),
   `revoked_at`, a short user-agent hint, and the SHA-256 of the current device secret. Devices are kept in
   `data/devices.json` (0600), so they survive restarts.
4. The answer sets the normal session cookie (as after a sign-in with a key) and a device cookie
   `nodestro_device` = `<device id>.<secret>` (HttpOnly, Secure, SameSite=Strict, path `/ui/api/device`, 90 days).

`POST /ui/api/device/session` renews a session from the device cookie when the session has expired or the backend
restarted. The secret rotates on every use: the old value is replaced by a new one. A device cookie presented with a
secret that was already rotated away revokes the device (a stolen copy and the owner's browser cannot both keep
working). Expired and revoked devices are refused with `401 pairing.device_revoked`, and their sessions end at once:
every request of a device session checks that the device still exists and is not revoked.

`DELETE /ui/api/device` (the paired browser) revokes its own device and signs out. On A:
`start.sh devices list`, `start.sh devices revoke ID`, `start.sh devices revoke --all` (through the local control
channel while running, directly on `data/devices.json` while stopped).

Device ids appear shortened in logs (`dev_ab12…`); secrets and codes never do.

## Discovery and connection test

`GET /api/app` lists `pairing` in `auth.methods` while pairing is on (the pairing request itself still needs TLS or
loopback), and reports `tls: {enabled, mode, fingerprint}` (fingerprint only for `self_signed`). The interface's
state machine (NAL-C2) stays: "connected" only after discovery, contract, sign-in (or device renewal), app info and
`GET /api/published/{slug}/runs?limit=1` succeeded. It never claims a successful workflow run.

## Threats and measures

| Threat | Measure |
| --- | --- |
| Guessing a code | 40 bits, 5 attempts per code, 20 failures per hour per address, 10 minutes, one use |
| Eavesdropping | pairing and device cookies only over TLS (loopback excepted); Secure cookies |
| Man in the middle with a self-signed certificate | human fingerprint comparison, permanent "restricted trust" mark, own certificate or proxy recommended; residual risk documented |
| Reuse of a code | one use; only its hash is kept; void after restart |
| Escalation | fixed scopes from the code; never operator rights; setup and control endpoints refuse device sessions (test) |
| Theft of the device cookie | HttpOnly, Secure, Strict, narrow path, rotation with reuse detection, 90-day sliding expiry, revocation |
| CSRF | the existing same-origin header and Origin check on every state-changing request |
| DNS rebinding | the existing host check (`host.not_allowed`) |
| Downgrade | the contract names its versions; the interface refuses backends outside its range |
| Unauthorized discovery | no broadcasts; anonymous discovery stays minimal |
| Codes in logs or history | the pairing link carries the code in the fragment; the code is never logged |

Residual risks: a person who accepts a self-signed certificate without comparing the fingerprint can be attacked by
someone in the same network; anyone with write access to the package folder can read `devices.json` hashes (not the
secrets) and the TLS key. Pairing is off by default and stays so until D1.

## Desktop network listener

The desktop's network listener (`network.py`) serves only the published API, discovery and `/health`, with API keys
sent as `Authorization: Bearer`: no interface, no cookies, so it offers no pairing. A host check would not protect it
further: DNS rebinding lets a foreign page reach the listener, but the browser sends it no credential, and the page
cannot read an answer of another origin. It would however break people who reach the listener by a name of their own
network (a router's DNS name), so C4 does not add one there. Plain HTTP on that listener stays possible after the
studio's warning; pairing never runs over it.
