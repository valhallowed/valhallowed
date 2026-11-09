// What the web map answers. Every route is a GET, and every file it serves is read once at start
// from an allow-list, so no request path ever reaches the file system.

import type { IncomingMessage, ServerResponse } from "node:http";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

export interface Asset {
  readonly type: string;
  readonly body: Buffer;
}

const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".woff2": "font/woff2",
};

/** The files the web map serves, by request path: its own pages, and the design system's tokens
 * and fonts under /design/, where tokens.css finds its fonts by relative path. */
export function assets(publicDir: string, designDir: string): Map<string, Asset> {
  const served = new Map<string, Asset>();
  const add = (path: string, file: string): void => {
    const type = TYPES[file.slice(file.lastIndexOf("."))];
    if (type !== undefined) {
      served.set(path, { type, body: readFileSync(file) });
    }
  };
  for (const name of readdirSync(publicDir)) {
    add(name === "index.html" ? "/" : `/${name}`, join(publicDir, name));
  }
  add("/design/tokens.css", join(designDir, "tokens.css"));
  for (const name of readdirSync(join(designDir, "fonts"))) {
    add(`/design/fonts/${name}`, join(designDir, "fonts", name));
  }
  return served;
}

/** The request handler: the build the image was made from, then the allow-listed files. */
export function handler(
  build: string,
  served: ReadonlyMap<string, Asset>,
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
    // The page loads only what the web map itself serves.
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    ...headers,
  });
  response.end(response.req.method === "HEAD" ? undefined : body);
}
