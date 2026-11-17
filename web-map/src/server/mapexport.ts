// B5, reader side, and world render: each world's map export, read as untrusted input (the world's
// own container writes it), turned into a north-up base image the web map keeps in memory, by
// world. A bad export never replaces the last good render; it is logged when its reason changes
// and reported in status. routes.ts serves the image, by world (docs/architecture.md § World render).
//
// A world can rewrite its export as often as it likes, so the work it can cause is bounded: a
// meta whose bytes are the last seen costs nothing past its 4 KiB read; a render starts at most
// RENDER_MIN_INTERVAL_MS after the world's last, and one world renders per poll. The raster is
// read in chunks and deflated off the event loop. What stays synchronous is the pixel loop in
// renderRgba, about 250 ms at the largest raster (60 ms at the plugin's 1000 pixels), and parsing a 4 KiB meta.

import { createHash, type Hash } from "node:crypto";
import { constants, readFileSync } from "node:fs";
import { lstat, open } from "node:fs/promises";
import { join } from "node:path";
import { Ajv2020, type ValidateFunction } from "ajv/dist/2020.js";
import type { Documents } from "./documents.js";
import { OCEAN, type Palette } from "./palette.js";
import { encodePng } from "./png.js";
import type { Log } from "./status.js";

/** The one file the reader reacts to: the plugin's dot-prefixed temporaries and every other name
 * in the directory are never looked at. */
export const META_FILE = "meta.json";
/** How often each world's meta.json is looked at. An export changes rarely. */
export const EXPORT_POLL_MS = 5000;
/** The least time between the starts of two renders of one world. */
export const RENDER_MIN_INTERVAL_MS = 60_000;
/** The most a meta.json may hold; the plugin writes about 300 bytes plus the world's name. */
export const MAX_META_BYTES = 4096;
/** The largest raster a world may export, in pixels a side. The plugin writes 1000; this leaves
 * room, and bounds what a hostile export can make the web map read (2048 * 2048 * 10 bytes). */
export const MAX_SIZE_PX = 2048;
/** A raster is read, and hashed, this much at a time. */
const CHUNK_BYTES = 1 << 20;
/** Height above the water at which the shading is brightest, in metres. */
export const HEIGHT_SPAN_M = 200;
/** Forest is where the raster's forest factor is below this (contract). */
export const FOREST_BELOW = 1.15;
// Shading is integer arithmetic on factors of 256, so the image is the same everywhere.
const SHADE_MIN = 160; // of 256, at the water line
const SHADE_RANGE = 96; // up to 256, at HEIGHT_SPAN_M above it
const FOREST_DARKEN = 208; // of 256

/** What a map export is refused for: its content. The message says what, never the meta's
 * values: the meta names the seed, which the web map serves nothing of. */
export class BadExport extends Error {}

/** A file could not be read for a reason that says nothing of its content (EIO, EMFILE, EACCES):
 * the next poll tries again. */
export class Unreadable extends BadExport {}

/** A file is not there: no export yet. */
export class Missing extends Error {}

/** contracts/plugin.map-export.schema.json */
export interface Meta {
  contract: 1;
  world: string;
  seed: { name: string; value: number };
  generator_version: number;
  water_level: number;
  geometry: { size: number; half_width: number };
  raster: { file: string; bytes: number; sha256: string };
}

/** A world's last good render: the encoded image and the geometry a client needs to place it.
 * Nothing of the meta is kept but this, so the seed never leaves the reader. */
export interface Render {
  png: Buffer;
  size: number;
  half_width: number;
  water_level: number;
}

export interface Taken {
  body: Buffer;
  /** Which file this was, from the opened handle: a new file, or a changed one, differs. */
  stamp: string;
}

/** The bytes of the regular file at `path`, at most `max` of them, named `name` in a refusal.
 * Refuses a link (lstat, then O_NOFOLLOW), anything but a regular file, a file with another
 * hard link, and a file that is not the one lstat saw. */
