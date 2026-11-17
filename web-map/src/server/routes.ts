// What the web map answers. Every route is a GET, and every file it serves is read once at start
// from an allow-list, so no request path ever reaches the file system: a world's render is served
// from memory, by a name matched exactly against the generation's worlds. No route reads a cookie
// or the query, and the only header any reads is If-None-Match: the players and the worlds are
// public and read-only (B9, B11).

import type { IncomingMessage, ServerResponse } from "node:http";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";
import type { Render } from "./mapexport.js";
import type { LivePlayers } from "./players.js";

export interface Asset {
  readonly type: string;
  readonly body: Buffer;
}

// The page loads only what the web map itself serves: the render's image is same-origin too.
const POLICY = "default-src 'self'; frame-ancestors 'none'";
const NO_STORE = { "Cache-Control": "no-store" };
const RENDER_PATH = /^\/api\/worlds\/([^/]+)\/render\.png$/;
const VERSION_LENGTH = 16;

/** The worlds of the adopted generation and their renders: what MapRenders knows. */
export interface Worlds {
  names(): string[];
  get(world: string): Render | undefined;
}

// A render's image never changes, so its version is worked out once per image.
const versions = new WeakMap<Buffer, string>();

/** The first 16 hex digits of the SHA-256 of a render's PNG (B11). */
export function versionOf(render: Render): string {
  let version = versions.get(render.png);
  if (version === undefined) {
    version = createHash("sha256").update(render.png).digest("hex").slice(0, VERSION_LENGTH);
    versions.set(render.png, version);
  }
  return version;
}

/** B11's worlds document: geometry and version only, never the water level. */
function worldsDocument(worlds: Worlds): string {
  return JSON.stringify({
    contract: 1,
    worlds: worlds.names().map((name) => {
      const render = worlds.get(name);
      return {
        name,
        render: render === undefined ? null : { size: render.size, half_width: render.half_width, version: versionOf(render) },
      };
    }),
  });
}

/** The world a render path names, if the generation has it: the name is decoded once and compared
 * with the known names, so nothing of the path is used but as a key. */
function renderOf(worlds: Worlds, path: string): Render | undefined {
  const encoded = RENDER_PATH.exec(path)?.[1];
  if (encoded === undefined) {
    return undefined;
  }
  let name: string;
  try {
    name = decodeURIComponent(encoded);
  } catch {
    return undefined;
  }
  return worlds.names().includes(name) ? worlds.get(name) : undefined;
}

const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".woff2": "font/woff2",
};

export interface Directories {
  /** The page and its styles. */
  public: string;
  /** The page's compiled script. */
  page: string;
  /** Leaflet's dist directory. */
  leaflet: string;
  /** The design system. */
  design: string;
}

/** The files the web map serves, by request path: its own page, styles and script; the two files
 * of Leaflet it needs, by name, under /leaflet/; and the design system's tokens and fonts under
 * /design/, where tokens.css finds its fonts by relative path. */
export function assets(directories: Directories): Map<string, Asset> {
  const served = new Map<string, Asset>();
  const add = (path: string, file: string): void => {
    const type = TYPES[file.slice(file.lastIndexOf("."))];
    if (type !== undefined) {
      served.set(path, { type, body: readFileSync(file) });
    }
  };
  for (const name of readdirSync(directories.public)) {
    add(name === "index.html" ? "/" : `/${name}`, join(directories.public, name));
  }
  add("/web-map.js", join(directories.page, "web-map.js"));
  for (const name of ["leaflet.js", "leaflet.css"]) {
    add(`/leaflet/${name}`, join(directories.leaflet, name));
  }
  add("/design/tokens.css", join(directories.design, "tokens.css"));
  for (const name of readdirSync(join(directories.design, "fonts"))) {
    add(`/design/fonts/${name}`, join(directories.design, "fonts", name));
  }
  return served;
}

/** The request handler: the build the image was made from, the live players, the worlds and their
 * renders, then the allow-listed files. */
export function handler(
  build: string,
  served: ReadonlyMap<string, Asset>,
  players: () => Promise<LivePlayers>,
  worlds: Worlds,
): (request: IncomingMessage, response: ServerResponse) => void {
  return (request: IncomingMessage, response: ServerResponse): void => {
    const path = new URL(request.url ?? "/", "http://web-map").pathname;
    if (request.method !== "GET" && request.method !== "HEAD") {
      answer(response, 405, "text/plain; charset=utf-8", "GET only\n", { Allow: "GET, HEAD" });
      return;
    }
    if (path === "/build") {
      // What acceptance and the healthcheck read: the commit this image was built from.
      answer(response, 200, "text/plain; charset=utf-8", `${build}\n`);
      return;
    }
    if (path === "/api/players") {
      // The database's error goes to the log and the status, never to a viewer.
      Promise.resolve().then(players).then(
        (live) => answer(response, 200, "application/json; charset=utf-8", JSON.stringify(live), NO_STORE),
        () => answer(response, 503, "text/plain; charset=utf-8", "the live players are unavailable\n", NO_STORE),
      );
      return;
    }
    if (path === "/api/worlds") {
      answer(response, 200, "application/json; charset=utf-8", worldsDocument(worlds), NO_STORE);
      return;
    }
    if (path.startsWith("/api/worlds/")) {
      const render = renderOf(worlds, path);
      if (render === undefined) {
        answer(response, 404, "text/plain; charset=utf-8", "not found\n", NO_STORE);
        return;
      }
      // The ETag is the version B11 names, so a client that has the image is told so.
      const etag = `"${versionOf(render)}"`;
      const headers = { "Cache-Control": "no-cache", ETag: etag };
      if (request.headers["if-none-match"] === etag) {
        response.writeHead(304, { ...headers, "Content-Security-Policy": POLICY, "X-Content-Type-Options": "nosniff" });
        response.end();
        return;
      }
      answer(response, 200, "image/png", render.png, headers);
      return;
    }
    const asset = served.get(path);
    if (asset === undefined) {
      answer(response, 404, "text/plain; charset=utf-8", "not found\n");
      return;
    }
    answer(response, 200, asset.type, asset.body);
  };
}

function answer(
  response: ServerResponse,
  status: number,
  type: string,
  body: string | Buffer,
  headers: Record<string, string> = {},
): void {
  response.writeHead(status, {
    "Content-Type": type,
    "Content-Length": Buffer.byteLength(body),
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": POLICY,
    ...headers,
  });
  response.end(response.req.method === "HEAD" ? undefined : body);
}
