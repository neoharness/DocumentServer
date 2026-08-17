# neoHarness Office third-party notices

neoHarness Office 9.3.3-nh1 is built from the pinned Euro-Office 9.3.3
DocumentServer source graph and the third-party components contained in or
retrieved by that build.

The complete corresponding-source release contains:

- the exact recursive source tree for the superproject and every submodule;
- the GNU Affero General Public License, version 3;
- component, font, dictionary, and bundled-library license texts in their
  original source locations;
- `NEOHARNESS-RELEASE/LICENSE-FILES.json`, an exact path, size, and SHA-256
  inventory of license and notice files;
- SPDX and CycloneDX software bills of materials for the source tree, viewer
  image, and headless runtime image; and
- the ordered neoHarness patch series and exact source-coordinate manifest.

The headless runtime includes Ubuntu packages for document inspection,
conversion, OCR, image processing, and intermediate transformations. Their
package metadata and licenses remain governed by their respective copyright
holders and licenses. The release SBOMs enumerate the exact packaged versions.

The source-controlled font payload includes redistributable families and their
license files, including Carlito, Caladea, Liberation, Noto, Open Sans, and
other scripts required by the native renderer. Microsoft proprietary font
files are not included. Because the pinned upstream `core-fonts` tree omits the
preferred source and license text for its Liberation Fonts 1.07.4 binaries,
neoHarness includes the unmodified upstream source archive and records its
origin and SHA-256 digest in `neoharness/release/FONT-SOURCES.md`.

This notice is informational and does not replace or narrow any component's
license terms. The mechanically generated license inventory and corresponding
source archive are the authoritative release materials.