export async function readRegular(path: string, name: string, max: number, hash?: Hash): Promise<Taken> {
  let before;
  try {
    before = await lstat(path);
  } catch (error) {
    throw failed(name, error);
  }
  if (!before.isFile()) {
    throw new BadExport(`${name} is not a regular file`);
  }
  if (before.size > max) {
    throw new BadExport(`${name} is larger than ${max} bytes`);
  }
  let handle;
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK | constants.O_NOCTTY);
  } catch (error) {
    throw failed(name, error);
  }
  try {
    const opened = await handle.stat();
    if (!opened.isFile() || opened.ino !== before.ino || opened.dev !== before.dev) {
      throw new BadExport(`${name} changed while it was opened`);
    }
    if (opened.nlink > 1) {
      throw new BadExport(`${name} has more than one link`);
    }
    if (opened.size > max) {
      throw new BadExport(`${name} is larger than ${max} bytes`);
    }
    const body = Buffer.alloc(opened.size);
    let at = 0;
    while (at < body.length) {
      const { bytesRead } = await handle.read(body, at, Math.min(CHUNK_BYTES, body.length - at), at);
      if (bytesRead === 0) {
        throw new BadExport(`${name} is shorter than it was`);
      }
      hash?.update(body.subarray(at, at + bytesRead));
      at += bytesRead;
    }
    return { body, stamp: `${opened.dev}:${opened.ino}:${opened.mtimeMs}:${opened.size}` };
  } catch (error) {
    throw error instanceof BadExport ? error : new Unreadable(`cannot read ${name}: ${code(error)}`);
  } finally {
    await handle.close().catch(() => undefined);
  }
}

function failed(name: string, error: unknown): Error {
  const reason = code(error);
  if (reason === "ENOENT" || reason === "ENOTDIR") {
    return new Missing(name);
  }
  // O_NOFOLLOW on a link that appeared after lstat.
  return reason === "ELOOP" ? new BadExport(`${name} is not a regular file`) : new Unreadable(`cannot read ${name}: ${reason}`);
}

function code(error: unknown): string {
  return (error as NodeJS.ErrnoException).code ?? "failed";
}

/** The B5 schema, compiled. */
export function metaValidator(contracts: string): ValidateFunction {
  const schema = JSON.parse(readFileSync(join(contracts, "plugin.map-export.schema.json"), "utf8")) as object;
  return new Ajv2020({ strict: true }).compile(schema);
}

/** The meta in `body`, valid against B5, its reader's rule on bytes, and for `world`. */
export function parseMeta(body: Buffer, validate: ValidateFunction, world: string): Meta {
  let document: unknown;
  try {
    document = JSON.parse(body.toString("utf8"));
  } catch {
    // Node's parse error quotes the text, which holds the seed.
    throw new BadExport(`${META_FILE} is not JSON`);
  }
  if (!validate(document)) {
    const first = validate.errors?.[0];
    throw new BadExport(`${META_FILE} at ${first?.instancePath || "(root)"}: ${first?.message ?? "it breaks its contract"}`);
  }
  const meta = document as Meta;
  const { size } = meta.geometry;
  if (size > MAX_SIZE_PX) {
    throw new BadExport(`${META_FILE} asks for a raster larger than ${MAX_SIZE_PX} pixels a side`);
  }
  if (meta.raster.bytes !== size * size * 10) {
    throw new BadExport(`${META_FILE}'s raster.bytes is not size * size * 10`);
  }
  if (meta.world !== world) {
    throw new BadExport(`${META_FILE} names another world`);
  }
  return meta;
}

/** The meta in `directory`, parsed and checked. */
export async function readMeta(directory: string, validate: ValidateFunction, world: string): Promise<Meta> {
  const { body } = await readRegular(join(directory, META_FILE), META_FILE, MAX_META_BYTES);
  return parseMeta(body, validate, world);
}

/** The raster the meta names, whole and checked against the meta's length and SHA-256. */
export async function readRaster(directory: string, meta: Meta): Promise<Buffer> {
  const hash = createHash("sha256");
  let taken: Taken;
  try {
    taken = await readRegular(join(directory, meta.raster.file), meta.raster.file, meta.raster.bytes, hash);
  } catch (error) {
    throw error instanceof Missing ? new BadExport(`${meta.raster.file} is missing`) : error;
  }
  if (taken.body.length !== meta.raster.bytes) {
    throw new BadExport(`${meta.raster.file} is not ${META_FILE}'s length`);
  }
  if (hash.digest("hex") !== meta.raster.sha256) {
    throw new BadExport(`${meta.raster.file} does not match ${META_FILE}'s SHA-256`);
  }
  return taken.body;
}

