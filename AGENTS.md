# AGENTS.md

Handoff and working contract for AI agents (Cursor, Claude Code, Codex) in this repo.

## What this is

**pi plays Morrowind**: a cloud LLM plays The Elder Scrolls III: Morrowind on macOS through
the [pi coding agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent). It is
modelled on [geohot/pi_plays_pokemon](https://github.com/geohot/pi_plays_pokemon): a pi
extension gives the model three tools (screenshots in, keyboard/mouse-style actions out), a
sidecar owns the game, and a small web UI shows the live screen and a step table. You watch
the model's actions and reasoning in the pi terminal itself.

## Play

```bash
pi -a "Leave the prison ship."                    # attach to the running game, or launch one
PI_FRESH=1 pi -a "Leave the prison ship."         # restart OpenMW with a new game
pi -a --model openai/gpt-5.5 "..."                # pick a model for one run
open http://localhost:8342                        # live screen + step table (+ debug state)
```

The extension starts the sidecar on the first tool call (log: `runs/sidecar.log`), and the
sidecar starts the game. Evidence lands in `runs/<RUN_NAME>/`: `frames/`, `actions.jsonl`
(with harness-only state), and `result.json` from `report` (the model's verdict plus the
harness milestones). Stop everything with `pkill -f "openmw --config"; pkill -f game.py`.

Differences from the Pokémon harness:

- The game runs in real time and **stays live between actions** by default: the world keeps
 going while the model thinks. `PI_PAUSE=1` (for both pi and the sidecar) freezes it
 between actions instead. The extension fills the `{{timing}}` line of `.pi/SYSTEM.md`
 and the `act` description to match, so the model is told which mode it is in. A sidecar
 that is already running keeps the mode it was started with, so restart it when switching.
- In-world actions (move, turn, activate…) run inside the game via Lua. Menus
 (name entry, dialogue, race selection) are driven with synthesized macOS mouse and
 keyboard events.

## Vision only (hard rule)

The model learns about the game **only from screenshots**. It gets no cell names,
coordinates, headings, stats, object lists, object IDs, menu names or journal text as
data. Tool replies are the screenshot plus a one-line step summary
(`step 12: turn degrees=-30, screen change 18.4%`), or an error for malformed arguments.
Screen change is computed from the before and after screenshots only.

- Actions mirror what a player can do with keyboard and mouse. Nothing targets an object
 by ID or navigates for the model. `activate` uses whatever is under the crosshair.
- An action that has no effect in the game still returns `done`, just like a key press
 that does nothing. Examples: activating with nothing in reach, attacking with no weapon
 readied, moving while a menu holds the world. The model has to notice from the screen.
- The mod still takes a full state snapshot on every reply. It is for the harness only:
 run logs (`actions.jsonl`), milestone detection, and the debug panel in the human web UI.
 Model-facing endpoints (`/act`, `/look`, `/reset`, `/screen.jpg`) never include it.
 The pi extension only calls those, and `pi.setActiveTools(["look", "act", "report"])`
 excludes bash and read, so the model can't reach `/status`, `/log` or `runs/`.
- **pi context files would leak game facts.** pi loads `AGENTS.md` from the working
 directory into the system prompt, and this file is full of spoilers. The extension's
 `before_agent_start` hook clears `contextFiles`, `skills` and `appendSystemPrompt`, and it
 forces the system prompt to `.pi/SYSTEM.md`. Verified by dumping the provider request
 (`PI_DUMP_REQUEST=/tmp/req.json`): it contains only `SYSTEM.md` and the three tools.
 Re-check this after pi upgrades.
- The console key (`` ` ``) is refused for `key`/`type`, since console commands would expose
 game data.

**First goal:** finish the prison-ship part of character creation and leave the boat. Success
means the player's cell becomes exterior (the Seyda Neen dock). The sidecar already records
this as the milestone `left the ship`.

Models: cloud only, and switchable (pi's `--provider/--model` flags, or `/login` for a
Claude or ChatGPT subscription). Currently logged in: OpenAI via ChatGPT OAuth, and pi's
default is `openai` / `gpt-5.5`.

## Machine setup (already done on this Mac)

- OpenMW **0.52.0** at `/Applications/OpenMW.app`. The game binary is
  `Contents/MacOS/openmw`. `open -a OpenMW.app` starts the *launcher*, not the game.
- Morrowind GOTY data (Steam):
  `~/Library/Application Support/Steam/steamapps/common/The Elder Scrolls III - Morrowind/Data Files`,
  wired up in the user's own `~/Library/Preferences/openmw/openmw.cfg`, which we layer on top of.
- `.venv/` uses Homebrew Python 3.12 with the packages in `requirements.txt` (pyobjc
  Quartz/Cocoa/ApplicationServices, pillow).
- `luajit` (Homebrew) for syntax-checking the mod: `luajit -bl file.lua > /dev/null`.
- `pi` 0.99.1 is installed globally (`npm i -g @earendil-works/pi-coding-agent`).
- Cursor has **Screen Recording** and **Accessibility** permissions. Whatever app runs the
  sidecar needs both. When pi starts it, that is the terminal running pi.

## Layout

| Path | Purpose |
| --- | --- |
| `.pi/extensions/morrowind.ts` | pi extension: `look` / `act` / `report` tools, starts the sidecar, forces the system prompt |
| `.pi/SYSTEM.md` | The model's whole system prompt (controls and harness behaviour, no game facts) |
| `game.py` | Sidecar: launches/attaches OpenMW, command channel to the mod, screenshots + screen change, macOS input, run logs, HTTP API + web UI on `127.0.0.1:8342` |
| `mod/piagent.omwscripts` | OpenMW content file registering the mod's scripts |
| `mod/scripts/piagent/player.lua` | In-game half: polls commands, runs actions frame by frame, pauses the world afterwards only for `pause=true` commands, prints `PIAGENT {json}` replies (with a harness-only state snapshot) |
| `mod/scripts/piagent/global.lua` | Global-script helpers (activation) |
| `mod/scripts/piagent/json.lua` | Tiny JSON encoder (the OpenMW sandbox has none) |
| `mod/piagent/cmd.txt` | Command file the sidecar rewrites (atomically) and the mod polls |
| `openmw/settings.cfg` | Settings template: 1280×720 borderless window, no cursor grab |
| `requirements.txt` | Python deps for `.venv/` |
| `runs/` | Generated: `openmw-config/` (cfg + **openmw.log**), `userdata/` (saves), `sidecar.log`, per-run evidence dirs (`frames/`, `actions.jsonl`, `run.json`, `result.json`) |

Not written yet: `README.md`, and optionally `stream.py` / `play.sh`. See the plan below.

## How it works

0. **Model's view.** Each tool call returns one screenshot. That is everything the model
 knows about the game (see "Vision only" above).
1. **Game launch.** `game.py` writes `runs/openmw-config/openmw.cfg`, which adds
   `data="<repo>/mod"` and `content=piagent.omwscripts` and points the logo/intro movie
   fallbacks at a nonexistent file so they're skipped. It then runs
   `openmw --config runs/openmw-config --user-data runs/userdata --no-grab --skip-menu --new-game`.
   The user's real game settings and saves are untouched. OpenMW writes its log to
   `runs/openmw-config/openmw.log`.
2. **Commands in.** The sidecar writes one Lua table literal to `mod/piagent/cmd.txt`, e.g.
   `{seq=5, nonce="ab12cd34", op="move", dir="forward", seconds=1}`. `player.lua` polls it
   through `openmw.vfs` every 50 ms in `onFrame` (which runs even while paused) and parses it
   with `util.loadCode`. Re-reading a rewritten file works. Latency is about 100 ms.
3. **Results out.** When an op finishes, the mod sets the `piagent` pause tag from the
   command's `pause` flag (the sidecar sends `pause=true` only with `PI_PAUSE=1`, so by
   default it unpauses) and `print`s `PIAGENT {json}` carrying `seq`, `nonce`, `outcome`, `note` and
 `state`. The sidecar follows `openmw.log` and matches replies on **nonce + seq**, so old
 log lines from before a sidecar restart are ignored. `outcome`, `note` and `state` go to
 the run log only. The model sees an error only when the mod rejects malformed arguments.
4. **Input (click, type, key).** Keys go through `CGEventPostToPid`, clicks through the HID
 event tap. Typed characters and clicks only register while OpenMW is frontmost, so every UI
 action runs inside `_focus_game()` / `_unfocus_game()`. These set the Accessibility
 `AXFrontmost` attribute (NSRunningApplication activation is unreliable from a background
 process) and remember the user's front app by pid, so focus goes back to it afterwards.
 Leaving OpenMW focused is harmful: with `--no-grab`, any mouse movement over its window
 turns the camera. With `PI_PAUSE=1`, gameplay keys (activate, jump, attack) are ignored
 while the world is paused, so when no menu is open the sidecar unpauses (`release`) for the
 input and the following `wait` pauses again. In the default live mode none of that is
 needed. Clicks are allowed anywhere, whether or not a menu is open.
 **Default keys: Space = activate, E = jump**, F = weapon, R = spell, J = journal.
5. **Observation.** `screencapture -x -o -l <windowID>` produces 1280×720, which is resized to
 `MODEL_WIDTH` (1024) JPEG. That image is the model's only observation. `_debug_summary()`
 turns the state JSON into text for the web UI's debug panel (humans only).

### Mod ops (`player.lua`)

`state`, `pause`, `release` (unpause and hand back to a human), `wait {seconds}`
(real time), `move {dir, seconds, run}`, `turn {degrees, pitch}` (both relative),
`activate` (crosshair object within the engine's reach, i.e. `camera.getFocusRay().hitObject`;
sent to the global script, which calls `activateBy`), `jump`, `stance {nothing|weapon|spell}`,
`attack {seconds}`, `noop` (ignored; written before each launch so a stale command is never
replayed).

If an op needs the world running but a menu holds it paused, the op finishes as `blocked`
after 1 s. There is a 45 s hard timeout per op. Both are logged and reported to the model
as a normal step.

### Sidecar HTTP API (`game.py`)

Model-facing (the extension uses only these):

- `POST /reset?name=RUN&fresh=1`: ensures the game is up (`fresh=1` restarts it with a new
 game), starts a run dir and returns `{run, step}`.
- `POST /act` with `{"action": ..., ...}` returns `{step, changed}` (`changed` = fraction of
 the screen that differs from the previous screenshot), or `{error}` (HTTP 400) for bad
 arguments. World actions map to mod ops. UI actions: `click {x, y, button?, double?}`
 (screenshot coords), `type {text}`, `key {key, times?}`. Each UI action is followed by
 `wait` (default 0.4 s) so scripts react.
- `POST /look` returns `{step}`. `GET /screen.jpg` returns the latest screenshot.

Humans/dev only (these carry game state): `GET /status` (includes `debug`), `/log`, `GET /`
web UI (screen, step table, debug state), `POST /release`, `POST /reload-lua`.

## Dev loop

```bash
.venv/bin/python -u game.py                       # sidecar (launches/attaches the game)
curl -X POST 'localhost:8342/reset?name=dev&fresh=1'
curl -X POST localhost:8342/act -H 'content-type: application/json' \
     -d '{"action":"move","dir":"forward","seconds":1}'
curl -s localhost:8342/status                     # debug state (never give this to the model)
curl -X POST localhost:8342/reload-lua            # after editing existing .lua files
open http://localhost:8342                         # watch
```

- **`reload-lua` picks up edits to existing files only.** OpenMW indexes the VFS at startup,
  so a *new* file under `mod/` (new module, new data file) needs a game restart
  (`reset?fresh=1`). `mod/piagent/cmd.txt` must exist before launch.
- `reload-lua` types `` ` `` → `reloadlua` → Enter → `` ` `` into the console. Don't run it
  while a text box (e.g. the Name window) has focus, because the keys would land in it.
- Mod errors show up in `runs/openmw-config/openmw.log` as
  `Can't start L@0x1[scripts/piagent/...]; Lua error: ...` or Lua tracebacks. The sidecar
  also echoes piagent errors to its stdout.
- **Agent shells:** processes started from a normal foreground agent shell are killed when
  the command returns, which looks like the game "crashing" silently. Run the sidecar (and
  anything long-lived) as a background job. The game is started by the sidecar in its own
  session, so it survives sidecar restarts, and the sidecar re-attaches via `pgrep`.
- `screencapture` refuses hidden file names like `.shot.png`.
- OpenMW Lua API reference ships in the app:
  `/Applications/OpenMW.app/Contents/Resources/resources/lua_api/openmw/*.lua`. Built-in
  scripts worth reading are under `resources/vfs/scripts/omw/`: `ui.lua` (UI modes and
  pause), `input/playercontrols.lua` (`I.Controls.override*`), `console/player.lua`.
- LuaJIT (Lua 5.1): `ipairs` and `#` do **not** work on OpenMW read-only proxy tables such as
  `I.UI.modes`. Use accessor functions (`I.UI.getMode()`).

## Current status (2026-09-30)

Verified end to end on the prison ship:

- Launch with the mod, videos skipped, harness config isolated from the user's game.
- Command channel in both directions, pause/unpause, nonce-matched replies.
- Harness-only state snapshot in the run log and web UI debug panel.
- Vision-only API: `/reset`, `/look` and `/act` return only `{run?, step}` or an argument
 error. The console key and removed ops (`walk_to`, `face`) are refused.
- Typing the name (`type "Nerevar"` + `key enter`) through the API. Clicking OK in a menu.
- Focus hand-back after launch and clicks (falls back to Finder).
- `turn` (relative yaw and pitch) steers toward a goal fixed at start, correcting part of the
 error each frame. It lands within 0.5° and clamps pitch at ±89°. The old one-shot
 `pitchChange` was applied twice.
- `activate` ignores statics, and activates an NPC under the crosshair (Jiub). During chargen
 the game disables activation until the tutorial box is dismissed, as it would for a
 player's `E` key.
- Chargen flow up to movement: the name window only opens after a few seconds of `wait`. Then
 `type` name, `key enter`, and after about 10 s of `wait` the "W and S move…" tutorial box
 appears. It is not a UI mode, but `click` on its Ok button works. That re-enables controls,
 and `move` forward/left works.
- `reload-lua` syntax-checks `mod/scripts/**/*.lua` with `luajit -bl` first (Homebrew
 `luajit`) and refuses to reload broken files.
- pi extension end to end with GPT-5.5, in print mode (`pi -a -p`): it starts the sidecar and
 a fresh game, then runs `look`, `act turn` and `report`. The model received the image and
 `step 1: turn degrees=30, screen change 19.5%`. `report` wrote `result.json` and ended the
 run. The provider request carried only `SYSTEM.md` and the three tools.
- First real run (GPT-5.5) left the ship at step 110. On the Hatch, `key space` through
 `/act` teleports within the key step itself, with the user's app in front. `key e` does
 nothing, since E is jump, and the tool description used to call activate "the E key".
 After the Lua `activate` op, the cell change only showed up during the next action.
- A stale sidecar keeps port 8342 and serves old code; new ones then die with "Address
 already in use". pi starts it by full path, so kill with `pkill -f 'game\.py'`.

Known issues / next fixes, in order:

1. More runs with the corrected key bindings in `.pi/SYSTEM.md`, then prompt tuning.
2. The Lua `activate` op: the teleport lands only in the next action, and the agent's first
 Hatch activation did nothing. Consider a longer settle, or remove the op and rely on
 `key space`. Verify `jump`, `stance` and `attack` in practice. The game still disables
 jumping, fighting and magic right after the tutorial box.
3. Clicks steal focus for about 0.5 s and move the user's real pointer (see "Menus" above).
 This is disruptive if the user works on the Mac during a run. Driving menus from Lua would
 fix it, if OpenMW's API can press engine menu buttons.
4. First-keystroke reliability: once, a console open/type sequence appeared to lose its
 first key. `reload-lua` has worked since. Keep an eye on it.

At handoff the sidecar (background job) and the game are running, on the Seyda Neen deck.

## Plan (remaining work)

1. First real run (see known issue 1), then tune `.pi/SYSTEM.md`. Keep it free of game
   facts: controls and harness behaviour only.
2. Optional, for unattended runs only: **`stream.py`** (pi `--mode json` events → terminal)
   and **`play.sh`** (`pi -a --mode json "$@" "Leave the prison ship." | tee runs/<run>/events.jsonl | .venv/bin/python -u stream.py`).
   The main interface stays the interactive pi app, where the user watches actions and
   reasoning.
3. `README.md` (setup, permissions, model choice), `git init`.
4. Later: save/resume between runs (menu scripts have `menu.saveGame/loadGame`), more
   milestones (census office, Balmora, Caius), cost/step logging.

## Ground rules

- Keep the harness small and readable; one sidecar file, one mod, one extension.
- Vision only: never add a path that gives the model game state beyond the screenshot.
 That includes tool results, error messages, system prompt facts and helper ops.
- Don't edit the user's own OpenMW config or saves; everything agent-related lives under
  `runs/`.
- No commits unless explicitly asked. Don't commit `runs/`, `.venv/`, or game data.
- Morrowind assets are copyrighted: never copy anything out of `Data Files` into the repo.
