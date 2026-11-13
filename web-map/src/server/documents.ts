// B1: the web map's generation and its scoped secrets, read from its own directory of the
// generations volume and validated against the contracts the image ships. The data layer's
// vhd_data/documents.py is the same reader in Python; the two ship in separate images, so each
// carries its own.

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { Ajv2020, type ErrorObject, type ValidateFunction } from "ajv/dist/2020.js";

export const GENERATION_FILE = "generation.json";
export const SECRETS_FILE = "secrets.json";

/** contracts/web-map.generation.schema.json */
export interface Generation {
  contract: number;
  generation: number;
  status_url: string;
  status_interval_s: number;
}

/** contracts/web-map.secrets.schema.json */
export interface Secrets {
  contract: number;
  generation: number;
}

export interface Documents {
  number: number;
  generation: Generation;
  secrets: Secrets;
}

/** The generation or its secrets break the B1 contract. */
export class InvalidDocuments extends Error {}

/** The generation's secrets are not written yet: missing, or for another generation. */
export class IncompleteDocuments extends Error {}

export class Reader {
  readonly #directory: string;
  readonly #generation: ValidateFunction;
  readonly #secrets: ValidateFunction;
  #read: { stamp: string; documents: Documents } | undefined;

  constructor(service: string, directory: string, contracts: string) {
    const ajv = new Ajv2020({ strict: true });
    this.#directory = directory;
    this.#generation = ajv.compile(schema(join(contracts, `${service}.generation.schema.json`)));
    this.#secrets = ajv.compile(schema(join(contracts, `${service}.secrets.schema.json`)));
  }

  /** The current documents, or undefined while the control plane has written none. Throws
   * IncompleteDocuments until secrets.json carries the same generation. */
  read(): Documents | undefined {
    const generationPath = join(this.#directory, GENERATION_FILE);
    const secretsPath = join(this.#directory, SECRETS_FILE);
    // Asked every second; the files change only when the control plane replaces them, which gives
    // each a new inode.
    const stamp = stampOf(generationPath, secretsPath);
    if (this.#read?.stamp === stamp) {
      return this.#read.documents;
    }
    // A service's directory holds two files. A directory here means the whole generations volume
    // is mounted, every child's secrets with it.
    if (existsSync(this.#directory) && readdirSync(this.#directory, { withFileTypes: true }).some((e) => e.isDirectory())) {
      throw new InvalidDocuments(`${this.#directory} shows other children's directories, not this service's files`);
    }
    if (!existsSync(generationPath)) {
      return undefined;
    }
    const generation = load(generationPath, this.#generation, false) as Generation;
    const number = generation.generation;
    if (!existsSync(secretsPath)) {
      throw new IncompleteDocuments(`generation ${number} has no ${SECRETS_FILE} yet`);
    }
    const secrets = load(secretsPath, this.#secrets, true) as Secrets;
    if (secrets.generation !== number) {
      throw new IncompleteDocuments(`${SECRETS_FILE} belongs to generation ${secrets.generation}, not ${number}`);
    }
    const documents = { number, generation, secrets };
    this.#read = { stamp, documents };
    return documents;
  }
}

function schema(path: string): object {
  return JSON.parse(readFileSync(path, "utf8")) as object;
}

/** Which version of each file is on disk, or a stamp that matches nothing when one is missing. */
function stampOf(...paths: string[]): string {
  const parts: number[] = [];
  for (const path of paths) {
    try {
      const info = statSync(path, { bigint: false });
      parts.push(info.ino, info.mtimeMs, info.size);
    } catch {
      return "missing";
    }
  }
  return parts.join(":");
}

/** A document that meets its contract. For a secret document a failure names where and which
 * rule, never the value: ajv's messages carry no data, and a JSON error's message can. */
function load(path: string, validate: ValidateFunction, secret: boolean): unknown {
  let document: unknown;
  try {
    document = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    const why = secret || !(error instanceof Error) ? "it is not JSON" : error.message;
    throw new InvalidDocuments(`${path}: ${why}`);
  }
  if (!validate(document)) {
    throw new InvalidDocuments(`${path}: ${problem(validate.errors ?? [])}`);
  }
  return document;
}

function problem(errors: ErrorObject[]): string {
  const first = errors[0];
  if (first === undefined) {
    return "it breaks its contract";
  }
  return `at ${first.instancePath || "(root)"}: ${first.message ?? first.keyword}`;
}

/** Holds the last good documents. Each pass takes a higher generation, or keeps what it holds
 * when the pair is refused or incomplete; a refusal is logged once, not on every pass. */
export class Adopter {
  readonly #reader: Pick<Reader, "read">;
  readonly #log: (line: string) => void;
  #documents: Documents | undefined;
  #refused: string | undefined;

  constructor(reader: Pick<Reader, "read">, log: (line: string) => void) {
    this.#reader = reader;
    this.#log = log;
  }

  get documents(): Documents | undefined {
    return this.#documents;
  }

  /** Take a new generation, or say why one cannot be taken; the last good one keeps running. */
  adopt(): void {
    try {
      const read = this.#reader.read();
      if (read !== undefined && read.number !== this.#documents?.number) {
        this.#log(`generation ${read.number} adopted`);
        this.#documents = read;
        this.#refused = undefined;
      }
    } catch (error) {
      if (!(error instanceof InvalidDocuments || error instanceof IncompleteDocuments)) {
        throw error;
      }
      if (error instanceof InvalidDocuments && error.message !== this.#refused) {
        this.#log(`refused this generation: ${error.message}`);
        this.#refused = error.message;
      }
    }
  }
}
