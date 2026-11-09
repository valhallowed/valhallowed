import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, renameSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { IncompleteDocuments, InvalidDocuments, Reader } from "../server/documents.js";
import { CONTRACTS } from "./paths.js";

const GENERATION = { contract: 1, generation: 2, status_url: "http://control-plane:9457/status", status_interval_s: 5 };

function directory(): string {
  return mkdtempSync(join(tmpdir(), "web-map-"));
}

// Written whole, then moved into place, as the control plane writes them.
function write(dir: string, name: string, document: object): void {
  writeFileSync(join(dir, `${name}.tmp`), JSON.stringify(document));
  renameSync(join(dir, `${name}.tmp`), join(dir, name));
}

test("no generation yet reads as none", () => {
  assert.equal(new Reader("web-map", directory(), CONTRACTS).read(), undefined);
});

test("a generation and its secrets read together", () => {
  const dir = directory();
  write(dir, "secrets.json", { contract: 1, generation: 2 });
  write(dir, "generation.json", GENERATION);
  const read = new Reader("web-map", dir, CONTRACTS).read();
  assert.equal(read?.number, 2);
  assert.equal(read?.generation.status_interval_s, 5);
});

test("secrets for another generation, or none, wait", () => {
  const dir = directory();
  const reader = new Reader("web-map", dir, CONTRACTS);
  write(dir, "generation.json", GENERATION);
  assert.throws(() => reader.read(), IncompleteDocuments);
  write(dir, "secrets.json", { contract: 1, generation: 1 });
  assert.throws(() => reader.read(), IncompleteDocuments);
  write(dir, "secrets.json", { contract: 1, generation: 2 });
  assert.equal(reader.read()?.number, 2);
});

test("a generation that breaks its contract is refused, and says where", () => {
  const dir = directory();
  write(dir, "secrets.json", { contract: 1, generation: 2 });
  write(dir, "generation.json", { ...GENERATION, port: 8456 });
  assert.throws(() => new Reader("web-map", dir, CONTRACTS).read(), (error: unknown) => {
    return error instanceof InvalidDocuments && error.message.includes("additional properties");
  });
});

test("secrets that are not JSON are refused without their text", () => {
  const dir = directory();
  writeFileSync(join(dir, "secrets.json"), "hunter2-not-json");
  write(dir, "generation.json", GENERATION);
  assert.throws(() => new Reader("web-map", dir, CONTRACTS).read(), (error: unknown) => {
    return error instanceof InvalidDocuments && !error.message.includes("hunter2");
  });
});

test("a directory that shows other children's is refused", () => {
  const dir = directory();
  mkdirSync(join(dir, "intake"));
  assert.throws(() => new Reader("web-map", dir, CONTRACTS).read(), InvalidDocuments);
});