/** The raster as north-up RGBA, one pixel per raster pixel, row 0 north as the contract lays it
 * out. A pixel is water when its height is at or below the water level (B5), and also when its
 * biome is Ocean at any height (this renderer's rule; B5 states only the height rule). Land has
 * its biome's colour, brighter with height above the water and darker under forest. Throws
 * BadExport on a value the contract says a raster never holds. */
export function renderRgba(meta: Meta, raster: Buffer, palette: Palette): Uint8Array {
  const count = meta.geometry.size * meta.geometry.size;
  const heights = count * 2;
  const forests = count * 6;
  const out = new Uint8Array(count * 4);
  for (let i = 0; i < count; i++) {
    const biome = raster.readUInt16LE(i * 2);
    const height = raster.readFloatLE(heights + i * 4);
    const forest = raster.readFloatLE(forests + i * 4);
    if (!Number.isFinite(height) || !Number.isFinite(forest)) {
      throw new BadExport(`${meta.raster.file} holds a height or forest factor that is not finite`);
    }
    let colour = palette.biomes.get(biome);
    if (colour === undefined && biome !== OCEAN) {
      throw new BadExport(`${meta.raster.file} holds a biome the contract does not name`);
    }
    // The colour's factor, 8.8 by 8.8 fixed point: 65536 leaves it as it is.
    let factor = 65536;
    if (colour === undefined || height <= meta.water_level) {
      colour = palette.water;
    } else {
      const above = Math.min(1, (height - meta.water_level) / HEIGHT_SPAN_M);
      factor = (SHADE_MIN + Math.floor(SHADE_RANGE * above)) * (forest < FOREST_BELOW ? FOREST_DARKEN : 256);
    }
    out[i * 4] = (colour[0] * factor) >> 16;
    out[i * 4 + 1] = (colour[1] * factor) >> 16;
    out[i * 4 + 2] = (colour[2] * factor) >> 16;
    out[i * 4 + 3] = 255;
  }
  return out;
}

async function renderOf(directory: string, meta: Meta, palette: Palette): Promise<Render> {
  const rgba = renderRgba(meta, await readRaster(directory, meta), palette);
  const { size, half_width } = meta.geometry;
  return { png: await encodePng(size, size, rgba), size, half_width, water_level: meta.water_level };
}

/** The export in `directory`, which is `world`'s, as a render. Throws BadExport when any rule of
 * B5 fails. */
export async function renderExport(directory: string, validate: ValidateFunction, palette: Palette, world: string): Promise<Render> {
  return renderOf(directory, await readMeta(directory, validate, world), palette);
}

/** What the unexpected is reported as: its message could quote anything. */
const UNEXPECTED = "render failed";

interface Watched {
  path: string;
  /** The meta file last looked at, and the SHA-256 of its bytes, once its content has been settled
   * (rendered or refused). An I/O failure clears both, so the next poll tries again. */
  stamp: string | undefined;
  digest: string | undefined;
  /** When this world's last render began. */
  renderedAt: number;
  render: Render | undefined;
  bad: string | undefined;
}

/** Watches each world's map export by polling its meta.json, and keeps each world's last good
 * render. A missing or empty directory is no export yet, not a failure. */
export class MapRenders {
  readonly #validate: ValidateFunction;
  readonly #palette: Palette;
  readonly #log: Log;
  readonly #worlds = new Map<string, Watched>();
  #nextAt = 0;
  #polling = false;

  constructor(validate: ValidateFunction, palette: Palette, log: Log) {
    this.#validate = validate;
    this.#palette = palette;
    this.#log = log;
  }

