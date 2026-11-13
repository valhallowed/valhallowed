import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { Ajv2020 } from "ajv/dist/2020.js";
import { Secret, type Documents } from "../server/documents.js";
import {
  CACHE_MS,
  grouped,
  Players,
  RETRY_FIRST_MS,
  RETRY_MAX_MS,
  Unavailable,
  type Connection,
  type Row,
} from "../server/players.js";
import { CONTRACTS } from "./paths.js";

function schema(file: string): object {
  return JSON.parse(readFileSync(join(CONTRACTS, file), "utf8")) as object;
}

// Strict mode takes the contract's string-or-null name only as a union type, and refuses the
// intake schema it refers to for allOf branches that name properties without a type; neither
// is a fault of what the web map answers.
const ajv = new Ajv2020({ strict: true, allowUnionTypes: true, strictTypes: false });
ajv.addSchema(schema("intake.events.schema.json"), "intake.events.schema.json");
const validate = ajv.compile(schema("web-map.players.schema.json"));

function documents(password = "pw", host = "db"): Documents {
  return {
    number: 1,
    generation: {
      contract: 1,
      generation: 1,
      status_url: "http://control-plane:9457/status",
      status_interval_s: 5,
      database: { host, port: 5432, name: "vhd", user: "web_map" },
    },
    secrets: { contract: 1, generation: 1, password: new Secret(password) },
  };
}

const ROWS: Row[] = [
  { world: "Alpha", name: "Asa", x: 1.5, y: 30, z: -4 },
  { world: "Alpha", name: null, x: 0, y: 0, z: 0 },
  { world: "Beta", name: "Bo", x: -100, y: 2, z: 99 },
];

/** A database that answers from rows, or fails, and counts what it was asked. */
class Fake {
  rows: Row[] = ROWS;
  failing: Error | undefined;
  asked: { text: string; values: unknown[] }[] = [];
  gate: Promise<void> | undefined;
  ended = false;

  connection(): Connection {
    return {
      query: async (text, values) => {
        this.asked.push({ text, values });
        await this.gate;
        if (this.failing !== undefined) {
          throw this.failing;
        }
        return this.rows;
      },
      end: async () => {
        this.ended = true;
      },
    };
  }
}

function players(fake: Fake = new Fake()): { players: Players; fake: Fake; logged: string[]; connects: Documents[] } {
  const logged: string[] = [];
  const connects: Documents[] = [];
  const instance = new Players(
    (line) => logged.push(line),
    (given) => {
      connects.push(given);
      return fake.connection();
    },
  );
  return { players: instance, fake, logged, connects };
}

async function running(): Promise<ReturnType<typeof players>> {
  const made = players();
  made.players.adopt(documents());
  await made.players.tick(0);
  assert.equal(made.players.status().state, "running");
  made.fake.asked.length = 0;
  return made;
}

test("the grouped answer meets the live players contract", () => {
  const answer = grouped(ROWS);
  assert.ok(validate(answer), JSON.stringify(validate.errors));
  assert.deepEqual(
    answer.worlds.map((world) => [world.name, world.players.length]),
    [
      ["Alpha", 2],
      ["Beta", 1],
    ],
  );
  assert.ok(validate(grouped([])), "no live player is no world");
});

test("no id can appear in the answer, whatever the row carries", () => {
  const leaky = [{ ...ROWS[0], id: 7, platform_id: "Steam_1", session_id: 3 }] as unknown as Row[];
  const answer = grouped(leaky);
  assert.ok(validate(answer), JSON.stringify(validate.errors));
  assert.deepEqual(Object.keys(answer.worlds[0]?.players[0] ?? {}).sort(), ["name", "x", "y", "z"]);
  const withId = { contract: 1, worlds: [{ name: "Alpha", players: [{ name: "Asa", x: 0, y: 0, z: 0, id: 7 }] }] };
  assert.ok(!validate(withId), "the contract itself refuses an id");
});

