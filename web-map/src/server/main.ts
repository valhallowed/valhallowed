// The web map service, run by tini as its container's init. It serves its page from the moment it
// starts, and reports status once a generation arrives, adopting each new one in place. The live
// players need the database: until the web map reaches it they answer 503 and its state is
// `starting`, while the page and /build, which the healthcheck reads, are served all the same.

import { createServer } from "node:http";
import { Adopter, Reader } from "./documents.js";
import { Players } from "./players.js";
import { assets, handler } from "./routes.js";
import { report, Reporter, SERVICE } from "./status.js";

// Where the web map listens on the install network; vhd apply publishes it on the manifest's
// web_map.port (control's layout.WEB_MAP_PORT).
const PORT = 8456;
const GENERATION = "/generation";
const CONTRACTS = "/opt/vhd/contracts";
const PUBLIC = "/opt/vhd/web-map/public";
const PAGE = "/opt/vhd/web-map/page";
const LEAFLET = "/opt/vhd/web-map/node_modules/leaflet/dist";
const DESIGN = "/opt/vhd/design";
const POLL_MS = 1000;
// A slow or idle client cannot hold a connection open, and no flood of them exhausts the process.
const HEADERS_TIMEOUT_MS = 10_000;
const REQUEST_TIMEOUT_MS = 30_000;
const MAX_CONNECTIONS = 256;

function log(line: string): void {
  process.stdout.write(`vhd-${SERVICE} ${line}\n`);
}

const reader = new Reader(SERVICE, GENERATION, CONTRACTS);
const reporter = new Reporter(log);
const held = new Adopter(reader, log);
const players = new Players(log);
let reportedAt = Number.NEGATIVE_INFINITY;

async function tick(): Promise<void> {
  held.adopt();
  const documents = held.documents;
  if (documents !== undefined) {
    players.adopt(documents);
    await players.tick();
  }
  if (documents === undefined || Date.now() - reportedAt < documents.generation.status_interval_s * 1000) {
    return;
  }
  reportedAt = Date.now();
  const { state, reason } = players.status();
  await reporter.send(documents.generation.status_url, report(documents.number, state, reason));
}

const server = createServer(
  { headersTimeout: HEADERS_TIMEOUT_MS, requestTimeout: REQUEST_TIMEOUT_MS },
  handler(process.env.VHD_BUILD ?? "unknown", assets({ public: PUBLIC, page: PAGE, leaflet: LEAFLET, design: DESIGN }), () => players.read()),
);
server.maxConnections = MAX_CONNECTIONS;
server.listen(PORT, () => log(`serving on ${PORT}`));

let running = true;
const timer = setInterval(() => {
  if (running) {
    tick().catch((error: unknown) => log(`status pass failed: ${String(error)}`));
  }
}, POLL_MS);

function stop(): void {
  running = false;
  clearInterval(timer);
  server.close(() => {
    players.end().finally(() => process.exit(0));
  });
}
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
