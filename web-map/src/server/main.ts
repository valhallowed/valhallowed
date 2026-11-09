// The web map service, run by tini as its container's init. It serves from the moment it starts,
// since nothing it serves yet depends on its generation, and reports status once a generation
// arrives, adopting each new one in place.

import { createServer } from "node:http";
import { IncompleteDocuments, InvalidDocuments, Reader, type Documents } from "./documents.js";
import { assets, handler } from "./routes.js";
import { report, Reporter, SERVICE } from "./status.js";

// Where the web map listens on the install network; vhd apply publishes it on the manifest's
// web_map.port (control's layout.WEB_MAP_PORT).
const PORT = 8456;
const GENERATION = "/generation";
const CONTRACTS = "/opt/vhd/contracts";
const PUBLIC = "/opt/vhd/web-map/public";
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
let documents: Documents | undefined;
// The last refusal logged, so a broken generation is logged once, not every second.
let refused: string | undefined;
let reportedAt = Number.NEGATIVE_INFINITY;

/** Take a new generation, or say why one cannot be taken; the last good one keeps running. */
function adopt(): void {
  try {
    const read = reader.read();
    if (read !== undefined && read.number !== documents?.number) {
      log(`generation ${read.number} adopted`);
      documents = read;
      refused = undefined;
    }
  } catch (error) {
    if (!(error instanceof InvalidDocuments || error instanceof IncompleteDocuments)) {
      throw error;
    }
    if (error instanceof InvalidDocuments && error.message !== refused) {
      log(`refused this generation: ${error.message}`);
      refused = error.message;
    }
  }
}

async function tick(): Promise<void> {
  adopt();
  if (documents === undefined || Date.now() - reportedAt < documents.generation.status_interval_s * 1000) {
    return;
  }
  reportedAt = Date.now();
  // Serving needs nothing the generation holds, so a web map with one runs.
  await reporter.send(documents.generation.status_url, report(documents.number, "running"));
}

const server = createServer(
  { headersTimeout: HEADERS_TIMEOUT_MS, requestTimeout: REQUEST_TIMEOUT_MS },
  handler(process.env.VHD_BUILD ?? "unknown", assets(PUBLIC, DESIGN)),
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
  server.close(() => process.exit(0));
}
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
