# Port the ventilation PDF editor to another branch

## Reusable Copilot prompt

Paste this into Copilot after checking out the branch that should receive the
feature. Replace the source branch name if the work has since moved:

```text
Port the Tommy ventilation PDF editor and product-photo reference feature from
the `feature/fastAPI-copy0.1` branch into the current branch.

First inspect the source branch's commits and diffs, then inspect the current
branch for overlapping changes. Do not reset, overwrite, or discard current
branch work. Adapt the feature to this branch's existing routes, permissions,
database and UI patterns rather than blindly copying files.

The feature includes:
- A Tommy-only blank A4 workspace PDF editor with draggable, movable, resizable
  SVG ventilation symbols and printable vector PDF saving.
- A dashboard library for authorized PDFs, with original placed SVGs available
  to download.
- A draggable/click-to-add palette for the 12 SVGs in
  `BRUSNA_Ventilation_SVG_Symbols/`.
- A per-symbol product-photo review using the mappings in
  `BRUSNA_Vent_SVG_Linked_Library/svg_to_real_vent_photos.json`, including
  model names, photo crops and brochure/source notes. Users can review photos
  before placement and reopen them from an already-placed catalog symbol.
- Authenticated, path-restricted routes for the SVG catalog and JPEG references.
  Keep the existing SVG safety validation and PDF access controls intact.

Include the required `BRUSNA_Ventilation_SVG_Symbols/` and
`BRUSNA_Vent_SVG_Linked_Library/` assets; do not include the separate
`BRUSNA_Vent_Product_Pictures/` source folder unless the target branch already
uses it or the user requests it. Preserve the warning that some photos are
representative category images rather than verified exact model matches.

Update directly related documentation and tests. Run the focused PDF/SVG/photo
tests, Python compilation, JavaScript syntax check, and `git diff --check`.
Review the final diff and report any conflicts or behavior that could not be
ported. Do not commit or push unless I explicitly ask.
```

## Source work

- Source branch: `feature/fastAPI-copy0.1`
- Main implementation: `server.py`, `static/app.js`, `static/index.html`,
  `static/app.css`
- Coverage: `tests/test_house_flow.py`
- Required local assets: `BRUSNA_Ventilation_SVG_Symbols/` and
  `BRUSNA_Vent_SVG_Linked_Library/`
- Validation:
  `python3 -m unittest tests.test_house_flow.UiWiringTests.test_surveyors_have_access_to_the_shared_pdf_and_svg_editor tests.test_house_flow.CalculatorApiTests.test_tommy_can_place_svg_on_a4_pdf_and_download_the_original`
  `python3 -m py_compile server.py`
  `node --check static/app.js`
  `git diff --check`
