# Task 15 — 3D Issue Gallery Attribution

## What the gallery is

`frontend/gallery.html` is an interactive viewer for the nine UIMRS civic issue
categories. It is an **illustrative teaching aid** for the project presentation.
It is **not** AI image recognition, **not** a reconstruction of any citizen
complaint, and **not** a digital twin of any city.

## Rendering stack

- Library: three.js r160, vendored locally at
  `frontend/vendor/three/three.module.min.js`.
- Licence: MIT, `frontend/vendor/three/LICENSE` (copyright three.js authors).
- Loading: imported as an ES module from `/vendor/three/three.module.min.js`. No
  CDN, no external hotlink, no runtime download.
- WebGL is detected before initialisation; when it is unavailable the viewer
  shows a clear fallback message instead of a broken canvas.

## Models

Every model is built **procedurally in the browser** from `THREE.*Geometry`
primitives (boxes, cylinders, spheres, tori, planes) and `MeshStandardMaterial`
instances. There are no imported `.glb`, `.gltf`, `.obj` or `.fbx` assets and
no third-party model files. The nine categories are:

1. Road Damage / Potholes
2. Garbage / Waste
3. Streetlights
4. Drainage / Sewage
5. Water Supply
6. Traffic Management / Signals
7. Trees / Environment
8. Illegal Construction
9. Public Property Damage

Models are constructed lazily (only the selected category is built) and cached
for the lifetime of the page. A shared ground platform and lighting rig are
created once at startup.

## Interaction

- Drag to rotate (pointer events, single pointer).
- Scroll/pinch to zoom (wheel + two-finger pinch).
- Arrow keys rotate, `+`/`-` zoom, `R` resets.
- Auto-rotate toggle.
- Reset-view button.
- Deep-link via URL fragment (`/gallery#road`).

## Performance notes

- Antialiasing enabled, pixel ratio capped at 2.
- Shadow maps 1024x1024, PCF soft.
- Geometry is simple (low segment counts) so the viewer runs on mid-range
  laptops.

## Provenance summary

| Asset | Source | Licence | Location |
|---|---|---|---|
| three.js r160 | Vendored from the three.js project | MIT | `frontend/vendor/three/` |
| Issue models | Authored for UIMRS (procedural code) | Project licence | `frontend/gallery.html` |
| Textures/images | None used | n/a | n/a |
