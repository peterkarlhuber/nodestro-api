# Vendored Swagger UI

The API reference page (`/api/docs`) serves these files itself (`nodestro_api.api_reference`), so it loads no
script from a third-party server and works offline.

- Package: `swagger-ui-dist` 5.34.0 (npm, published 2026-10-19), Apache License 2.0 (`LICENSE`, `NOTICE`;
  bundled third-party licenses in `swagger-ui-bundle.js.LICENSE.txt`).
- npm integrity: `sha512-H6tAIjr3XqE31PbkZl0OFya1ziRLDfTwh3J6OHdumPtPCLfjTnyLeagl/55kFNZcTOz9a9QI9WP0FXldwiGJag==`
- Files taken unchanged from the package (SHA-256, checked by `tests/test_api_reference.py`):

| File | SHA-256 |
|---|---|
| swagger-ui-bundle.js | 3f6abf70480db7e07914df4c98a77385f22ea585eaf457a3e6415ba0eae2d167 |
| swagger-ui.css | c3cfe8248c4b0b89673600fb829924eafbac6de4bc4b2b06f5f5243483074d0a |
| favicon-32x32.png | 3ed612f41e050ca5e7000cad6f1cbe7e7da39f65fca99c02e99e6591056e5837 |
| swagger-ui-bundle.js.LICENSE.txt | e5a4792feaa5332dfba37f1d7adf84308b93b4f2d8da0569a9f48a3cb61c969b |
| LICENSE | cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30 |
| NOTICE | 0d20d1adef18aee3f40dd258172155521ce702ac445cb5f7b7d60ed32dad2fb2 |

`nodestro-init.js` is Nodestro's own (MIT): it starts Swagger UI without an inline script.

To update: `npm pack swagger-ui-dist@<exact version>`, copy the files above from `package/`, and update the version,
the integrity, the hashes here and `SWAGGER_UI_VERSION` / `VENDORED_SHA256` in `nodestro_api/api_reference.py`.
