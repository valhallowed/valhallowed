import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, link, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import { crc32, inflateSync } from "node:zlib";
import type { Documents } from "../server/documents.js";
import {
  BadExport,
  EXPORT_POLL_MS,
  MAX_META_BYTES,
  MAX_SIZE_PX,
  MapRenders,
  metaValidator,
  readMeta,
  readRaster,
  renderExport,
  renderRgba,
  RENDER_MIN_INTERVAL_MS,
  type Meta,
} from "../server/mapexport.js";
import { readPalette, type Palette, type Rgb } from "../server/palette.js";
import { CONTRACTS, DESIGN, EXAMPLES, GOLDEN } from "./paths.js";

const SYNTHETIC = "plugin.map-export-synthetic";
const WORLD = "Meadowfold";
const validate = metaValidator(CONTRACTS);
const palette = readPalette(join(DESIGN, "tokens.css"));
const META = JSON.parse(readFileSync(join(EXAMPLES, `${SYNTHETIC}.json`), "utf8")) as Meta;
const RASTER = readFileSync(join(EXAMPLES, `${SYNTHETIC}.raster.bin`));
const GOLDEN_RGBA = readFileSync(join(GOLDEN, `${SYNTHETIC}.rgba`));
const SIZE = META.geometry.size;
const COUNT = SIZE * SIZE;

