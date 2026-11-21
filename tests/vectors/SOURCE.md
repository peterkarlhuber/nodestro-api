# Shared security test vectors (pinned copy)

`security-vectors.json` is an unchanged copy of nodestro-runtime's canonical shared security test vectors
(MUP-ONA MP-A8, finding VB-7): egress, ID token and redaction cases that Nodestro and Axentra both run.
`tests/test_security_vectors_jwt.py` (T-VEC-3) runs its `jwt` vectors against `nodestro_api.identity.OidcClient`
and first checks that the copy has the SHA-256 and version recorded below. The copy is pinned like a dependency: the
test never reads the runtime's test files, so a change of the canonical corpus reaches this repository only when the
copy is updated here. Field reference and maintenance process: `tests/vectors/README.md` in nodestro-runtime.

Origin: `https://github.com/peterkarlhuber/nodestro-runtime`, file `tests/vectors/security-vectors.json`, copied from
commit `9ee46fe840e0eb0425cd10189ec83721be63c717` (branch `mup/a8-security-vectors`)
Version: `1`
SHA-256: `f94167380a97c42ef2d02de8bbe10a899f00fe813c136998c9614c776e571283`

## Updating the copy

Replace `security-vectors.json` with the canonical file unchanged (never edit it by hand), then update the origin
commit, `Version` and `SHA-256` above (`sha256sum tests/vectors/security-vectors.json`) and run
`pytest tests/test_security_vectors_jwt.py`. A deviation of Nodestro's OIDC client is fixed, or listed with its
reason in the canonical file's `known_differences` (platform `nodestro`) first.

# The desktop launch with every NS-002 switch off

`launch-switches-off.json` holds the answers of the request matrix of `tests/launch_matrix.py` (desktop product app
with and without a launch secret: launch, logout, sessions, changes with and without `Origin`, body types and CSRF
header, rebinding and proxy headers), recorded from nodestro-api master `6783a527db2f95f3839418d13ac7dd1d41898b05`,
before the NS-002 switches existed. `tests/test_launch_hardening.py` checks that the code with every switch off still
gives these answers byte for byte (only the random session cookie values are replaced). Never regenerate it from a
commit with NS-002: a difference means the switches changed the behavior while off.
