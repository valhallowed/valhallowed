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
}
const plane = runInNewContext(`${readFileSync(join(PAGE, "world-coordinates.js"), "utf8")}\n({ worldToLatLng, renderBounds })`) as Plane;
const same = (actual: unknown, expected: unknown): void => assert.deepEqual(JSON.parse(JSON.stringify(actual)), expected);

test("a world position is [z, x] on the plane: north up, east right", () => {
  same(plane.worldToLatLng(-312.5, 1204.75), [1204.75, -312.5]);
  same(plane.worldToLatLng(0, 0), [0, 0]);
});

test("the render's edges, not its pixel centres, lie at +-half_width", () => {
  same(plane.renderBounds(10500), [[-10500, -10500], [10500, 10500]]);
});

test("a position lies over the pixel B5 says it falls in", () => {
  // B5's synthetic export: 16 pixels across 21000 metres; the image's north-west corner is the
  // plane's (half_width, -half_width), row 0 north and column 0 west.
  const size = 16;
  const half = 10500;
  const pitch = (2 * half) / size;
  const [[south, west], [north, east]] = plane.renderBounds(half);
  for (const [x, z, column, row] of [
    [-half, half - 1, 0, 0],
    [half - 1, -half + 1, size - 1, size - 1],
    [0, 0, 8, 8],
    [-312.5, 1204.75, 7, 7],
    [3000, 7000, 10, 2],
  ] as const) {
    const [latitude, longitude] = plane.worldToLatLng(x, z);
    const fromTop = Math.floor((north - latitude) / pitch);
    const fromLeft = Math.floor((longitude - west) / pitch);
    assert.deepEqual([fromLeft, fromTop], [column, row], `(${x}, ${z})`);
    assert.ok(latitude >= south && latitude < north && longitude >= west && longitude < east);
  }
});
