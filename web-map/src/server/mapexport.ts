// B5, reader side, and world render: each world's map export, read as untrusted input (the world's
// own container writes it), turned into a north-up base image the web map keeps in memory, by
// world. A bad export never replaces the last good render; it is logged once and reported in
// status. The image is not served here (docs/architecture.md § World render).

import { createHash } from "node:crypto";
import { closeSync, constants, fstatSync, lstatSync, openSync, readFileSync, readSync, type Stats } from "node:fs";
import { join } from "node:path";
import { Ajv2020, type ValidateFunction } from "ajv/dist/2020.js";
import type { Documents } from "./documents.js";
import { OCEAN, type Palette } from "./palette.js";
import { encodePng } from "./png.js";
import type { Log } from "./status.js";

export const META_FILE = "meta.json";
// The one file the reader reacts to: the plugin's dot-prefixed temporaries and every other name in
// the directory are never looked at.
/** How often each world's meta.json is looked at. An export changes rarely. */
export const EXPORT_POLL_MS = 5000;
/** The most a meta.json may hold; the plugin writes about 300 bytes plus the world's name. */
export const MAX_META_BYTES = 4096;
/** The largest raster a world may export, in pixels a side. The plugin writes 1000; this leaves
 * room, and bounds what a hostile export can make the web map read (2048 * 2048 * 10 bytes). */
export const MAX_SIZE_PX = 2048;
/** Height above the water at which the shading is brightest, in metres. */
export const HEIGHT_SPAN_M = 200;
/** Forest is where the raster's forest factor is below this (contract). */
export const FOREST_BELOW = 1.15;
// Shading is integer arithmetic on factors of 256, so the image is the same everywhere.
const SHADE_MIN = 160; // of 256, at the water line
const SHADE_RANGE = 96; // up to 256, at HEIGHT_SPAN_M above it
const FOREST_DARKEN = 208; // of 256

/** What a map export is refused for. The message says what, never the meta's values: the meta
 * names the seed, which the web map serves nothing of. */
export class BadExport extends Error {}

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

/** The bytes of a regular file inside `directory`, at most `max` of them. Refuses a link (lstat,
 * then O_NOFOLLOW), anything but a regular file, and a file that is not the one lstat saw. */
export function readRegular(path: string, name: string, max: number): Buffer {
  let before: Stats;
  try {
    before = lstatSync(path);
  } catch (error) {
    throw new BadExport(`cannot read ${name}: ${code(error)}`);
  }
  if (!before.isFile()) {
    throw new BadExport(`${name} is not a regular file`);
  }
  if (before.size > max) {
    throw new BadExport(`${name} is larger than ${max} bytes`);
  }
  let fd: number;
  try {
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  } catch (error) {
    throw new BadExport(`cannot read ${name}: ${code(error)}`);
  }
  try {
    const opened = fstatSync(fd);
    if (!opened.isFile() || opened.ino !== before.ino || opened.dev !== before.dev) {
      throw new BadExport(`${name} changed while it was opened`);
    }
    if (opened.size > max) {
      throw new BadExport(`${name} is larger than ${max} bytes`);
    }
    const body = Buffer.alloc(opened.size);
    let at = 0;
    while (at < body.length) {
      const got = readSync(fd, body, at, body.length - at, at);
      if (got === 0) {
        throw new BadExport(`${name} is shorter than it was`);
      }
      at += got;
    }
    return body;
  } catch (error) {
    if (error instanceof BadExport) {
      throw error;
    }
    throw new BadExport(`cannot read ${name}: ${code(error)}`);
  } finally {
    closeSync(fd);
  }
}

function code(error: unknown): string {
  return (error as NodeJS.ErrnoException).code ?? "failed";
}

/** The B5 schema, compiled. */
export function metaValidator(contracts: string): ValidateFunction {
  const schema = JSON.parse(readFileSync(join(contracts, "plugin.map-export.schema.json"), "utf8")) as object;
  return new Ajv2020({ strict: true }).compile(schema);
}