  /** Watch the generation's worlds: a new one, or one with a new path, starts with nothing; one
   * listed again as it was keeps its render; a gone one is dropped. */
  adopt(documents: Documents): void {
    const listed = new Map(documents.generation.worlds.map((world) => [world.name, world.map_export]));
    for (const [name, watched] of this.#worlds) {
      if (listed.get(name) !== watched.path) {
        this.#worlds.delete(name);
      }
    }
    for (const [name, path] of listed) {
      if (!this.#worlds.has(name)) {
        this.#worlds.set(name, { path, stamp: undefined, digest: undefined, renderedAt: Number.NEGATIVE_INFINITY, render: undefined, bad: undefined });
      }
    }
  }

  /** The names of the adopted generation's worlds, in its order. */
  names(): string[] {
    return [...this.#worlds.keys()];
  }

  /** The world's last good render, if it has one. */
  get(world: string): Render | undefined {
    return this.#worlds.get(world)?.render;
  }

  /** Each world whose newest export was refused, and why, while it is. */
  problems(): { world: string; reason: string }[] {
    return [...this.#worlds].flatMap(([world, { bad }]) => (bad === undefined ? [] : [{ world, reason: bad }]));
  }

  /** Look at each world's meta.json, once per EXPORT_POLL_MS, and render a changed one: one world
   * per poll, and none within RENDER_MIN_INTERVAL_MS of its last. A poll still running is not
   * started again. */
  async poll(now: number = Date.now()): Promise<void> {
    if (this.#polling || now < this.#nextAt) {
      return;
    }
    this.#polling = true;
    this.#nextAt = now + EXPORT_POLL_MS;
    try {
      for (const [name, watched] of this.#worlds) {
        if (await this.#look(name, watched, now)) {
          break;
        }
      }
    } finally {
      this.#polling = false;
    }
  }

  /** Whether it rendered. Nothing here throws: a failure is the world's, and settles as its
   * reason. */
  async #look(name: string, watched: Watched, now: number): Promise<boolean> {
    let seen: { stamp: string; digest: string } | undefined;
    let worked = false;
    try {
      const { body, stamp } = await readRegular(join(watched.path, META_FILE), META_FILE, MAX_META_BYTES);
      if (stamp === watched.stamp) {
        return false;
      }
      // A touched meta that says what it said costs nothing more.
      const digest = createHash("sha256").update(body).digest("hex");
      if (digest === watched.digest) {
        watched.stamp = stamp;
        return false;
      }
      seen = { stamp, digest };
      const meta = parseMeta(body, this.#validate, name);
      if (now - watched.renderedAt < RENDER_MIN_INTERVAL_MS) {
        return false; // looked at again next poll
      }
      watched.renderedAt = now;
      worked = true;
      const render = await renderOf(watched.path, meta, this.#palette);
      watched.stamp = seen.stamp;
      watched.digest = seen.digest;
      this.#settle(name, watched, render, undefined);
      this.#log(`world ${name}: rendered its map export, ${render.size} by ${render.size} pixels`);
      return true;
    } catch (error) {
      if (error instanceof Missing) {
        // No export yet: nothing to render, and nothing wrong.
        watched.stamp = watched.digest = undefined;
        this.#settle(name, watched, undefined, undefined);
        return false;
      }
      if (error instanceof Unreadable || !(error instanceof BadExport)) {
        watched.stamp = watched.digest = undefined;
        this.#settle(name, watched, undefined, error instanceof Unreadable ? error.message : UNEXPECTED);
        return worked;
      }
      if (seen !== undefined) {
        watched.stamp = seen.stamp;
        watched.digest = seen.digest;
      }
      this.#settle(name, watched, undefined, error.message);
      return worked;
    }
  }

  /** Take a good render, or note why the export is bad and keep the last good one. A bad export
   * is logged when its reason changes, not on every look. */
  #settle(name: string, watched: Watched, render: Render | undefined, bad: string | undefined): void {
    if (render !== undefined) {
      watched.render = render;
    }
    if (bad !== undefined && bad !== watched.bad) {
      this.#log(`world ${name}: map export refused: ${bad}${watched.render === undefined ? "" : "; its last good render stays"}`);
    }
    watched.bad = bad;
  }
}