const made: string[] = [];
afterEach(() => {
  for (const dir of made.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function directory(): string {
  const dir = mkdtempSync(join(tmpdir(), "map-export-"));
  made.push(dir);
  return dir;
}

// Written whole, then renamed into place, as the plugin writes them.
function put(dir: string, name: string, body: Buffer | string): void {
  writeFileSync(join(dir, `.${name}.tmp`), body);
  renameSync(join(dir, `.${name}.tmp`), join(dir, name));
}

function exportInto(dir: string, meta: object = META, raster: Buffer = RASTER): void {
  put(dir, "raster.bin", raster);
  put(dir, "meta.json", JSON.stringify(meta));
}

function metaWith(change: (meta: Meta) => void): Meta {
  const meta = structuredClone(META);
  change(meta);
  return meta;
}

function documents(worlds: { name: string; map_export: string }[]): Documents {
  return { generation: { worlds } } as unknown as Documents;
}

/** Each poll is one poll interval, and one render interval's worth of polls apart as `step`. */
function watching(...paths: string[]): { renders: MapRenders; logged: string[]; poll: (step?: number) => Promise<void> } {
  const logged: string[] = [];
  const renders = new MapRenders(validate, palette, (line) => logged.push(line));
  renders.adopt(documents(paths.map((map_export, i) => ({ name: i === 0 ? WORLD : `World${i}`, map_export }))));
  let now = 0;
  return { renders, logged, poll: (step = RENDER_MIN_INTERVAL_MS) => renders.poll((now += step)) };
}

/** A PNG's pixels, decoded: this encoder writes one IDAT and filter 0 on every row. Every chunk's
 * CRC is checked, and the last chunk is an empty IEND. */
function decode(png: Buffer): { width: number; height: number; rgba: Buffer } {
  assert.deepEqual([...png.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const width = png.readUInt32BE(16);
  const height = png.readUInt32BE(20);
  assert.deepEqual([...png.subarray(24, 26)], [8, 6]);
  const parts: Buffer[] = [];
  let last = "";
  let lastLength = -1;
  for (let at = 8; at < png.length; ) {
    const length = png.readUInt32BE(at);
    last = png.toString("latin1", at + 4, at + 8);
    lastLength = length;
    assert.equal(png.readUInt32BE(at + 8 + length), crc32(png.subarray(at + 4, at + 8 + length)), `${last}'s CRC`);
    if (last === "IDAT") {
      parts.push(png.subarray(at + 8, at + 8 + length));
    }
    at += length + 12;
  }
  assert.equal(last, "IEND");
  assert.equal(lastLength, 0);
  const raw = inflateSync(Buffer.concat(parts));
  const rows: Buffer[] = [];
  for (let row = 0; row < height; row++) {
    assert.equal(raw[row * (width * 4 + 1)], 0);
    rows.push(raw.subarray(row * (width * 4 + 1) + 1, (row + 1) * (width * 4 + 1)));
  }
  return { width, height, rgba: Buffer.concat(rows) };
}

function pixel(rgba: Uint8Array, column: number, row: number): number[] {
  const at = (row * SIZE + column) * 4;
  return [...rgba.subarray(at, at + 4)];
}

/** What a raster says of a pixel, read from the file's planes and nothing else. */
function sample(raster: Buffer, column: number, row: number): { biome: number; height: number; forest: number } {
  const i = row * SIZE + column;
  return { biome: raster.readUInt16LE(i * 2), height: raster.readFloatLE(COUNT * 2 + i * 4), forest: raster.readFloatLE(COUNT * 6 + i * 4) };
}

/** A copy of the raster with one pixel changed. */
function withPixel(column: number, row: number, biome: number, height: number): Buffer {
  const raster = Buffer.from(RASTER);
  const i = row * SIZE + column;
  raster.writeUInt16LE(biome, i * 2);
  raster.writeFloatLE(height, COUNT * 2 + i * 4);
  return raster;
}

/** The renderer's shading rule, restated: brightness 160/256 at the water line up to 256/256 at
 * 200 m above it, and 208/256 of that under forest (a factor below 1.15). */
function shaded(colour: Rgb, height: number, forest: number): number[] {
  const steps = Math.floor(96 * Math.min(1, (height - META.water_level) / 200));
  const brightness = (160 + steps) / 256;
  const darker = forest < 1.15 ? 208 / 256 : 1;
  return [...colour.map((c) => Math.floor(c * brightness * darker)), 255];
}

/** Whether `out` is `colour` times one scalar k in [0, 1], floored per channel. */
function isScaled(out: number[], colour: Rgb): boolean {
  let low = 0;
  let high = 1;
  for (let i = 0; i < 3; i++) {
    const c = colour[i] as number;
    const o = out[i] as number;
    if (c === 0) {
      if (o !== 0) return false;
      continue;
    }
    low = Math.max(low, o / c);
    high = Math.min(high, (o + 1) / c);
  }
  return low < high || (low === 1 && high >= 1);
}

test("the synthesized export renders to the committed image", () => {
  const rgba = renderRgba(META, RASTER, palette);
  assert.equal(rgba.length, COUNT * 4);
  assert.ok(Buffer.from(rgba).equals(GOLDEN_RGBA), "the render differs from golden/*.rgba");
});

test("every pixel is the water colour where the rules say water, and its biome's colour dimmed otherwise", () => {
  const rgba = renderRgba(META, RASTER, palette);
  let water = 0;
  let land = 0;
  for (let row = 0; row < SIZE; row++) {
    for (let column = 0; column < SIZE; column++) {
      const { biome, height } = sample(RASTER, column, row);
      const got = pixel(rgba, column, row);
      if (height <= META.water_level || biome === 256) {
        assert.deepEqual(got, [...palette.water, 255], `water at ${column},${row}`);
        water++;
      } else {
        assert.ok(isScaled(got.slice(0, 3), palette.biomes.get(biome) as Rgb), `land at ${column},${row}`);
        assert.equal(got[3], 255);
        land++;
      }
    }
  }
  assert.ok(water > 0 && land > 0);
});

test("a few pixels are what the rules say: biome colour, height shading, forest, north up", () => {
  const rgba = renderRgba(META, RASTER, palette);
  assert.equal(sample(RASTER, 0, 0).height, META.water_level);
  assert.deepEqual(pixel(rgba, 0, 0), [...palette.water, 255]);
  // Mountain, not forest; plains, not forest; meadows, forest.
  for (const [column, row, biome] of [[12, 0, 4], [12, 5, 16], [6, 4, 1]] as const) {
    const { biome: found, height, forest } = sample(RASTER, column, row);
    assert.equal(found, biome);
    assert.deepEqual(pixel(rgba, column, row), shaded(palette.biomes.get(biome) as Rgb, height, forest), `${column},${row}`);
  }
  assert.ok(sample(RASTER, 6, 4).forest < 1.15 && sample(RASTER, 12, 5).forest >= 1.15);
});

test("water is height at or below the water level, whatever the biome; Ocean is water at any height", () => {
  const water = [...palette.water, 255];
  // The synthetic export has no land pixel at or below the water level, so make them.
  assert.deepEqual(pixel(renderRgba(META, withPixel(8, 8, 1, META.water_level), palette), 8, 8), water, "meadows exactly at the level");
  assert.deepEqual(pixel(renderRgba(META, withPixel(8, 8, 4, META.water_level - 1), palette), 8, 8), water, "mountain below it");
  assert.notDeepEqual(pixel(renderRgba(META, withPixel(8, 8, 1, META.water_level + 0.5), palette), 8, 8), water, "meadows just above it");
  assert.deepEqual(pixel(renderRgba(META, withPixel(8, 8, 256, META.water_level + 100), palette), 8, 8), water, "Ocean above it");
});

test("the PNG decodes to the render, and the render holds geometry and no meta", async () => {
  const dir = directory();
  exportInto(dir);
  const render = await renderExport(dir, validate, palette, WORLD);
  const { width, height, rgba } = decode(render.png);
  assert.deepEqual([width, height], [SIZE, SIZE]);
  assert.ok(rgba.equals(GOLDEN_RGBA));
  assert.deepEqual(Object.keys(render).sort(), ["half_width", "png", "size", "water_level"]);
  assert.deepEqual({ ...render, png: undefined }, { png: undefined, size: 16, half_width: 10500, water_level: 30 });
});

test("a world's export is picked up, and nothing of its seed is kept", async () => {
  const dir = directory();
  exportInto(dir);
  const { renders, logged, poll } = watching(dir);
  await poll();
  const render = renders.get(WORLD);
  assert.equal(render?.size, 16);
  const everything = JSON.stringify([render, renders.problems(), logged]);
  assert.ok(!everything.includes("Hildir") && !everything.includes("1234567"), "the seed leaked");
  assert.ok(!render?.png.includes("Hildir"));
});

test("a missing or empty directory is no export yet, and one that appears renders", async () => {
  const root = directory();
  const missing = join(root, "absent");
  const empty = join(root, "empty");
  mkdirSync(empty);
  const { renders, logged, poll } = watching(missing, empty);
  await poll();
  assert.equal(renders.get(WORLD), undefined);
  assert.deepEqual(renders.problems(), []);
  assert.deepEqual(logged, []);
  mkdirSync(missing);
  exportInto(missing);
  await poll();
  assert.equal(renders.get(WORLD)?.size, 16);
  assert.equal(renders.get("World1"), undefined);
});

test("temporaries and other files are ignored", async () => {
  const dir = directory();
  writeFileSync(join(dir, ".meta.json.4242.tmp"), "not json");
  writeFileSync(join(dir, ".raster.bin.4242.tmp"), "half a raster");
  writeFileSync(join(dir, "notes.txt"), "hello");
  const { renders, logged, poll } = watching(dir);
  await poll();
  assert.equal(renders.get(WORLD), undefined);
  assert.deepEqual(renders.problems(), []);
  exportInto(dir);
  writeFileSync(join(dir, ".meta.json.9.tmp"), "{broken");
  await poll();
  assert.equal(renders.get(WORLD)?.size, 16);
  assert.deepEqual(renders.problems(), []);
  assert.equal(logged.length, 1);
});

test("a raster that does not match its meta keeps the last good render and is reported once", async () => {
  const dir = directory();
  exportInto(dir);
  const { renders, logged, poll } = watching(dir);
  await poll();
  const good = renders.get(WORLD);
  assert.ok(good);
  const damaged = Buffer.from(RASTER);
  damaged.writeUInt8(damaged.readUInt8(0) ^ 1, 0);
  exportInto(dir, metaWith((meta) => (meta.water_level = 29)), damaged);
  await poll();
  assert.equal(renders.get(WORLD), good);
  assert.deepEqual(renders.problems(), [{ world: WORLD, reason: "raster.bin does not match meta.json's SHA-256" }]);
  // Polled again with the same meta, then with a newer meta that fails the same way: logged once.
  await poll();
  exportInto(dir, metaWith((meta) => (meta.generator_version = 3)), damaged);
  await poll();
  assert.equal(logged.filter((line) => line.includes("refused")).length, 1);
  assert.ok(logged.at(-1)?.includes("last good render stays"));
  // The next good export replaces the render and clears the report.
  exportInto(dir, metaWith((meta) => (meta.water_level = 31)));
  await poll();
  assert.notEqual(renders.get(WORLD), good);
  assert.equal(renders.get(WORLD)?.water_level, 31);
  assert.deepEqual(renders.problems(), []);
});

test("a meta touched without changing costs no render", async () => {
  const dir = directory();
  exportInto(dir);
  const { renders, logged, poll } = watching(dir);
  await poll();
  const good = renders.get(WORLD);
  for (let i = 0; i < 3; i++) {
    put(dir, "meta.json", JSON.stringify(META));
    await poll();
  }
  assert.equal(renders.get(WORLD), good);
  assert.equal(logged.length, 1);
});

test("a world renders at most once per minimum interval, and one world per poll", async () => {
  const [first, second] = [directory(), directory()];
  exportInto(first);
  exportInto(second, metaWith((meta) => (meta.world = "World1")));
  const { renders, poll } = watching(first, second);
  await poll();
  assert.equal(renders.get(WORLD)?.size, 16);
  assert.equal(renders.get("World1"), undefined, "the second world waits for the next poll");
  await poll(EXPORT_POLL_MS);
  assert.equal(renders.get("World1")?.size, 16);
  // A changed export inside the interval waits for it.
  const before = renders.get(WORLD);
  exportInto(first, metaWith((meta) => (meta.water_level = 31)));
  await poll(EXPORT_POLL_MS);
  assert.equal(renders.get(WORLD), before);
  await poll(RENDER_MIN_INTERVAL_MS);
  assert.equal(renders.get(WORLD)?.water_level, 31);
});

test("a refused raster waits for a new meta; a raster that cannot be opened is tried again with the same meta", async (t) => {
  const dir = directory();
  exportInto(dir);
  const { renders, poll } = watching(dir);
  renameSync(join(dir, "raster.bin"), join(dir, "raster.real"));
  mkdirSync(join(dir, "raster.bin"));
  await poll();
  assert.deepEqual(renders.problems(), [{ world: WORLD, reason: "raster.bin is not a regular file" }]);
  rmSync(join(dir, "raster.bin"), { recursive: true });
  renameSync(join(dir, "raster.real"), join(dir, "raster.bin"));
  // A content refusal waits for a new meta: the same meta is not read again.
  await poll();
  assert.equal(renders.get(WORLD), undefined);
  put(dir, "meta.json", JSON.stringify(META) + "\n");
  await poll();
  assert.equal(renders.get(WORLD)?.size, 16);
  if (process.getuid?.() === 0) {
    t.skip("root opens any file, so EACCES cannot be made");
    return;
  }
  const next = directory();
  exportInto(next);
  chmodSync(join(next, "raster.bin"), 0);
  const again = watching(next);
  await again.poll();
  assert.match(again.renders.problems()[0]?.reason ?? "", /cannot read raster.bin: EACCES/);
  chmodSync(join(next, "raster.bin"), 0o644);
  await again.poll();
  assert.equal(again.renders.get(WORLD)?.size, 16);
  assert.deepEqual(again.renders.problems(), []);
});

test("an unexpected failure is reported as a fixed reason and the world is tried again", async () => {
  const dir = directory();
  exportInto(dir);
  const logged: string[] = [];
  let broken = true;
  const failing = { ...palette, biomes: new Proxy(palette.biomes, { get(target, key) {
    if (broken && key === "get") throw new Error("seed Hildir 1234567");
    const value = Reflect.get(target, key, target) as unknown;
    return typeof value === "function" ? value.bind(target) : value;
  } }) } as Palette;
  const renders = new MapRenders(validate, failing, (line) => logged.push(line));
  renders.adopt(documents([{ name: WORLD, map_export: dir }]));
  await renders.poll(RENDER_MIN_INTERVAL_MS);
  assert.deepEqual(renders.problems(), [{ world: WORLD, reason: "render failed" }]);
  assert.ok(!JSON.stringify(logged).includes("Hildir"));
  broken = false;
  await renders.poll(2 * RENDER_MIN_INTERVAL_MS);
  assert.equal(renders.get(WORLD)?.size, 16);
  assert.deepEqual(renders.problems(), []);
});

test("a raster of another length than its meta's is refused", async () => {
  const dir = directory();
  exportInto(dir, META, RASTER.subarray(0, 100));
  await assert.rejects(async () => readRaster(dir, await readMeta(dir, validate, WORLD)), /not meta.json's length/);
  exportInto(dir, META, Buffer.concat([RASTER, Buffer.alloc(10)]));
  await assert.rejects(async () => readRaster(dir, await readMeta(dir, validate, WORLD)), /larger than 2560 bytes/);
  rmSync(join(dir, "raster.bin"));
  await assert.rejects(async () => readRaster(dir, await readMeta(dir, validate, WORLD)), /raster.bin is missing/);
});

test("a meta that breaks B5, or names another world, is refused without echoing its values", async () => {
  for (const meta of [
    metaWith((m) => ((m.raster as { file: string }).file = "other.bin")),
    metaWith((m) => ((m.geometry as { size: number }).size = 0)),
    { ...META, extra: 1 },
    metaWith((m) => (m.raster.bytes = 2561)),
    metaWith((m) => (m.world = "Somewhere")),
  ]) {
    const dir = directory();
    exportInto(dir, meta);
    await assert.rejects(readMeta(dir, validate, WORLD), (error: Error) => error instanceof BadExport && !error.message.includes("Somewhere"));
  }
  const dir = directory();
  put(dir, "meta.json", `{"seed":{"name":"Hildir" "value":1}}`);
  await assert.rejects(readMeta(dir, validate, WORLD), (error: Error) => error instanceof BadExport && !error.message.includes("Hildir"));
});

test("a raster holding a biome, height or forest factor the contract rules out is refused", () => {
  const unknownBiome = Buffer.from(RASTER);
  unknownBiome.writeUInt16LE(3, 0);
  const infiniteHeight = Buffer.from(RASTER);
  infiniteHeight.writeFloatLE(Number.POSITIVE_INFINITY, COUNT * 2);
  const nanForest = Buffer.from(RASTER);
  nanForest.writeFloatLE(Number.NaN, COUNT * 6);
  for (const raster of [unknownBiome, infiniteHeight, nanForest]) {
    assert.throws(() => renderRgba(META, raster, palette), BadExport);
  }
});

test("a symlinked meta or raster is refused", async () => {
  const target = directory();
  exportInto(target);
  for (const name of ["meta.json", "raster.bin"]) {
    const dir = directory();
    exportInto(dir);
    renameSync(join(dir, name), join(dir, `${name}.real`));
    symlinkSync(join(target, name), join(dir, name));
    const { renders, poll } = watching(dir);
    await poll();
    assert.equal(renders.get(WORLD), undefined, name);
    assert.deepEqual(renders.problems(), [{ world: WORLD, reason: `${name} is not a regular file` }], name);
  }
});

test("a file with another hard link is refused", async () => {
  for (const name of ["meta.json", "raster.bin"]) {
    const dir = directory();
    exportInto(dir);
    await new Promise<void>((resolve, reject) => link(join(dir, name), join(dir, `${name}.again`), (error) => (error ? reject(error) : resolve())));
    await assert.rejects(renderExport(dir, validate, palette, WORLD), /more than one link/, name);
  }
});

test("a meta or raster larger than its bound is refused", async () => {
  const dir = directory();
  put(dir, "meta.json", " ".repeat(MAX_META_BYTES + 1));
  await assert.rejects(readMeta(dir, validate, WORLD), /larger than 4096 bytes/);
  const huge = metaWith((m) => {
    m.geometry.size = MAX_SIZE_PX + 1;
    m.raster.bytes = (MAX_SIZE_PX + 1) ** 2 * 10;
  });
  exportInto(dir, huge);
  await assert.rejects(readMeta(dir, validate, WORLD), /larger than 2048 pixels/);
});

test("a directory where a file belongs is refused", async () => {
  const dir = directory();
  mkdirSync(join(dir, "meta.json"));
  await assert.rejects(readMeta(dir, validate, WORLD), /not a regular file/);
});

test("a named pipe where the meta belongs is refused without being opened", async (t) => {
  const dir = directory();
  try {
    execFileSync("mkfifo", [join(dir, "meta.json")]);
  } catch {
    t.skip("mkfifo is not available");
    return;
  }
  await assert.rejects(readMeta(dir, validate, WORLD), /not a regular file/);
});

test("a world that leaves the generation is dropped, one listed as it was keeps its render, one with a new path starts fresh", async () => {
  const [dir, other] = [directory(), directory()];
  exportInto(dir);
  const { renders, poll } = watching(dir);
  await poll();
  const render = renders.get(WORLD);
  assert.ok(render);
  renders.adopt(documents([{ name: WORLD, map_export: dir }]));
  assert.equal(renders.get(WORLD), render);
  renders.adopt(documents([{ name: WORLD, map_export: other }]));
  assert.equal(renders.get(WORLD), undefined);
  renders.adopt(documents([]));
  assert.equal(renders.get(WORLD), undefined);
});

test("every palette token reads, and a missing one fails", () => {
  const colours: Palette = palette;
  assert.equal(colours.biomes.size, 8);
  const css = join(directory(), "tokens.css");
  writeFileSync(css, ":root { --map-water: #112233; }");
  assert.throws(() => readPalette(css), /--map-meadows/);
});

test("a token in a comment, or outside the first :root block, is not read", () => {
  const dir = directory();
  const read = readFileSync(join(DESIGN, "tokens.css"), "utf8");
  const tokens = [...read.matchAll(/--map-[a-z-]+:\s*#[0-9a-f]{6};/g)].map((match) => match[0]).join("\n");
  const css = join(dir, "tokens.css");
  writeFileSync(css, `:root { /* --map-water: #000000; */ --map-water: #112233; ${tokens.replace(/--map-water:\s*#[0-9a-f]{6};/, "")} }\n:root[data-theme="x"] { --map-water: #ffffff; }`);
  assert.deepEqual(readPalette(css).water, [0x11, 0x22, 0x33]);
  writeFileSync(css, `:root { color: red; }\n:root[data-theme="x"] { ${tokens} }`);
  assert.throws(() => readPalette(css), /--map-water/);
});
