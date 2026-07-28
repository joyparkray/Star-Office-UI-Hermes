# Star Office Animation System

How Star Office frontend (`game.js`, Phaser engine) renders agent states.

## State → Visual Mapping

| State | Desk character | Sofa | Special sprite |
|-------|---------------|------|----------------|
| `idle` | Hidden | Shows character lounging (`sofa_busy` animation) | — |
| `writing` | Visible, working animation | Empty (`sofa_idle`) | — |
| `researching` | Same as writing | — | — |
| `executing` | Same as writing | — | — |
| `syncing` | Hidden | Empty | `sync_anim` overlay plays (52-frame sprite, rotating/pulsing) |
| `error` | Hidden | Empty | `errorBug` walks left-right across the screen |

No physical "walk" transition — just visibility toggles. The desk-sprite
appears/disappears instantly. Only the sofa has a "lying down" animation
when idle.

## Polling & State Changes

- `game.js` polls `/status` every **2 seconds** (`FETCH_INTERVAL = 2000`)
- State changes are detected in `fetchStatus()` by comparing `nextState !== currentState`
- When state changes, a typewriter effect updates the status text line
- Between polls, manual `/set_state` calls may be overwritten by Hermes hooks (race condition)

## sync_anim (dynamic frame count)

- **File**: `sync-animation-v3-grid.webp`
- **Frame size**: 256×256
- **Position**: `LAYOUT.furniture.syncAnim`
- **Frame count**: Dynamically read from spritesheet (`textures.get('sync_anim')?.frameTotal`), not hardcoded. Frames 1 through (total-2) are played; frame 0 is the static idle frame.
- **Guard**: `syncAnimPlayable` flag prevents animation start when spritesheet has fewer than 3 usable frames.
- **Trigger**: `effectiveStateForServer === 'syncing'`
- **Stop**: Any non-syncing state

## errorBug (crawling bug)

- **File**: `error-bug-spritesheet-grid` (220×220 frames)
- **Trigger**: `effectiveStateForServer === 'error'`
- **Behavior**: Ping-pongs between `leftX` and `rightX` at `pingPong.speed`
- **Stop**: Any non-error state hides the sprite

## Bubbles

- Speech/status bubbles appear above characters every `BUBBLE_INTERVAL`
- Auto-destroy after 3 seconds
- Positioned relative to the active character's anchor point

## Known Pitfall: `on_session_start` Pushes Idle (fixed v2.4.1)

**Observed 2026-07-26.** The hook's `run()` function in `star_office_hook.py`
unconditionally pushed state to the backend on every `on_session_start` event.
Since the hook initializes new sessions with `phase=idle`, each session start
(including cron sessions like `cron_dadda7400df4_*`) reset the frontend
animation to idle.

**Diagnosis:** Inspect the hook state file for sessions with `phase=idle` and
recent `updated_at` timestamps matching session-start events:
```bash
python3 -c "
import json, time
with open('$TMPDIR/star-office-hermes-hook-state.json') as f:
    d = json.load(f)
now = time.time()
for sid, s in d.get('sessions',{}).items():
    ua = s.get('updated_at')
    age = f'{now-ua:.0f}s ago' if ua else 'N/A'
    print(f'{sid[-30:]:30s} phase={s.get(\"phase\"):10s} age={age}')
"
```
If cron sessions show phase=idle alongside the main session, each one pushed
idle at startup.

**Fix:** Added guard in `run()` → `reserve()`: skip push when event is
`on_session_start` and computed state is `idle`. The session is still tracked
but does not trigger a backend state update. Requires Hermes Desktop restart
for the plugin to pick up the change.

## Demo (manual state injection)

```bash
# POST directly to Star Office — bypasses hook
curl -s -X POST http://127.0.0.1:19000/set_state \
  -H 'Content-Type: application/json' \
  -d '{"state":"syncing","detail":"coordinating..."}'
sleep 10
curl -s -X POST http://127.0.0.1:19000/set_state \
  -H 'Content-Type: application/json' \
  -d '{"state":"error","detail":"tool failed"}'
sleep 10
curl -s -X POST http://127.0.0.1:19000/set_state \
  -H 'Content-Type: application/json' \
  -d '{"state":"idle","detail":"ready"}'

# Warning: running this inline causes Hermes hook to immediately overwrite
# with "executing". Use background process or control panel instead.
```

## Control Panel

Star Office frontend at `http://127.0.0.1:19000` has manual state buttons:
[待命] [写作] [研究] [执行] [同步]

These POST to `/set_state` directly and are NOT overridden by hooks
(because the hook's dedup logic skips same-state pushes).
