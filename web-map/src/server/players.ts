// B11: the live players the web map answers, read from the data layer's live_player view (B6)
// with the web_map role's own sign-in. The database may start after the web map or restart while
// it runs, so the web map reaches it with a capped backoff and reports `starting`, as the data
// layer's intake does (vhd_data/intake.py); until it is reached, players are refused at once
// and the page keeps being served.

import pg from "pg";
import type { Documents } from "./documents.js";
import type { Log, State } from "./status.js";

// A live player was seen within three of the plugin's position intervals, which are 30 s
// (plugin/src/Telemetry/Players.cs; docs/architecture.md § Data layer, live player). A world that
// stopped sends no leave, so this cut is what ends its players.
export const LIVE_WINDOW_S = 90;
// How long a read's answer serves later requests: many viewers poll, the database is asked once.
export const CACHE_MS = 2000;
// Waits between attempts to reach the database, doubling to the most.
export const RETRY_FIRST_MS = 1000;
export const RETRY_MAX_MS = 30_000;
const CONNECT_TIMEOUT_MS = 5000;
const STATEMENT_TIMEOUT_MS = 5000;
const QUERY_TIMEOUT_MS = 6000;
const IDLE_TIMEOUT_MS = 30_000;
// Two connections: a flood of viewers cannot open more, and one query is in flight at a time.
const MAX_CONNECTIONS = 2;

/** contracts/web-map.players.schema.json */
export interface LivePlayer {
  name: string | null;
  x: number;
  y: number;
  z: number;
}
export interface LiveWorld {
  name: string;
  players: LivePlayer[];
}
export interface LivePlayers {
  contract: 1;
  worlds: LiveWorld[];
}

/** A row of the live_player view, as the query selects it. */
export interface Row {
  world: string;
  name: string | null;
  x: number;
  y: number;
  z: number;
}

/** The boundary the tests replace: one parameterized statement, its rows. */
export type Query = (text: string, values: unknown[]) => Promise<Row[]>;

export interface Connection {
  query: Query;
  end(): Promise<void>;
}
export type Connect = (documents: Documents, log: Log) => Connection;

/** Players cannot be read: the database is not reached yet, or has failed. */
export class Unavailable extends Error {}

// received_at is the database's clock, which one machine keeps; a game's own clock (at) is not.
const READ =
  "SELECT world, name, x, y, z FROM live_player " +
  "WHERE received_at > now() - make_interval(secs => $1) " +
  "ORDER BY world, name NULLS LAST";
const CHECK = "SELECT 1 FROM live_player LIMIT 1";

/** The rows grouped by world, in the order they come: a world shows only with a live player. */
export function grouped(rows: readonly Row[]): LivePlayers {
  const worlds: LiveWorld[] = [];
  for (const { world, name, x, y, z } of rows) {
    let entry = worlds.at(-1);
    if (entry?.name !== world) {
      entry = { name: world, players: [] };
      worlds.push(entry);
    }
    entry.players.push({ name: name === "" ? null : name, x, y, z });
  }
  return { contract: 1, worlds };
}

/** The only code that talks to PostgreSQL. The sign-in is the generation's, never a constant. */
export function open(documents: Documents, log: Log): Connection {
  const { host, port, name, user } = documents.generation.database;
  const { password } = documents.secrets;
  const pool = new pg.Pool({
    host,
    port,
    database: name,
    user,
    password: () => password.reveal(),
    max: MAX_CONNECTIONS,
    connectionTimeoutMillis: CONNECT_TIMEOUT_MS,
    statement_timeout: STATEMENT_TIMEOUT_MS,
    query_timeout: QUERY_TIMEOUT_MS,
    idleTimeoutMillis: IDLE_TIMEOUT_MS,
    application_name: "vhd-web-map",
  });
  // An idle connection the database drops is an error event; with no listener it ends the process.
  pool.on("error", (error) => log(`a database connection dropped: ${error.message}`));
  return {
    query: async (text, values) => (await pool.query<Row>(text, values)).rows,
    end: () => pool.end(),
  };
}

