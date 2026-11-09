import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { Ajv2020 } from "ajv/dist/2020.js";
import { MAX_REASON, reason, report, Reporter } from "../server/status.js";
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
