/**
 * morrowind.ts - pi plays Morrowind: screenshots in, keyboard and mouse out.
 *
 * The sidecar (game.py) owns OpenMW and the web UI. This extension starts it
 * on demand and points it at a fresh run directory, so playing is just:
 *   pi -a "Leave the prison ship."
 *
 * Vision only: tool results carry the screenshot, the step number and how much
 * the screen changed. Nothing else from the sidecar reaches the model, and the
 * system prompt is forced to .pi/SYSTEM.md so no context files (AGENTS.md is
 * full of game facts) or skills leak in.
 *
 * Environment:
 *   GAME_URL  sidecar address (default http://127.0.0.1:8342)
 *   RUN_NAME  evidence directory under runs/ (default: run-<timestamp>)
 *   PI_FRESH  set to restart OpenMW with a new game instead of attaching to
 *             the running one
 *   PI_DUMP_REQUEST  file to write the latest provider request to, to audit
 *             exactly what the model receives
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const ROOT = join(fileURLToPath(import.meta.url), "..", "..", "..");
const GAME = process.env.GAME_URL ?? "http://127.0.0.1:8342";
// PI_PAUSE=1 freezes the world between actions (the sidecar inherits this env).
const PAUSED = process.env.PI_PAUSE === "1";
const TIMING = PAUSED
  ? "- The game is paused between your actions, so take your time. Time only passes while an\n" +
    "  action runs. Scripted scenes, characters walking and dialogue that plays out on its\n" +
    "  own only advance during actions, so use `act` with `wait` when you expect the game to\n" +
    "  do something on its own."
  : "- The game runs in real time and keeps going while you think: characters move and\n" +
    "  scripted scenes play on. A screenshot shows the moment it was taken, so things may\n" +
    "  have moved on by the time your next action runs. Use `act` with `wait` to let time\n" +
    "  pass without doing anything.";
const SYSTEM_PROMPT = readFileSync(join(ROOT, ".pi", "SYSTEM.md"), "utf8").replace("{{timing}}", TIMING);

let chain: Promise<unknown> = Promise.resolve();
let ready: Promise<void> | null = null;
let runDir = "";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function up(): Promise<boolean> {
  try {
    return (await fetch(`${GAME}/status`)).ok;
  } catch {
    return false;
  }
}

// The first tool call in a session starts the sidecar if needed (it launches
// or attaches to OpenMW) and resets it into this session's run directory.
function ensureGame(): Promise<void> {
  if (ready) return ready;
  ready = (async () => {
    if (!(await up())) {
      mkdirSync(join(ROOT, "runs"), { recursive: true });
      const fd = openSync(join(ROOT, "runs", "sidecar.log"), "a");
      const python = existsSync(join(ROOT, ".venv/bin/python")) ? join(ROOT, ".venv/bin/python") : "python3";
      const child = spawn(python, ["-u", join(ROOT, "game.py")], {
        cwd: ROOT, detached: true, stdio: ["ignore", fd, fd],
      });
      child.unref();
      for (let i = 0; i < 100 && !(await up()); i++) await sleep(200);
      if (!(await up())) throw new Error(`game sidecar did not start - see ${join(ROOT, "runs", "sidecar.log")}`);
    }
    const name = process.env.RUN_NAME ?? `run-${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)}`;
    const fresh = process.env.PI_FRESH ? "&fresh=1" : "";
    const res = await fetch(`${GAME}/reset?name=${encodeURIComponent(name)}${fresh}`, { method: "POST" });
    const body = await res.json() as { run?: string; error?: string };
    if (!res.ok || !body.run) throw new Error(`game reset failed: ${body.error ?? res.status}`);
    runDir = join(ROOT, "runs", body.run);
  })();
  ready.catch(() => { ready = null; });  // let the next tool call retry
  return ready;
}

async function screen(): Promise<{ type: "image"; data: string; mimeType: string }> {
  const res = await fetch(`${GAME}/screen.jpg`);
  if (!res.ok) throw new Error(`screenshot failed: ${res.status}`);
  const data = Buffer.from(await res.arrayBuffer()).toString("base64");
  return { type: "image", data, mimeType: "image/jpeg" };
}

async function post(path: string, body?: object): Promise<any> {
  const res = await fetch(`${GAME}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json() as { error?: string };
  if (!res.ok || json.error) throw new Error(json.error ?? `sidecar error ${res.status}`);
  return json;
}

// Tool calls run one at a time, in order, even if the model issues several.
function serial<T>(fn: () => Promise<T>): Promise<T> {
  const run = chain.then(fn);
  chain = run.catch(() => undefined);
  return run;
}

const lit = <T extends string>(values: readonly T[], description: string) =>
  Type.Union(values.map((v) => Type.Literal(v)), { description });

const ACTIONS = ["move", "turn", "activate", "jump", "stance", "attack", "wait", "click", "type", "key"] as const;

const look = defineTool({
  name: "look",
  label: "Look",
  description: "Return the current screen without doing anything.",
  parameters: Type.Object({}),

  async execute() {
    return serial(async () => {
      await ensureGame();
      const { step } = await post("/look");
      return {
        content: [{ type: "text" as const, text: `Current screen (step ${step}):` }, await screen()],
        details: { step },
      };
    });
  },
});

const act = defineTool({
  name: "act",
  label: "Act",
  description:
    "Do one thing in the game, then return the new screen and how much of it changed. " +
    (PAUSED
      ? "The game is paused between actions; time only passes while an action runs. "
      : "The game keeps running between actions. ") +
    "Actions: move {dir, seconds?, run?} walk or run; turn {degrees?, pitch?} turn the view " +
    "(degrees: + right, - left; pitch: + look down, - look up; both relative); " +
    "activate - use/talk to/open what is under the crosshair (the Space key); jump; " +
    "stance {stance} ready a weapon or spell, or put it away; attack {seconds?} attack or cast " +
    "with the readied weapon/spell (seconds = how long to hold for a power attack); " +
    "wait {seconds} let time pass without input; " +
    "click {x, y, button?, double?} click at screenshot pixel coordinates (for menus and dialog " +
    "boxes; in the world a left click attacks with whatever is readied); type {text} type text into the focused text field; " +
    "key {key, times?} press a key. After click, type and key, 0.4 s (or `seconds`) passes so the game can react.",
  parameters: Type.Object({
    action: lit(ACTIONS, "What to do"),
    dir: Type.Optional(lit(["forward", "back", "left", "right"] as const, "move: direction")),
    seconds: Type.Optional(Type.Number({ description: "move: 0.1-10 (default 1); wait: 0-15; attack: 0.05-3; click/type/key: settle time" })),
    run: Type.Optional(Type.Boolean({ description: "move: run instead of walk (default true)" })),
    degrees: Type.Optional(Type.Number({ description: "turn: -180..180, + = right" })),
    pitch: Type.Optional(Type.Number({ description: "turn: + = look down, - = look up" })),
    stance: Type.Optional(lit(["weapon", "spell", "nothing"] as const, "stance: what to ready")),
    x: Type.Optional(Type.Number({ description: "click: x in screenshot pixels" })),
    y: Type.Optional(Type.Number({ description: "click: y in screenshot pixels" })),
    button: Type.Optional(lit(["left", "right"] as const, "click: mouse button (default left)")),
    double: Type.Optional(Type.Boolean({ description: "click: double-click" })),
    text: Type.Optional(Type.String({ description: "type: text to type (max 200 chars)" })),
    key: Type.Optional(Type.String({
      description: "key: a letter, digit, enter, escape, tab, space, backspace, delete, up, down, left, right, " +
        "home, end, pageup, pagedown or f1-f12",
    })),
    times: Type.Optional(Type.Integer({ description: "key: presses (default 1)", minimum: 1, maximum: 10 })),
  }),

  async execute(_id, params) {
    return serial(async () => {
      await ensureGame();
      const { step, changed } = await post("/act", params);
      const args = Object.entries(params)
        .filter(([k, v]) => k !== "action" && v !== undefined)
        .map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : v}`)
        .join(" ");
      const pct = (changed * 100).toFixed(1);
      return {
        content: [
          { type: "text" as const, text: `step ${step}: ${params.action}${args ? " " + args : ""}, screen change ${pct}%` },
          await screen(),
        ],
        details: { step, changed },
      };
    });
  },
});

const report = defineTool({
  name: "report",
  label: "Report",
  description:
    "Record your verdict and end the run. Set done=true only when the goal you were given is " +
    "achieved. Call it the moment that happens, or with done=false and a note on how far you " +
    "got when you decide to stop.",
  parameters: Type.Object({
    done: Type.Boolean({ description: "True only when the goal is achieved" }),
    note: Type.String({ description: "One short sentence on what happened during the run" }),
  }),

  async execute(_id, params) {
    return serial(async () => {
      await ensureGame();
      // Harness-side facts go into result.json only, never into the tool result.
      const status = await (await fetch(`${GAME}/status`)).json() as { step: number; milestones: string[] };
      const outcome = {
        step: status.step, done: params.done, note: params.note,
        milestones: status.milestones, ts: new Date().toISOString(),
      };
      writeFileSync(join(runDir, "result.json"), JSON.stringify(outcome, null, 2));
      return {
        content: [{ type: "text" as const, text: `Recorded: done=${params.done} at step ${status.step}. Run over.` }],
        details: { step: status.step, done: params.done },
        terminate: true,
      };
    });
  },
});

export default function (pi: ExtensionAPI) {
  pi.registerTool(look);
  pi.registerTool(act);
  pi.registerTool(report);

  pi.on("session_start", () => {
    pi.setActiveTools(["look", "act", "report"]);
    chain = Promise.resolve();
    ready = null;
  });

  pi.on("before_agent_start", (event) => {
    event.systemPromptOptions.contextFiles = [];
    event.systemPromptOptions.skills = [];
    event.systemPromptOptions.appendSystemPrompt = "";
    return { systemPrompt: SYSTEM_PROMPT };
  });

  pi.on("before_provider_request", (event) => {
    if (process.env.PI_DUMP_REQUEST) writeFileSync(process.env.PI_DUMP_REQUEST, JSON.stringify(event.payload));
  });
}
