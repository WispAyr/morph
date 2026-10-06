// Record what crosspointd's automatic rules engine decides on one tick.
//
// Usage: node tools/crosspointd_rules_oracle.mjs /path/to/crosspoint | gzip -n -9 > tests/fixtures/crosspointd_rule_decisions.jsonl.gz
//
// The first line is a header naming the Crosspoint commit. Every other line is one scenario: the MORPH context it
// corresponds to, and what Core.runRules did for one rule and its destination: the command it sent, if any, and the
// status it set (level and kind). The inputs are the state runRules reads: the rule, the destination's derived
// fields (on air, offline, in flight, what it shows), whether a live matching source exists, whether the source on
// the destination is live, whether this rule made the current route, and recent failed attempts. Lock derivation
// is covered by the manual take/release oracle; here the derived fields are set directly. Only sendCommand is
// stubbed.
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const root = resolve(process.argv[2] || "../crosspoint");
const { Core } = await import(pathToFileURL(join(root, "server/src/core.mjs")).href);
const commit = execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
const dataDir = mkdtempSync(join(tmpdir(), "crosspointd-rules-oracle-"));

// Status text → kind. Order matters: the more specific patterns first.
const KINDS = [
  [/^disabled$/, "disabled"],
  [/rules may only target local destinations/, "peer_destination"],
  [/^destination .* is (offline|not registered)$/, "destination_unavailable"],
  [/operator-only/, "operator_only"],
  [/^release waiting: .* is on air$/, "release_waiting_on_air"],
  [/^waiting: destination on air/, "queued_on_air"],
  [/\(on air\)$/, "on_air_routed"],
  [/^idle$/, "on_air_idle"],
  [/^command in flight$/, "in_flight"],
  [/^take failed: .* retrying$/, "take_retrying"],
  [/^release failed: .* retrying$/, "release_retrying"],
  [/^taking /, "take"],
  [/^releasing /, "release"],
  [/^waiting: .* busy with /, "busy"],
  [/^idle: no matching live source$/, "idle_no_source"],
  [/ → /, "already_routed"],
];

function kindOf(text) {
  const match = KINDS.find(([pattern]) => pattern.test(text));
  if (!match) throw new Error(`unclassified rule status: ${text}`);
  return match[1];
}

function decide({ rule, destination, candidate, current_source, route, retry }) {
  const core = new Core({ node: { id: "studio" }, dataDir, hub: { rtsp: "rtsp://hub", whep: "http://hub" }, hubPoller: null });
  core.closed = true;
  const r = { id: "r1", name: "Callers to CAM 5", enabled: rule.enabled, match: { kind: "caller" }, destination: "studio/cam5", policy: rule.policy, release: rule.release };
  core.rules = [r];

  const now = Date.now();
  const source = (id, kind, live) => {
    const s = core._source("owner", { id, kind }, null);
    s.live = live; s.createdAt = now - 1000;
    core.sources.set(s.id, s);
    return s;
  };
  const cand = candidate.present ? source("caller1", "caller", true) : null;
  const other = destination.current === "other" ? source("cam1", "camera", current_source.live) : null;

  if (destination.registered) {
    const d = core._dest("owner", { id: "cam5", kind: "sd-slot" }, null);
    d.locked = destination.on_air;
    if (destination.offline) d.offlineSince = now;
    if (destination.peer) d.origin = { node: "van", link: "up" };
    d.operatorOnly = destination.operator_only;
    d.current = destination.current === "candidate" ? cand.id : destination.current === "other" ? other.id : null;
    core.destinations.set(d.id, d);
    if (route.mine) core.routes.set(d.id, { destination: d.id, source: d.current, by: "rule:r1", at: now });
    if (destination.command_in_flight) core.pending.set("in-flight", { id: "in-flight", destination: d.id });
  }

  const st = core.rs(r);
  if (cand && retry.take_failed_recently) st.lastTry.set(`take|studio/cam5|${cand.id}`, { at: now, error: "router busy" });
  if (retry.release_failed_recently) st.lastTry.set("release|studio/cam5", { at: now, error: "router busy" });

  const commands = [];
  core.sendCommand = (command) => { commands.push(command); return new Promise(() => {}); };
  core.runRules();
  if (commands.length > 1) throw new Error("more than one command in one tick");
  return { command: commands[0]?.action ?? null, level: st.status.level, kind: kindOf(st.status.text) };
}

function* scenarios() {
  const flags = [false, true];
  const destinations = [
    { registered: true, offline: false, peer: false, operator_only: false },
    { registered: false, offline: false, peer: false, operator_only: false },
    { registered: true, offline: true, peer: false, operator_only: false },
    { registered: true, offline: false, peer: true, operator_only: false },
    { registered: true, offline: false, peer: false, operator_only: true },
  ];
  for (const enabled of flags) for (const policy of ["if-free", "replace"]) for (const release of flags)
    for (const base of destinations) for (const on_air of flags) for (const command_in_flight of flags)
      for (const present of flags) for (const current of present ? ["none", "candidate", "other"] : ["none", "other"])
        for (const live of current === "other" ? flags : [false])
          for (const mine of current === "none" ? [false] : flags)
            for (const take_failed_recently of present ? flags : [false]) for (const release_failed_recently of flags)
              yield {
                rule: { enabled, policy, release },
                destination: { ...base, on_air, command_in_flight, current },
                candidate: { present },
                current_source: { live },
                route: { mine },
                retry: { take_failed_recently, release_failed_recently },
              };
}

process.stdout.write(JSON.stringify({ crosspoint_commit: commit, generator: "tools/crosspointd_rules_oracle.mjs" }) + "\n");
let count = 0;
for (const scenario of scenarios()) {
  process.stdout.write(JSON.stringify({ ...scenario, outcome: decide(scenario) }) + "\n");
  count += 1;
}
rmSync(dataDir, { recursive: true, force: true });
process.stderr.write(`${count} rule scenarios from crosspoint ${commit}\n`);
