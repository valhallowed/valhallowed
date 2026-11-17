import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Ajv2020 } from "ajv/dist/2020.js";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";
import { MapRenders, metaValidator } from "../server/mapexport.js";
import { readPalette } from "../server/palette.js";
import type { LivePlayers } from "../server/players.js";
import { assets, handler } from "../server/routes.js";
import { CONTRACTS, DESIGN, EXAMPLES, LEAFLET, PAGE, PUBLIC } from "./paths.js";

const LIVE: LivePlayers = { contract: 1, worlds: [{ name: "Alpha", players: [{ name: "Asa", x: 1, y: 2, z: 3 }] }] };
// Meadowfold exports the synthetic map (seed Hildir, value 1234567); Quietfield has no export yet.
const RENDERED = "Meadowfold";
const UNRENDERED = "Quietfield";
const SEED = ["Hildir", "1234567"];
const POLICY = "default-src 'self'; frame-ancestors 'none'";
let server: Server;
let base: string;
let scratch: string;
let renders: MapRenders;
let players: () => Promise<LivePlayers> = async () => LIVE;

before(async () => {
  scratch = mkdtempSync(join(tmpdir(), "routes-"));
  const exported = join(scratch, "export");
  mkdirSync(exported);
  for (const file of ["json", "raster.bin"]) {
    copyFileSync(join(EXAMPLES, `plugin.map-export-synthetic.${file}`), join(exported, file === "json" ? "meta.json" : "raster.bin"));
  }
  renders = new MapRenders(metaValidator(CONTRACTS), readPalette(join(DESIGN, "tokens.css")), () => undefined);
  renders.adopt({
    generation: {
      worlds: [
        { name: RENDERED, map_export: exported },
        { name: UNRENDERED, map_export: join(scratch, "absent") },
      ],
    },
  } as never);
  await renders.poll();
  server = createServer(handler("abc1234", assets({ public: PUBLIC, page: PAGE, leaflet: LEAFLET, design: DESIGN }), () => players(), renders));
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

after(() => {
  server.close();
  rmSync(scratch, { recursive: true, force: true });
});

test("/build is the commit the image was built from", async () => {
  const response = await fetch(`${base}/build`);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "abc1234\n");
});

test("the page, its styles, the design tokens and their fonts are served", async () => {
  for (const [path, type] of [
    ["/", "text/html"],
    ["/web-map.css", "text/css"],
    ["/web-map.js", "text/javascript"],
    ["/leaflet/leaflet.js", "text/javascript"],
    ["/leaflet/leaflet.css", "text/css"],
    ["/design/tokens.css", "text/css"],
    ["/design/fonts/alegreya-sans-400.woff2", "font/woff2"],
  ] as const) {
    const response = await fetch(`${base}${path}`);
    assert.equal(response.status, 200, path);
    assert.ok(response.headers.get("content-type")?.startsWith(type), path);
    assert.ok(response.headers.get("content-security-policy")?.includes("default-src 'self'"), path);
  }
});

test("the page carries the trademark line", async () => {
  const page = await (await fetch(`${base}/`)).text();
  assert.ok(page.includes("Valheim is a trademark of Iron Gate AB."));
});

test("nothing outside the allow-list is served, a path that climbs included", async () => {
  for (const path of [
    "/leaflet/",
    "/leaflet/LICENSE",
    "/leaflet/leaflet-src.js",
    "/leaflet/leaflet.js.map",
    "/leaflet/images/layers.png",
    "/leaflet/../package.json",
    "/index.html",
    "/design/fonts/OFL-alegreya-sans.txt",
    "/../package.json",
    "/%2e%2e/package.json",
  ]) {
    assert.equal((await fetch(`${base}${path}`)).status, 404, path);
  }
});

test("only GET and HEAD are answered", async () => {
  const response = await fetch(`${base}/build`, { method: "POST", body: "x" });
  assert.equal(response.status, 405);
  assert.equal(response.headers.get("allow"), "GET, HEAD");
  const head = await fetch(`${base}/build`, { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
});

test("/api/players is the live players, never stored", async () => {
  players = async () => LIVE;
  const response = await fetch(`${base}/api/players`);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/json; charset=utf-8");
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.ok(response.headers.get("content-security-policy")?.includes("default-src 'self'"));
  assert.deepEqual(await response.json(), LIVE);
  const head = await fetch(`${base}/api/players`, { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
});

test("/api/players is a plain 503 that says nothing of why, whether it refuses or throws", async () => {
  for (const failure of [
    () => Promise.reject(new Error("password authentication failed for user web_map")),
    () => {
      throw new Error("thrown at once");
    },
  ]) {
    players = failure;
    const response = await fetch(`${base}/api/players`);
    assert.equal(response.status, 503);
    assert.equal(response.headers.get("content-type"), "text/plain; charset=utf-8");
    assert.equal(response.headers.get("cache-control"), "no-store");
    assert.equal(await response.text(), "the live players are unavailable\n");
  }
  players = async () => LIVE;
});

test("/api/worlds is B11's worlds document: geometry and version, null before a render, no database", async () => {
  players = () => Promise.reject(new Error("no database"));
  const response = await fetch(`${base}/api/worlds`);
  players = async () => LIVE;
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/json; charset=utf-8");
  assert.equal(response.headers.get("cache-control"), "no-store");
  const body = await response.text();
  const schema = JSON.parse(readFileSync(join(CONTRACTS, "web-map.worlds.schema.json"), "utf8")) as object;
  const document = JSON.parse(body) as { worlds: { name: string; render: { version: string } | null }[] };
  const validate = new Ajv2020({ strict: true }).compile(schema);
  assert.ok(validate(document), JSON.stringify(validate.errors));
  assert.deepEqual(document.worlds.map((world) => world.name), [RENDERED, UNRENDERED]);
  const png = Buffer.from(await (await fetch(`${base}/api/worlds/${RENDERED}/render.png`)).arrayBuffer());
  assert.deepEqual(document.worlds[0]?.render, {
    size: 16,
    half_width: 10500,
    version: createHash("sha256").update(png).digest("hex").slice(0, 16),
  });
  assert.equal(document.worlds[1]?.render, null);
  assert.ok(!/water|seed/.test(body));
});

test("a world's render is its cached PNG, revalidated by its version", async () => {
  const url = `${base}/api/worlds/${RENDERED}/render.png`;
  const response = await fetch(url);
  const png = Buffer.from(await response.arrayBuffer());
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "image/png");
  assert.equal(response.headers.get("cache-control"), "no-cache");
  assert.deepEqual([...png.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const etag = response.headers.get("etag");
  assert.equal(etag, `"${createHash("sha256").update(png).digest("hex").slice(0, 16)}"`);
  const same = await fetch(url, { headers: { "If-None-Match": etag ?? "" } });
  assert.equal(same.status, 304);
  assert.equal(await same.text(), "");
  assert.equal(same.headers.get("etag"), etag);
  assert.equal((await fetch(url, { headers: { "If-None-Match": '"0000000000000000"' } })).status, 200);
  const head = await fetch(url, { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(head.headers.get("content-length"), String(png.length));
});

test("an unknown, unrendered or malformed world name is a 404", async () => {
  for (const path of [
    `/api/worlds/${UNRENDERED}/render.png`,
    "/api/worlds/Nowhere/render.png",
    "/api/worlds/meadowfold/render.png",
    "/api/worlds//render.png",
    "/api/worlds/%E0%A4%A/render.png",
    "/api/worlds/%2e%2e/render.png",
    `/api/worlds/${RENDERED}%2F..%2F${RENDERED}/render.png`,
    `/api/worlds/${RENDERED}/render.png/`,
    `/api/worlds/${RENDERED}/raster.bin`,
    `/api/worlds/${RENDERED}`,
    "/api/worlds/",
    "/api/worlds/..%2f..%2fetc%2fpasswd/render.png",
  ]) {
    const response = await fetch(`${base}${path}`);
    assert.equal(response.status, 404, path);
    assert.equal(response.headers.get("content-type"), "text/plain; charset=utf-8", path);
  }
});

test("the seed is in no response", async () => {
  const bodies: Buffer[] = [];
  for (const path of ["/", "/web-map.js", "/api/players", "/api/worlds", `/api/worlds/${RENDERED}/render.png`, "/api/worlds/Nowhere/render.png", "/build"]) {
    bodies.push(Buffer.from(await (await fetch(`${base}${path}`)).arrayBuffer()));
  }
  for (const word of SEED) {
    assert.ok(bodies.every((body) => !body.includes(word)), word);
  }
});

test("the content security policy is the same on every route, an error and a 304 included", async () => {
  const etag = (await fetch(`${base}/api/worlds/${RENDERED}/render.png`)).headers.get("etag") ?? "";
  const requests: [string, RequestInit?][] = [
    ["/"],
    ["/web-map.css"],
    ["/web-map.js"],
    ["/design/tokens.css"],
    ["/build"],
    ["/api/players"],
    ["/api/worlds"],
    [`/api/worlds/${RENDERED}/render.png`],
    [`/api/worlds/${RENDERED}/render.png`, { headers: { "If-None-Match": etag } }],
    ["/api/worlds/Nowhere/render.png"],
    ["/nothing"],
    ["/build", { method: "POST", body: "x" }],
  ];
  for (const [path, init] of requests) {
    const response = await fetch(`${base}${path}`, init);
    assert.equal(response.headers.get("content-security-policy"), POLICY, `${init?.method ?? "GET"} ${path} ${response.status}`);
    assert.equal(response.headers.get("x-content-type-options"), "nosniff", path);
  }
});
