// Where the world is on the page's plane, as pure functions with no Leaflet in them, so a test can
// run the compiled file alone. A classic script, like web-map.ts, which loads after it.
//
// The plane is CRS.Simple in world metres: latitude is the world's z (north), longitude its x
// (east), so north is up and east is right. contracts/plugin.map-export.schema.json (B5) frames the
// world render: it covers x from -half_width up to but not including +half_width and z from
// +half_width down to but not including -half_width, row 0 is north and column 0 west, so the
// image's edges, not its pixel centres, sit at +-half_width.

type PlanePoint = [latitude: number, longitude: number];

/** A position in the world, as a point on the plane: [z, x]. Height has no place on it. */
function worldToLatLng(x: number, z: number): PlanePoint {
  return [z, x];
}

/** Where a world render of half-width `halfWidth` lies on the plane: its south-west and north-east
 * corners. */
function renderBounds(halfWidth: number): [PlanePoint, PlanePoint] {
  return [worldToLatLng(-halfWidth, -halfWidth), worldToLatLng(halfWidth, halfWidth)];
}

/** The zoom at which one render pixel is one screen pixel: a plane at zoom z shows 2^z screen
 * pixels to the metre, and a render pixel is 2 * half_width / size metres. */
function renderZoom(size: number, halfWidth: number): number {
  return Math.log2(size / (2 * halfWidth));
}
