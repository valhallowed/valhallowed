import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";
import { assets, handler } from "../server/routes.js";
import { DESIGN, PUBLIC } from "./paths.js";

let server: Server;
let base: string;

before(async () => {
  server = createServer(handler("abc1234", assets(PUBLIC, DESIGN)));
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
  for (const path of ["/index.html", "/design/fonts/OFL-alegreya-sans.txt", "/../package.json", "/%2e%2e/package.json"]) {
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
