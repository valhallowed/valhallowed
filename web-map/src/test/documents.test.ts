import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, renameSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { Adopter, IncompleteDocuments, InvalidDocuments, Reader } from "../server/documents.js";
import { CONTRACTS } from "./paths.js";

const GENERATION = {
  contract: 2,
  generation: 2,
  status_url: "http://control-plane:9457/status",
  status_interval_s: 5,
  database: { host: "database", port: 5432, name: "vhd", user: "web_map" },
};
const PASSWORD = "Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFy";

function secrets(generation: number): object {
  return { contract: 2, generation, password: PASSWORD };
}

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
  write(dir, "secrets.json", secrets(2));
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
  write(dir, "secrets.json", secrets(1));
  assert.throws(() => reader.read(), IncompleteDocuments);
  write(dir, "secrets.json", secrets(2));
  assert.equal(reader.read()?.number, 2);
});

test("a generation that breaks its contract is refused, and says where", () => {
  const dir = directory();
  write(dir, "secrets.json", secrets(2));
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

test("the reader constructs against the real contracts, and reads the database and password", () => {
  const dir = directory();
  write(dir, "secrets.json", secrets(2));
  write(dir, "generation.json", GENERATION);
  const read = new Reader("web-map", dir, CONTRACTS).read();
  assert.deepEqual(read?.generation.database, GENERATION.database);
  assert.equal(read?.secrets.password, PASSWORD);
});

test("the held documents do not show the password when serialized", () => {
  const dir = directory();
  write(dir, "secrets.json", secrets(2));
  write(dir, "generation.json", GENERATION);
  const read = new Reader("web-map", dir, CONTRACTS).read();
  assert.ok(!JSON.stringify(read).includes(PASSWORD));
});

test("a generation without a database is refused", () => {
  const dir = directory();
  const { database: _database, ...without } = GENERATION;
  write(dir, "secrets.json", secrets(2));
  write(dir, "generation.json", without);
  assert.throws(() => new Reader("web-map", dir, CONTRACTS).read(), (error: unknown) => {
    return error instanceof InvalidDocuments && error.message.includes("database");
  });
});

test("secrets without a password are refused", () => {
  const dir = directory();
  write(dir, "secrets.json", { contract: 2, generation: 2 });
  write(dir, "generation.json", GENERATION);
  assert.throws(() => new Reader("web-map", dir, CONTRACTS).read(), (error: unknown) => {
    return error instanceof InvalidDocuments && error.message.includes("password");
  });
});

test("a short password is refused, naming the rule and not the value", () => {
  const dir = directory();
  write(dir, "secrets.json", { contract: 2, generation: 2, password: "hunter2" });
  write(dir, "generation.json", GENERATION);
  assert.throws(() => new Reader("web-map", dir, CONTRACTS).read(), (error: unknown) => {
    return error instanceof InvalidDocuments && error.message.includes("pattern") && !error.message.includes("hunter2");
  });
});

test("a refused generation leaves the last good one adopted, logged once; a valid one replaces it", () => {
  const dir = directory();
  const lines: string[] = [];
  const held = new Adopter(new Reader("web-map", dir, CONTRACTS), (line) => lines.push(line));
  write(dir, "secrets.json", secrets(2));
  write(dir, "generation.json", GENERATION);
  held.adopt();
  assert.equal(held.documents?.number, 2);

  write(dir, "secrets.json", secrets(3));
  write(dir, "generation.json", { ...GENERATION, generation: 3, status_interval_s: 0 });
  held.adopt();
  held.adopt();
  assert.equal(held.documents?.number, 2);
  assert.equal(lines.filter((line) => line.startsWith("refused")).length, 1);

  write(dir, "secrets.json", secrets(4));
  write(dir, "generation.json", { ...GENERATION, generation: 4 });
  held.adopt();
  assert.equal(held.documents?.number, 4);
  assert.ok(lines.every((line) => !line.includes(PASSWORD)));
});
