import assert from "node:assert/strict";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, renameSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { inflateSync } from "node:zlib";
import type { Documents } from "../server/documents.js";
import {
  BadExport,
  EXPORT_POLL_MS,
  MAX_META_BYTES,
  MAX_SIZE_PX,
  MapRenders,
  metaValidator,
  readMeta,
  renderExport,
  renderRgba,
  readRaster,
  type Meta,
} from "../server/mapexport.js";
import { readPalette, type Palette, type Rgb } from "../server/palette.js";
import { CONTRACTS, DESIGN, EXAMPLES, GOLDEN } from "./paths.js";

const SYNTHETIC = "plugin.map-export-synthetic";
const validate = metaValidator(CONTRACTS);
const palette = readPalette(join(DESIGN, "tokens.css"));
const META = JSON.parse(readFileSync(join(EXAMPLES, `${SYNTHETIC}.json`), "utf8")) as Meta;
const RASTER = readFileSync(join(EXAMPLES, `${SYNTHETIC}.raster.bin`));

function directory(): string {
  return mkdtempSync(join(tmpdir(), "map-export-"));
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

function watching(...paths: string[]): { renders: MapRenders; logged: string[]; poll: () => void } {
  const logged: string[] = [];
  const renders = new MapRenders(validate, palette, (line) => logged.push(line));
  renders.adopt({ generation: { worlds: paths.map((map_export, i) => ({ name: `World${i}`, map_export })) } } as unknown as Documents);
  let now = 0;
  return { renders, logged, poll: () => renders.poll((now += EXPORT_POLL_MS)) };
}

/** A PNG's pixels, decoded: this encoder writes one IDAT and filter 0 on every row. */
function decode(png: Buffer): { width: number; height: number; rgba: Buffer } {
  assert.deepEqual([...png.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const width = png.readUInt32BE(16);
  const height = png.readUInt32BE(20);
  assert.deepEqual([...png.subarray(24, 26)], [8, 6]);
  const parts: Buffer[] = [];
  for (let at = 8; at < png.length; ) {
    const length = png.readUInt32BE(at);
    if (png.toString("latin1", at + 4, at + 8) === "IDAT") {
      parts.push(png.subarray(at + 8, at + 8 + length));
    }
    at += length + 12;
  }
  const raw = inflateSync(Buffer.concat(parts));
  const rows: Buffer[] = [];
  for (let row = 0; row < height; row++) {
    assert.equal(raw[row * (width * 4 + 1)], 0);
    rows.push(raw.subarray(row * (width * 4 + 1) + 1, (row + 1) * (width * 4 + 1)));
  }
  return { width, height, rgba: Buffer.concat(rows) };
}

function pixel(rgba: Uint8Array, column: number, row: number, size = META.geometry.size): number[] {
  const at = (row * size + column) * 4;
  return [...rgba.subarray(at, at + 4)];
}

/** What the raster says of a pixel, read from the file's planes and nothing else. */
function sample(column: number, row: number): { biome: number; height: number; forest: number } {
  const i = row * META.geometry.size + column;
  const n = META.geometry.size ** 2;
  return { biome: RASTER.readUInt16LE(i * 2), height: RASTER.readFloatLE(n * 2 + i * 4), forest: RASTER.readFloatLE(n * 6 + i * 4) };
}

/** A land pixel's colour from the palette, worked out by the rules in the contract's words. */
function shaded(colour: Rgb, height: number, forest: number): number[] {
  const steps = Math.floor((96 * Math.min(1, (height - META.water_level) / 200)));
  const brightness = (160 + steps) / 256;
  const darker = forest < 1.15 ? 208 / 256 : 1;
  return [...colour.map((c) => Math.floor(c * brightness * darker)), 255];
}

test("the synthesized export renders to the committed image", () => {
  const rgba = renderRgba(META, RASTER, palette);
  assert.equal(rgba.length, 16 * 16 * 4);
  assert.ok(Buffer.from(rgba).equals(readFileSync(join(GOLDEN, `${SYNTHETIC}.rgba`))), "the render differs from golden/*.rgba");
});

test("the render's pixels follow the raster: water, biomes, shading, forest, north up", () => {
  const rgba = renderRgba(META, RASTER, palette);
  // Row 0 is north and column 0 west: the Ocean corner at exactly the water level is water.
  assert.deepEqual(sample(0, 0), { biome: 256, height: 30, forest: sample(0, 0).forest });
  assert.deepEqual(pixel(rgba, 0, 0), [...palette.water, 255]);
  // A pixel below the water level is water, its biome apart.
  assert.deepEqual(pixel(rgba, 0, 15), [...palette.water, 255]);
  // Mountain, not forest; plains, not forest; meadows, forest.
  for (const [column, row, biome] of [[12, 0, 4], [12, 5, 16], [6, 4, 1]] as const) {
    const { biome: found, height, forest } = sample(column, row);
    assert.equal(found, biome);
    assert.deepEqual(pixel(rgba, column, row), shaded(palette.biomes.get(biome) as Rgb, height, forest), `${column},${row}`);
  }
  assert.ok(sample(6, 4).forest < 1.15 && sample(12, 5).forest >= 1.15);
  // Every pixel opaque.
  for (let i = 3; i < rgba.length; i += 4) assert.equal(rgba[i], 255);
});

test("the PNG decodes to the render, and the render holds geometry and no meta", () => {
  const dir = directory();
  exportInto(dir);
  const render = renderExport(dir, validate, palette);
  const { width, height, rgba } = decode(render.png);
  assert.deepEqual([width, height], [16, 16]);
  assert.ok(rgba.equals(readFileSync(join(GOLDEN, `${SYNTHETIC}.rgba`))));
  assert.deepEqual(Object.keys(render).sort(), ["half_width", "png", "size", "water_level"]);
  assert.deepEqual({ ...render, png: undefined }, { png: undefined, size: 16, half_width: 10500, water_level: 30 });
});

test("a world's export is picked up, and its render keeps neither seed nor name of one", () => {
  const dir = directory();
  exportInto(dir);
  const { renders, logged, poll } = watching(dir);
  poll();
  const render = renders.get("World0");
  assert.equal(render?.size, 16);
  const everything = JSON.stringify([render, renders.problems(), logged]);
  assert.ok(!everything.includes("Hildir") && !everything.includes("1234567"), "the seed leaked");
  assert.ok(!render?.png.includes("Hildir"));
});

test("a missing or empty directory is no export yet, and one that appears renders", () => {
  const root = directory();
  const missing = join(root, "absent");
  const empty = join(root, "empty");
  mkdirSync(empty);
  const { renders, logged, poll } = watching(missing, empty);
  poll();
  assert.equal(renders.get("World0"), undefined);
  assert.deepEqual(renders.problems(), []);
  assert.deepEqual(logged, []);
  mkdirSync(missing);
  exportInto(missing);
  poll();
  assert.equal(renders.get("World0")?.size, 16);
  assert.equal(renders.get("World1"), undefined);
});

test("temporaries and other files are ignored", () => {
  const dir = directory();
  writeFileSync(join(dir, ".meta.json.4242.tmp"), "not json");
  writeFileSync(join(dir, ".raster.bin.4242.tmp"), "half a raster");
  writeFileSync(join(dir, "notes.txt"), "hello");
  const { renders, logged, poll } = watching(dir);
  poll();
  assert.equal(renders.get("World0"), undefined);
  assert.deepEqual(renders.problems(), []);
  exportInto(dir);
  writeFileSync(join(dir, ".meta.json.9.tmp"), "{broken");
  poll();
  assert.equal(renders.get("World0")?.size, 16);
  assert.deepEqual(renders.problems(), []);
  assert.equal(logged.length, 1);
});

test("a raster that does not match its meta keeps the last good render and is reported once", () => {
  const dir = directory();
  exportInto(dir);
  const { renders, logged, poll } = watching(dir);
  poll();
  const good = renders.get("World0");
  assert.ok(good);
  const damaged = Buffer.from(RASTER);
  damaged.writeUInt8(damaged.readUInt8(0) ^ 1, 0);
  exportInto(dir, META, damaged);
  poll();
  assert.equal(renders.get("World0"), good);
  assert.deepEqual(renders.problems(), ["world World0: map export refused: raster.bin does not match meta.json's SHA-256"]);
  // Polled again with the same meta, then with a newer meta that fails the same way: logged once.
  poll();
  exportInto(dir, metaWith((meta) => (meta.generator_version = 3)), damaged);
  poll();
  assert.equal(logged.filter((line) => line.includes("refused")).length, 1);
  assert.ok(logged.at(-1)?.includes("last good render stays"));
  // The next good export replaces the render and clears the report.
  exportInto(dir, metaWith((meta) => (meta.water_level = 31)));
  poll();
  assert.notEqual(renders.get("World0"), good);
  assert.equal(renders.get("World0")?.water_level, 31);
  assert.deepEqual(renders.problems(), []);
});

test("a raster of another length than its meta's is refused", () => {
  const dir = directory();
  exportInto(dir, META, RASTER.subarray(0, 100));
  assert.throws(() => readRaster(dir, readMeta(dir, validate)), BadExport);
  exportInto(dir, META, Buffer.concat([RASTER, Buffer.alloc(10)]));
  assert.throws(() => readRaster(dir, readMeta(dir, validate)), /larger than 2560 bytes/);
});

test("a meta that breaks B5 is refused without echoing its values", () => {
  for (const meta of [
    metaWith((m) => ((m.raster as { file: string }).file = "other.bin")),
    metaWith((m) => ((m.geometry as { size: number }).size = 0)),
    { ...META, extra: 1 },
    metaWith((m) => (m.raster.bytes = 2561)),
  ]) {
    const dir = directory();
    exportInto(dir, meta);
    assert.throws(() => readMeta(dir, validate), BadExport);
  }
  const dir = directory();
  put(dir, "meta.json", `{"seed":{"name":"Hildir" "value":1}}`);
  assert.throws(() => readMeta(dir, validate), (error: Error) => error instanceof BadExport && !error.message.includes("Hildir"));
});

test("a raster holding a biome, height or forest factor the contract rules out is refused", () => {
  const unknownBiome = Buffer.from(RASTER);
  unknownBiome.writeUInt16LE(3, 0);
  const infiniteHeight = Buffer.from(RASTER);
  infiniteHeight.writeFloatLE(Number.POSITIVE_INFINITY, 512);
  const nanForest = Buffer.from(RASTER);
  nanForest.writeFloatLE(Number.NaN, 1536);
  for (const raster of [unknownBiome, infiniteHeight, nanForest]) {
    assert.throws(() => renderRgba(META, raster, palette), BadExport);
  }
});

test("a symlinked meta or raster is refused", () => {
  const target = directory();
  exportInto(target);
  for (const name of ["meta.json", "raster.bin"]) {
    const dir = directory();
    exportInto(dir);
    renameSync(join(dir, name), join(dir, `${name}.real`));
    symlinkSync(join(target, name), join(dir, name));
    const { renders, poll } = watching(dir);
    poll();
    assert.equal(renders.get("World0"), undefined, name);
    assert.match(renders.problems()[0] ?? "", /is not a regular file/, name);
  }
});

test("a meta or raster larger than its bound is refused", () => {
  const dir = directory();
  put(dir, "meta.json", " ".repeat(MAX_META_BYTES + 1));
  assert.throws(() => readMeta(dir, validate), /larger than 4096 bytes/);
  const huge = metaWith((m) => {
    m.geometry.size = MAX_SIZE_PX + 1;
    m.raster.bytes = (MAX_SIZE_PX + 1) ** 2 * 10;
  });
  exportInto(dir, huge);
  assert.throws(() => readMeta(dir, validate), /larger than 2048 pixels/);
});

test("a directory or a device where a file belongs is refused", () => {
  const dir = directory();
  mkdirSync(join(dir, "meta.json"));
  assert.throws(() => readMeta(dir, validate), /not a regular file/);
});

test("a world that leaves the generation is dropped", () => {
  const dir = directory();
  exportInto(dir);
  const { renders, poll } = watching(dir);
  poll();
  renders.adopt({ generation: { worlds: [] } } as unknown as Documents);
  assert.equal(renders.get("World0"), undefined);
});

test("every palette token reads, and a missing one fails", () => {
  const colours: Palette = palette;
  assert.equal(colours.biomes.size, 8);
  const css = join(directory(), "tokens.css");
  writeFileSync(css, ":root { --map-water: #112233; }");
  assert.throws(() => readPalette(css), /--map-meadows/);
});
