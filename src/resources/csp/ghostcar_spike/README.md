# CSP Ghost Car spike

This is a deliberately small CSP Lua runtime probe. Copy the folder to
`extension/lua/new-modes/ghostcargenerator-spike` in an Assetto Corsa
installation and enable it as a CSP new mode.

It places the Initial G GR86 Premium KN5 model 5 m to the side, follows the player body orientation,
steers the front wheel nodes, and rolls the rear wheel nodes.
It does not load `.acreplay` bytes directly. If `ghost-runtime-v1.json` is
present next to `mode.lua`, the mode loads the exported body timeline and
interpolates it in-game; without that file it falls back to the visual spike
animation.

The result is also a go/no-go check for the CSP scene API. If the second model
does not render, or the wheel nodes cannot be transformed independently, stop
before extending the runtime player.

The first spike intentionally hard-codes
`ig_toyota_gr86_premium/toyota_gr86_premium.kn5` so the test is deterministic.
It does not depend on the active player's car model.

The included `manifest.ini` makes the mode appear in CSP's new-mode selector;
there is no separate `MODEL_KN5` field to fill in.