/** The meta in `directory`, valid against B5 and its reader's rule on bytes. */
export function readMeta(directory: string, validate: ValidateFunction): Meta {
  const text = readRegular(join(directory, META_FILE), META_FILE, MAX_META_BYTES).toString("utf8");
  let document: unknown;
  try {
    document = JSON.parse(text);
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
  return meta;
}

/** The raster the meta names, whole and checked against the meta's length and SHA-256. */
export function readRaster(directory: string, meta: Meta): Buffer {
  const raster = readRegular(join(directory, meta.raster.file), meta.raster.file, meta.raster.bytes);
  if (raster.length !== meta.raster.bytes) {
    throw new BadExport(`${meta.raster.file} is not ${META_FILE}'s length`);
  }
  if (createHash("sha256").update(raster).digest("hex") !== meta.raster.sha256) {
    throw new BadExport(`${meta.raster.file} does not match ${META_FILE}'s SHA-256`);
  }
  return raster;
}

/** The raster as north-up RGBA, one pixel per raster pixel, row 0 north as the contract lays it
 * out. Water is at or below the water level, and the Ocean biome; land has its biome's colour,
 * brighter with height above the water and darker under forest. Throws BadExport on a value the
 * contract says a raster never holds. */
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

/** The export in `directory` as a render. Throws BadExport when any rule of B5 fails. */
export function renderExport(directory: string, validate: ValidateFunction, palette: Palette): Render {
  const meta = readMeta(directory, validate);
  const rgba = renderRgba(meta, readRaster(directory, meta), palette);
  const { size, half_width } = meta.geometry;
  return { png: encodePng(size, size, rgba), size, half_width, water_level: meta.water_level };
}

interface Watched {
  path: string;
  /** Which meta.json was last read, good or bad. */
  stamp: string | undefined;
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

  constructor(validate: ValidateFunction, palette: Palette, log: Log) {
    this.#validate = validate;
    this.#palette = palette;
    this.#log = log;
  }

  /** Watch the generation's worlds: a new one starts with nothing, a gone one is dropped. */
  adopt(documents: Documents): void {
    const listed = new Map(documents.generation.worlds.map((world) => [world.name, world.map_export]));
    for (const [name, watched] of this.#worlds) {
      if (listed.get(name) !== watched.path) {
        this.#worlds.delete(name);
      }
    }
    for (const [name, path] of listed) {
      if (!this.#worlds.has(name)) {
        this.#worlds.set(name, { path, stamp: undefined, render: undefined, bad: undefined });
      }
    }
  }

  /** The world's last good render, if it has one. */
  get(world: string): Render | undefined {
    return this.#worlds.get(world)?.render;
  }

  /** Why each world's newest export was refused, one line each, while it is. */
  problems(): string[] {
    return [...this.#worlds].flatMap(([name, { bad }]) => (bad === undefined ? [] : [`world ${name}: map export refused: ${bad}`]));
  }

  /** Look at each world's meta.json, once per EXPORT_POLL_MS, and render a new one. */
  poll(now: number = Date.now()): void {
    if (now < this.#nextAt) {
      return;
    }
    this.#nextAt = now + EXPORT_POLL_MS;
    for (const [name, watched] of this.#worlds) {
      this.#look(name, watched);
    }
  }

  #look(name: string, watched: Watched): void {
    const meta = join(watched.path, META_FILE);
    let stamp: string;
    try {
      const info = lstatSync(meta);
      stamp = `${info.ino}:${info.mtimeMs}:${info.size}`;
    } catch (error) {
      const missing = (error as NodeJS.ErrnoException).code === "ENOENT" || (error as NodeJS.ErrnoException).code === "ENOTDIR";
      // No export yet: nothing to render, and nothing wrong.
      this.#settle(name, watched, undefined, missing ? undefined : `cannot read ${META_FILE}: ${code(error)}`);
      watched.stamp = undefined;
      return;
    }
    if (stamp === watched.stamp) {
      return;
    }
    watched.stamp = stamp;
    try {
      const render = renderExport(watched.path, this.#validate, this.#palette);
      this.#settle(name, watched, render, undefined);
      this.#log(`world ${name}: rendered its map export, ${render.size} by ${render.size} pixels`);
    } catch (error) {
      if (!(error instanceof BadExport)) {
        throw error;
      }
      this.#settle(name, watched, undefined, error.message);
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
