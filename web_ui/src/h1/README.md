# H1 — Spatial Gesture-Controlled 3D Workspace

Full-screen mode isolated from Nexus and D2.

## Activation

Voice / text:

- `Open H1` · `Start H1` · `Enable H1` · `Show H1` · `Enter H1`
- `Close H1` · `Exit H1` · `Stop H1`

Also: on-screen **Exit** button.

## Install

1. Copy the entire `web_ui/src/h1/` folder into your project at the same path.
2. Apply the patches in `../App.tsx.H1.patch.md` (or merge the listed changes into `web_ui/src/App.tsx`).

H1 reuses `d2/gestures/GestureSmoother.ts` (`OneEuroFilter`) — do not remove D2.

## Architecture

```
Camera → Hand Tracking (MediaPipe, up to 2 hands)
  → Landmark Processor + OneEuro smoothing
  → Coordinate calibration (affine)
  → Spatial Target Manager (Raycaster + dwell)
  → Interaction State Machine
  → Gesture Interpreter
  → H1Controller → Three.js
```

## Interaction summary

Directions are mirrored for the selfie camera: **move your hand left → content moves/rotates left**.

| Gesture | Condition | Effect |
|--------|-----------|--------|
| Stable hover (~320 ms) + pinch | Over object | Select / lock object |
| **Pinch + drag** | Object selected | **Move** object (follows hand) |
| **Pinch + fast swipe** (release) | Object selected | **Spin** object; coasts ~2–3s |
| **Open-hand swipe** | Object selected | **Spin** object with inertia |
| **Open-hand / empty swipe** | Nothing selected | **Rotate whole scene**; coasts ~2–3s |
| **Both hands pinch** | Over / on object | **Resize** (scale by distance between hands) |
| **Pinch + fling down** | Object selected | **Delete** object |
| Finger circle | Object selected, open hand | Continuous rotation (CW/CCW) |
| Color swatch | Object selected | Change material color |

Corner pinch-to-resize has been removed.

No cursor, reticle, or magnetic targeting. Feedback is object highlight / emission only.

## Objects

Procedural only: Cube, Cuboid, Sphere, Pyramid (BoxGeometry / SphereGeometry / ConeGeometry).

## Files

- `core/H1State.ts` — state types
- `core/H1Controller.ts` — public API + inertia tick
- `gestures/H1GestureController.ts` — MediaPipe + state machine
- `scene/H1Scene.tsx` — Three.js + camera background
- `H1Root.tsx` — shell + minimal HUD
- `h1.css` — holographic UI chrome
