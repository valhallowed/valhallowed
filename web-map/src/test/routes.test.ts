import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";
import type { LivePlayers } from "../server/players.js";
import { assets, handler } from "../server/routes.js";
import { DESIGN, LEAFLET, PAGE, PUBLIC } from "./paths.js";

const LIVE: LivePlayers = { contract: 1, worlds: [{ name: "Alpha", players: [{ name: "Asa", x: 1, y: 2, z: 3 }] }] };
let server: Server;
let base: string;
let players: () => Promise<LivePlayers> = async () => LIVE;

before(async () => {
  server = createServer(handler("abc1234", assets({ public: PUBLIC, page: PAGE, leaflet: LEAFLET, design: DESIGN }), () => players()));
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

after(() => {
  server.close();
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
