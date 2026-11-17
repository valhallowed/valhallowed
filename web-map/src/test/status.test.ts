import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { Ajv2020 } from "ajv/dist/2020.js";
import { MAX_REASON, reason, report, Reporter, withProblems } from "../server/status.js";
import { CONTRACTS } from "./paths.js";

// The contract requires reason inside an if/then, which ajv's strict mode flags as a style choice.
const validate = new Ajv2020({ strict: true, strictRequired: false }).compile(
  JSON.parse(readFileSync(join(CONTRACTS, "service.status.schema.json"), "utf8")) as object,
);

test("every report the web map makes meets the status contract", () => {
  for (const status of [report(3, "running"), report(3, "running", "ignored"), report(1, "failing", "bad generation")]) {
    assert.ok(validate(status), JSON.stringify(validate.errors));
  }
  assert.equal(report(3, "running", "ignored").reason, undefined);
});

test("a reason is one printable line, cut to the contract's length", () => {
  assert.equal(reason("two\nlines\u0007"), "two lines");
  assert.equal(reason(""), "no reason given");
  assert.equal(reason("x".repeat(600)).length, MAX_REASON);
});

test("a failing report is logged once, and its recovery once", async () => {
  const logged: string[] = [];
  let up = false;
  const reporter = new Reporter(
    (line) => logged.push(line),
    async () => {
      if (!up) throw new Error("refused");
    },
  );
  const status = report(1, "running");
  await reporter.send("http://control-plane:9457/status", status);
  await reporter.send("http://control-plane:9457/status", status);
  up = true;
  await reporter.send("http://control-plane:9457/status", status);
  assert.deepEqual(
    logged.map((line) => line.split(":")[0]),
    ["status {\"contract\"", "cannot report status to http", "status reports reach http"],
  );
});

test("a refused map export fails the report with its reason, and a healthy one changes nothing", () => {
  assert.deepEqual(withProblems("running", undefined, []), { state: "running" });
  assert.deepEqual(withProblems("starting", "waiting for the database", []), { state: "starting", reason: "waiting for the database" });
  const joined = withProblems("running", undefined, [
    { world: "A", reason: "x" },
    { world: "B", reason: "y" },
  ]);
  assert.deepEqual(joined, { state: "failing", reason: "world A: map export refused: x; world B: map export refused: y" });
  assert.ok(validate(report(2, joined.state, joined.reason)), JSON.stringify(validate.errors));
  assert.equal(withProblems("starting", "waiting for the database", [{ world: "A", reason: "p" }]).reason, "waiting for the database; world A: map export refused: p");
});

test("a reason too long for the contract leads with the count and every refused world's name", () => {
  const problems = ["Alpha", "Bravo", "Charlie"].map((world) => ({ world, reason: "r".repeat(300) }));
  const cut = withProblems("running", undefined, problems);
  assert.ok(cut.reason?.startsWith("map exports refused for 3 worlds: Alpha, Bravo, Charlie; world Alpha"));
  assert.equal(cut.reason?.length, MAX_REASON);
  assert.ok(validate(report(2, cut.state, cut.reason)));
  assert.ok(withProblems("running", undefined, [{ world: "A", reason: "r".repeat(600) }]).reason?.startsWith("map export refused for 1 world: A; "));
});
