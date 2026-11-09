// B1, status up: what the web map reports to the control plane's status listener, as
// contracts/service.status.schema.json defines it. The data layer's vhd_data/status.py is the
// same reporter in Python.

export const SERVICE = "web-map";
export const REPORT_TIMEOUT_MS = 5000;
// The longest reason the contract takes.
export const MAX_REASON = 500;

export type State = "starting" | "running" | "failing";

export interface ServiceStatus {
  contract: 1;
  generation: number;
  service: typeof SERVICE;
  state: State;
  reason?: string;
}

export type Post = (url: string, body: string) => Promise<void>;
export type Log = (line: string) => void;

export async function post(url: string, body: string): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    signal: AbortSignal.timeout(REPORT_TIMEOUT_MS),
  });
  await response.body?.cancel();
  if (!response.ok) {
    throw new Error(`it answered ${response.status}`);
  }
}

/** A status report: a reason only when the service is not running and knows why. */
export function report(generation: number, state: State, why?: string): ServiceStatus {
  const status: ServiceStatus = { contract: 1, generation, service: SERVICE, state };
  if (state !== "running" && why !== undefined) {
    status.reason = reason(why);
  }
  return status;
}

/** text as the contract takes a reason: one line of printable characters, cut to fit. */
export function reason(text: string): string {
  const line = text.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
  return (line || "no reason given").slice(0, MAX_REASON);
}

/** Sends status to the control plane. Observations fail soft: a failed report is logged once and
 * never stops the service. */
export class Reporter {
  readonly #post: Post;
  readonly #log: Log;
  #failing = false;
  #logged: string | undefined;

  constructor(log: Log, send: Post = post) {
    this.#log = log;
    this.#post = send;
  }

  async send(url: string, status: ServiceStatus): Promise<void> {
    const body = JSON.stringify(status);
    // Logged when it changes: a service sends the same report every few seconds for weeks.
    if (body !== this.#logged) {
      this.#log(`status ${body}`);
      this.#logged = body;
    }
    try {
      await this.#post(url, body);
    } catch (error) {
      if (!this.#failing) {
        this.#log(`cannot report status to ${url}: ${error instanceof Error ? error.message : String(error)}`);
      }
      this.#failing = true;
      return;
    }
    if (this.#failing) {
      this.#log(`status reports reach ${url} again`);
    }
    this.#failing = false;
  }
}
