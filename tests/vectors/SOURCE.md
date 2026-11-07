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
