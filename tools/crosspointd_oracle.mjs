// Record what crosspointd actually decides for manual take / release requests.
//
// Usage: node tools/crosspointd_oracle.mjs /path/to/crosspoint [--shadow LOG] | gzip -n -9 > tests/fixtures/crosspointd_manual_decisions.jsonl.gz
//
// The first line is a header naming the Crosspoint commit. Every other line is one scenario: the MORPH context
// the scenario corresponds to, and the outcome crosspointd's own Core.manualTake / manualRelease produced for it.
// Only the I/O edges are stubbed: sending a command to an owner, and forwarding to a peer node. Everything that
// decides the outcome (normalisation, recompute's lock derivation, the checks and their order) is Crosspoint's code.
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const root = resolve(process.argv[2] || "../crosspoint");
// --shadow LOG: also attach crosspointd's own decision log (server/src/decisionlog.mjs) to every Core and drive each
// scenario through it, so tools/shadow_check.py can be checked end to end against the log crosspointd itself writes.
const shadowAt = process.argv.indexOf("--shadow");
const shadowPath = shadowAt > 0 ? resolve(process.argv[shadowAt + 1]) : null;
const attachDecisionLog = shadowPath ? (await import(pathToFileURL(join(root, "server/src/decisionlog.mjs")).href)).attachDecisionLog : null;
const { Core } = await import(pathToFileURL(join(root, "server/src/core.mjs")).href);
const commit = execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim();

const NODE = "studio";
const PEER = "van";
const dataDir = mkdtempSync(join(tmpdir(), "crosspointd-oracle-"));

const ERRORS = [
  [/is a screen — it shows layouts/, "screen_needs_layout"],
  [/does not accept/, "not_accepted"],
  [/^not-granted:/, "pull_not_granted"],
  [/is ON AIR/, "on_air"],
  [/already in flight/, "in_flight"],
];

function outcomeOfError(error) {
  const match = ERRORS.find(([pattern]) => pattern.test(error.message));
  if (!match) throw new Error(`unclassified crosspointd error: ${error.message}`);
  return { status: "deny", reason: match[1] };
}

async function decide(scenario) {
  const core = new Core({ node: { id: NODE }, dataDir, hub: { rtsp: "rtsp://hub", whep: "http://hub" }, hubPoller: null });
  core.closed = true;   // never write config or events to disk
  const { request, destination, source } = scenario;

  const d = core._dest("owner", {
    id: destination.peer ? `${PEER}/dest` : "dest",
    kind: destination.kind, accepts: destination.accepts, onProgram: destination.on_program, operatorOnly: destination.operator_only,
  }, null);
  if (destination.peer) { d.id = `${PEER}/dest`; d.origin = { node: PEER, link: "up" }; d.originLocked = destination.on_program; d.tallyLocal = {}; }
  core.destinations.set(d.id, d);

  const s = core._source("owner", { id: source.peer ? `${PEER}/src` : "src", kind: source.kind }, null);
  if (source.peer) { s.id = `${PEER}/src`; s.origin = { node: PEER, link: "up", grants: source.pull_granted ? ["pull"] : [] }; }
  core.sources.set(s.id, s);

  if (destination.program_tally) core.tallyReports.set("switcher", { program: new Set([d.id]), preview: new Set(), display: new Set() });
  if (destination.command_in_flight) core.pending.set("in-flight", { id: "in-flight", destination: d.id });

  core.sendCommand = async (command) => ({ command });
  core.remote = { command: async ({ action }) => ({ forwarded: action }) };
  const shadow = attachDecisionLog?.(core, shadowPath);

  try {
    let result;
    try {
      result = request.action === "take"
        ? await core.manualTake({ destination: d.id, source: s.id, force: request.force })
        : await core.manualRelease({ destination: d.id, force: request.force });
    } catch (error) {
      if (!error.status) throw error;
      return outcomeOfError(error);
    }
    if (result.forwarded) return { status: "allow", action: `forward_${result.forwarded}` };
    const { action, force, extra } = result.command;
    if (action === "propose") return { status: "allow", action: `propose_${extra.propose}` };
    return { status: "allow", action: force ? `forced_${action}` : action };
  } finally {
    await shadow?.flush();   // every scenario's log line lands before the next scenario starts
  }
}

function* scenarios() {
  const flags = [false, true];
  const destinations = [];
  for (const kind of ["sd-slot", "screen"]) for (const accepts of [[], ["camera"]]) for (const operator_only of flags)
    for (const peer of flags) for (const on_program of flags) for (const program_tally of flags) for (const command_in_flight of flags)
      destinations.push({ kind, accepts, operator_only, peer, on_program, program_tally, command_in_flight });
  const sources = [];
  for (const kind of ["camera", "caller", "composition"]) {
    sources.push({ kind, peer: false, pull_granted: true });
    for (const pull_granted of flags) sources.push({ kind, peer: true, pull_granted });
  }
  for (const force of flags) for (const destination of destinations) {
    for (const source of sources) yield { request: { action: "take", force }, destination, source };
    yield { request: { action: "release", force }, destination, source: sources[0] };
  }
}

process.stdout.write(JSON.stringify({ crosspoint_commit: commit, generator: "tools/crosspointd_oracle.mjs" }) + "\n");
let count = 0;
for (const scenario of scenarios()) {
  process.stdout.write(JSON.stringify({ ...scenario, outcome: await decide(scenario) }) + "\n");
  count += 1;
}
rmSync(dataDir, { recursive: true, force: true });
process.stderr.write(`${count} scenarios from crosspoint ${commit}\n`);
