# neoHarness web-apps 9.3.3 port

The `web-apps` gitlink for neoHarness Office 9.3.3-nh1 is based on the exact
Euro-Office 9.3.3 coordinate
`91f0f979fdecda9d0cf416cef63ba8de9e990624`. The neoHarness fork retains only
the changes that are not already present at that coordinate.

## Legacy ten-patch disposition

| Legacy patch | 9.3.3 disposition |
| --- | --- |
| 0001 (`1c31c6a44293`) | Already upstream; the main-editor branding gate is present in the pinned baseline. |
| 0002 (`27cd8a47c8a1`) | Already upstream; title and loader-logo tokens are present in the pinned baseline. |
| 0003 (`f27b8bfafff9`) | Already upstream; the theme contract is documented in the pinned baseline. |
| 0004 (`7d3001e58a4a`) | Already upstream; dark loader selection uses `theme-type-dark`. |
| 0005 (`22d02b3d00ff`) | Already upstream; the mobile branding gate is present in the pinned baseline. |
| 0006 (`1590ce5f0514`) | Already upstream; dead romb styling is gone and the spreadsheet title is corrected. |
| 0007 (`01d508a6330b`) | Ported as `2229740d47`: allow only the signed hide-logo configuration on embed surfaces. |
| 0008 (`1a1d98e35ffa`) | Ported as `9d8fa07997`: prevent cold-load wordmark flash and collapse the hidden logo gutter. |
| 0009 (`e2a17547c19c`) | Ported to the current webpack pipeline as `6067b19e1a`: deploy the theme logo to the shared consumed path, reject the stock logo digest, keep a missing customer name empty, and add the whitelabel theme. |
| 0010 (`62f4fcd354b2`) | Superseded by the 9.3.3 webpack lock graph; a clean `npm ci` succeeds without changing `build/package-lock.json`. |

The Euro-Office merge tip containing legacy patches 0001-0006,
`8fcdf4604c9ba1416f5f0573be3dc003064717fc`, is an ancestor of the pinned
9.3.3 coordinate. Replaying those patches would create misleading empty or
duplicate history.

## Verification

The port was built with the production webpack pipeline and the whitelabel
theme:

```sh
cd web-apps/build
npm ci --ignore-scripts
PRODUCT_VERSION=9.3.3 \
BUILD_ROOT=/tmp/neoharness-web-apps \
THEME=whitelabel \
NODE_ENV=production \
node scripts/build-pipeline.js
```

The replacement, deployment, bundle, and browser-floor gates all pass. The
shared embed logo and per-editor embed logos are byte-identical to the selected
theme asset and are not byte-identical to the stock ONLYOFFICE asset.