test("a name that is empty is no name", () => {
  const answer = grouped([{ world: "Alpha", name: "", x: 0, y: 0, z: 0 }]);
  assert.ok(validate(answer), JSON.stringify(validate.errors));
  assert.equal(answer.worlds[0]?.players[0]?.name, null);
});

test("a new sign-in starts again, the same sign-in does not", async () => {
  const { players: held, connects, fake } = await running();
  held.adopt(documents());
  assert.equal(connects.length, 1);
  held.adopt(documents("other"));
  assert.equal(connects.length, 2);
  assert.deepEqual(held.status(), { state: "starting", reason: "waiting for the database" });
  held.adopt(documents("other", "elsewhere"));
  assert.equal(connects.length, 3);
  assert.ok(fake.ended, "the old connection ends");
});

test("it reports starting until the database answers, then running", async () => {
  const { players: held, fake } = players();
  assert.equal(held.status().state, "starting");
  held.adopt(documents());
  fake.failing = new Error("refused");
  await held.tick(0);
  assert.deepEqual(held.status(), { state: "starting", reason: "cannot reach the database: refused" });
  fake.failing = undefined;
  await held.tick(RETRY_FIRST_MS);
  assert.deepEqual(held.status(), { state: "running" });
});

test("the wait between attempts doubles to its cap", async () => {
  const { players: held, fake } = players();
  held.adopt(documents());
  fake.failing = new Error("refused");
  let now = 0;
  const waits = [1000, 2000, 4000, 8000, 16_000, RETRY_MAX_MS, RETRY_MAX_MS];
  for (const wait of waits) {
    const before = fake.asked.length;
    await held.tick(now);
    assert.equal(fake.asked.length, before + 1, `asks at ${now}`);
    await held.tick(now + wait - 1);
    assert.equal(fake.asked.length, before + 1, `waits ${wait}`);
    now += wait;
  }
});

test("while the database is not reached, a read refuses and sends no query", async () => {
  const { players: held, fake } = players();
  await assert.rejects(held.read(0), Unavailable);
  held.adopt(documents());
  await assert.rejects(held.read(0), Unavailable);
  assert.equal(fake.asked.length, 0);
});

test("a read is the grouped rows", async () => {
  const { players: held } = await running();
  const answer = await held.read(0);
  assert.ok(validate(answer), JSON.stringify(validate.errors));
  assert.equal(answer.worlds.length, 2);
});

test("a failed read fails the state, refuses until the database answers again, and is not cached", async () => {
  const { players: held, fake } = await running();
  fake.failing = new Error("terminating connection");
  await assert.rejects(held.read(0), Unavailable);
  assert.deepEqual(held.status(), { state: "failing", reason: "cannot reach the database: terminating connection" });
  fake.asked.length = 0;
  await assert.rejects(held.read(1), Unavailable);
  assert.equal(fake.asked.length, 0, "a refusal costs no query");
  fake.failing = undefined;
  await held.tick(0);
  assert.equal(held.status().state, "running");
  assert.equal((await held.read(2)).worlds.length, 2);
});

test("readers at once share one query, and an answer serves for its cache time", async () => {
  const { players: held, fake } = await running();
  let release = (): void => undefined;
  fake.gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const together = Promise.all([held.read(0), held.read(0), held.read(0)]);
  release();
  const answers = await together;
  assert.equal(fake.asked.length, 1);
  assert.equal(answers[0], answers[2]);
  await held.read(CACHE_MS - 1);
  assert.equal(fake.asked.length, 1);
  await held.read(CACHE_MS);
  assert.equal(fake.asked.length, 2);
});

test("every statement is parameterized text the web map wrote", async () => {
  const { players: held, fake } = await running();
  await held.read(0);
  assert.ok(fake.asked.every(({ text }) => !/\$\{|\+/.test(text) && text.includes("live_player")));
});