function signIn(documents: Documents): string {
  return JSON.stringify([documents.generation.database, documents.secrets.password.reveal()]);
}

export interface PlayersStatus {
  state: State;
  reason?: string;
}

/** Holds the connection and its state: starting until the database answers, running while it
 * does, failing once a read fails. */
export class Players {
  readonly #log: Log;
  readonly #connect: Connect;
  #connection: Connection | undefined;
  #signedIn: string | undefined;
  #state: State = "starting";
  #reason: string | undefined = "waiting for the generation";
  #retryAt = 0;
  #checking = false;
  #wait = RETRY_FIRST_MS;
  #flight: Promise<LivePlayers> | undefined;
  #cache: { at: number; answer: LivePlayers } | undefined;

  constructor(log: Log, connect: Connect = open) {
    this.#log = log;
    this.#connect = connect;
  }

  status(): PlayersStatus {
    return this.#reason === undefined ? { state: this.#state } : { state: this.#state, reason: this.#reason };
  }

  /** Take a generation's sign-in. New database settings or a new password sign in again. */
  adopt(documents: Documents): void {
    const next = signIn(documents);
    if (next === this.#signedIn) {
      return;
    }
    const before = this.#connection;
    this.#signedIn = next;
    this.#connection = this.#connect(documents, this.#log);
    this.#cache = undefined;
    this.#flight = undefined;
    this.#set("starting", "waiting for the database");
    this.#retryAt = 0;
    this.#wait = RETRY_FIRST_MS;
    before?.end().catch(() => undefined);
  }

  /** Until the database answers, try it with a capped backoff. */
  async tick(now: number = Date.now()): Promise<void> {
    const connection = this.#connection;
    if (connection === undefined || this.#state === "running" || this.#checking || now < this.#retryAt) {
      return;
    }
    this.#checking = true;
    try {
      await connection.query(CHECK, []);
    } catch (error) {
      if (connection === this.#connection) {
        this.#set(this.#state === "failing" ? "failing" : "starting", `cannot reach the database: ${message(error)}`);
        this.#retryAt = now + this.#wait;
        this.#wait = Math.min(this.#wait * 2, RETRY_MAX_MS);
      }
      return;
    } finally {
      this.#checking = false;
    }
    if (connection === this.#connection) {
      this.#set("running");
      this.#wait = RETRY_FIRST_MS;
    }
  }

  /** The live players. Refuses at once unless the database is reached, so an outage costs no
   * query; a failed read fails the state, and no failure is kept. */
  read(now: number = Date.now()): Promise<LivePlayers> {
    const connection = this.#connection;
    if (connection === undefined || this.#state !== "running") {
      return Promise.reject(new Unavailable(this.#reason ?? "the database is not reached"));
    }
    if (this.#cache !== undefined && now - this.#cache.at < CACHE_MS) {
      return Promise.resolve(this.#cache.answer);
    }
    // One query in flight, whoever asks meanwhile.
    this.#flight ??= this.#fetch(connection, now);
    return this.#flight;
  }

  async #fetch(connection: Connection, now: number): Promise<LivePlayers> {
    try {
      const answer = grouped(await connection.query(READ, [LIVE_WINDOW_S]));
      if (connection === this.#connection) {
        this.#cache = { at: now, answer };
      }
      return answer;
    } catch (error) {
      if (connection === this.#connection) {
        this.#cache = undefined;
        this.#set("failing", `cannot reach the database: ${message(error)}`);
      }
      throw new Unavailable(message(error));
    } finally {
      if (connection === this.#connection) {
        this.#flight = undefined;
      }
    }
  }

  async end(): Promise<void> {
    const connection = this.#connection;
    this.#connection = undefined;
    this.#signedIn = undefined;
    await connection?.end().catch(() => undefined);
  }

  #set(state: State, reason?: string): void {
    // Logged when it changes: a failing database is retried for as long as it is down.
    if (state !== this.#state || reason !== this.#reason) {
      this.#log(`players ${state}${reason === undefined ? "" : `: ${reason}`}`);
    }
    this.#state = state;
    this.#reason = reason;
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
