import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { runInNewContext } from "node:vm";
import { test } from "node:test";
import { PAGE } from "./paths.js";

// The page's script is a classic one, with no exports: run it in a context of its own and take its
// two functions. A result crosses realms as JSON, since deepEqual compares prototypes.
interface Plane {
  worldToLatLng(x: number, z: number): [number, number];
  renderBounds(halfWidth: number): [[number, number], [number, number]];
  renderZoom(size: number, halfWidth: number): number;
}
const plane = runInNewContext(`${readFileSync(join(PAGE, "world-coordinates.js"), "utf8")}\n({ worldToLatLng, renderBounds, renderZoom })`) as Plane;
const same = (actual: unknown, expected: unknown): void => assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected);

test("a world position is [z, x] on the plane: north up, east right", () => {
  same(plane.worldToLatLng(-312.5, 1204.75), [1204.75, -312.5]);
  same(plane.worldToLatLng(0, 0), [0, 0]);
});

test("the render's edges, not its pixel centres, lie at +-half_width", () => {
  same(plane.renderBounds(10500), [[-10500, -10500], [10500, 10500]]);
});

// B5's formulas: a position falls in column floor((x + half) / pitch) and row floor((half - z) / pitch),
// on the raster for x in [-half, half) and z in (-half, half].
test("a position lies over the pixel B5 says it falls in, edges included", () => {
  const size = 16;
  const half = 10500;
  const pitch = (2 * half) / size;
  const [[south, west], [north, east]] = plane.renderBounds(half);
  const cell = (x: number, z: number): [number, number] => {
    const [latitude, longitude] = plane.worldToLatLng(x, z);
    return [Math.floor((longitude - west) / pitch), Math.floor((north - latitude) / pitch)];
  };
  for (const [x, z, column, row] of [
    [-half, half, 0, 0],
    [-half, half - 1, 0, 0],
    [half - 1, -half + 1, size - 1, size - 1],
    [0, 0, 8, 8],
    [-312.5, 1204.75, 7, 7],
    [3000, 7000, 10, 2],
  ] as const) {
    assert.deepEqual(cell(x, z), [column, row], `(${x}, ${z})`);
    const [latitude, longitude] = plane.worldToLatLng(x, z);
    assert.ok(latitude > south && latitude <= north && longitude >= west && longitude < east, `(${x}, ${z}) is on the raster`);
  }
  // Off the raster: z = -half is row `size`, x = +half is column `size`.
  assert.deepEqual(cell(0, -half), [8, size]);
  assert.deepEqual(cell(half, 0), [size, 8]);
  const [latitude] = plane.worldToLatLng(0, -half);
  const [, longitude] = plane.worldToLatLng(half, 0);
  assert.ok(!(latitude > south), "z = -half_width is off");
  assert.ok(!(longitude < east), "x = +half_width is off");
});

test("a render pixel is one screen pixel at the zoom its pitch gives", () => {
  assert.equal(plane.renderZoom(1000, 500), 0);
  assert.equal(plane.renderZoom(512, 256), 0);
  assert.equal(plane.renderZoom(16, 10500), Math.log2(16 / 21000));
  assert.equal(plane.renderZoom(2048, 512), 1);
});
