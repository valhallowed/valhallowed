#!/usr/bin/env bash
# Acceptance check: the web map serves the base image of the acceptance world's
# map export, and the image holds what is true of any seed, because the game
# picks the acceptance world's seed. GET /api/worlds passes
# contracts/web-map.worlds.schema.json and gives the world a render whose size
# and half_width are the export's geometry. GET /api/worlds/<name>/render.png
# is a PNG of size by size pixels, with the render's version as its ETag and
# the first 16 hex digits of its SHA-256, answered with Cache-Control no-cache,
# and a request carrying that ETag is answered 304. The image's water share, the
# pixels of exactly the palette's water colour, lies in WATER_MIN..WATER_MAX, and
# the export's raster holds more than one biome above the water level. The exact
# image is checked by the web map's own test against a synthesized export. See
# accept.sh for the ACCEPT_ variables.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
base="http://127.0.0.1:$ACCEPT_WEB_MAP_PORT"
export_dir=/data/map-export

# The export covers a square of side 2 * half_width. Past the water edge,
# half_width, the terrain lies below the water level whatever its biome, so the
# base image draws water over at least the square outside the inscribed disc:
# 1 - pi/4, about 0.215. Land already thins out from 10000 m, so a real seed
# sits well above that; WATER_MIN keeps a margin under it for pixel sampling at
# the disc's edge. The world has land, so the share is below 1; WATER_MAX only
# refuses an image that is nearly all water, with room for a seed that is
# mostly ocean.
WATER_MIN=0.20
WATER_MAX=0.95

# The plugin exports 60 s after the world loads, which is about when the plugin
# reports it, then samples 1000 rows with a 10 ms pause after each (10 s and
# the sampling itself), and the web map polls the export every 5 s. 360 s is
# several times that. accept.sh's callers leave room for its 20-minute wait;
# the tightest is ci.yml's accept job at 40 minutes (release.yml's promote has
# 45, ci.yml's build and nightly's game 90). This wait, accept-97's restore (4)
# and accept-99's import (7) bring the waits to 20 + 6 + 4 + 7 = 37 minutes,
# which fits all of them with the other checks' few minutes to spare.
wait_s=360

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

diagnose() {
  echo "accept: $1" >&2
  echo "accept: GET /api/worlds answered '${answer:-nothing}'" >&2
  echo "$ACCEPT_STATUS" >&2
  docker exec "$ACCEPT_WORLD" ls -l "$export_dir" >&2 || true
  docker logs "$ACCEPT_WORLD" 2>&1 | grep -F ':Valhallowed]' | grep -i 'map export' | tail -n 10 >&2 || true
  docker logs --tail 40 "$ACCEPT_WEB_MAP" >&2 || true
  exit 1
}

render='' answer=''
start=$SECONDS
while ((SECONDS - start < wait_s)); do
  if answer=$(curl -fsS --max-time 5 "$base/api/worlds") &&
    render=$(jq -ce --arg world "$ACCEPT_WORLD_NAME" \
      '.worlds[] | select(.name == $world) | .render | select(. != null)' <<< "$answer" 2>/dev/null); then
    break
  fi
  render=''
  sleep 5
done
[[ -n $render ]] || diagnose "the web map gave $ACCEPT_WORLD_NAME no render in ${wait_s}s"
echo "accept: the web map had a render $((SECONDS - start))s after this check started"

# The contract is checked as scripts/check-schema.sh checks its examples.
if ! out=$(docker run --rm -i -v "$root:/repo:ro" -w /repo "$("$root/scripts/tool-image.sh" python)" sh -c '
  pip install --quiet --disable-pip-version-check --root-user-action=ignore --no-deps -r scripts/requirements-check-schema.txt &&
  check-jsonschema --schemafile contracts/web-map.worlds.schema.json -' <<< "$answer" 2>&1); then
  echo "accept: the web map's answer to GET /api/worlds breaks its contract:" >&2
  echo "$out" >&2
  exit 1
fi
size=$(jq -r .size <<< "$render") version=$(jq -r .version <<< "$render")

# The export, as the world's container holds it: its geometry, and the biomes the base image draws
# as land, which are those above the water level: past the water edge the game's biome can be
# Ashlands while the terrain is under water, and the image draws that as water.
if ! exported=$(docker exec -i "$ACCEPT_WORLD" python3 - "$export_dir" <<'PY'
import array, json, sys
directory = sys.argv[1]
with open(directory + "/meta.json") as handle:
    meta = json.load(handle)
size = meta["geometry"]["size"]
with open(directory + "/raster.bin", "rb") as handle:
    raster = handle.read()
count = size * size
biomes = array.array("H")
biomes.frombytes(raster[: count * 2])
heights = array.array("f")
heights.frombytes(raster[count * 2 : count * 6])
if sys.byteorder != "little":
    biomes.byteswap()
    heights.byteswap()
above = {b for b, h in zip(biomes, heights) if b != 256 and h > meta["water_level"]}
print(json.dumps({"size": size, "half_width": meta["geometry"]["half_width"], "bytes": len(raster),
                  "biomes_above_water": sorted(above)}))
PY
); then
  diagnose "the world's map export could not be read from $export_dir"
fi
if ! jq -e --argjson render "$render" \
  '.size == $render.size and .half_width == $render.half_width and .bytes == .size * .size * 10' <<< "$exported" >/dev/null; then
  diagnose "the export $exported is not the render's geometry $render"
fi
if ! jq -e '.biomes_above_water | length > 1' <<< "$exported" >/dev/null; then
  diagnose "the export's raster holds fewer than two biomes above the water level: $(jq -c .biomes_above_water <<< "$exported")"
fi

# The image, its ETag and its conditional answer.
url="$base/api/worlds/$(jq -rn --arg world "$ACCEPT_WORLD_NAME" '$world | @uri')/render.png"
curl -fsS --max-time 10 -D "$tmp/headers" -o "$tmp/render.png" "$url" || diagnose "GET $url failed"
# The Node container's user is not the runner's, and a umask or rootless Docker may hide the file.
chmod 644 "$tmp/render.png"
etag=$(tr -d '\r' < "$tmp/headers" | sed -n 's/^[Ee][Tt]ag: *//p')
control=$(tr -d '\r' < "$tmp/headers" | sed -n 's/^[Cc]ache-[Cc]ontrol: *//p')
[[ $etag == "\"$version\"" ]] || diagnose "the base image's ETag is '$etag', not \"$version\""
digest=$(sha256sum "$tmp/render.png")
[[ ${digest:0:16} == "$version" ]] || diagnose "the render's version $version is not the first 16 hex digits of the base image's SHA-256, ${digest:0:16}"
[[ $control == no-cache ]] || diagnose "the base image's Cache-Control is '$control', not no-cache"
code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' -H "If-None-Match: $etag" "$url") || code=''
[[ $code == 304 ]] || diagnose "a request carrying the base image's ETag was answered '$code', not 304"

# The PNG is read on the web map image's Node, which the page check uses too, with the palette read
# by the server's own code. Every filter type is undone, so the check does not depend on the
# encoder's choice. The container needs no network.
node_image=$(docker inspect --format '{{.Config.Image}}' "$ACCEPT_WEB_MAP")
if ! read_out=$(docker run --rm -i --network none -v "$tmp/render.png:/render.png:ro" -e PNG=/render.png \
  --entrypoint node "$node_image" --input-type=module - 2>"$tmp/node.err" <<'JS'
import { readFileSync } from "node:fs";
import { inflateSync } from "node:zlib";
import { readPalette } from "/opt/vhd/web-map/server/palette.js";

const png = readFileSync(process.env.PNG);
if (png.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a") throw new Error("not a PNG");
let width = 0, height = 0, depth = 0, colour = 0, interlace = 0;
const data = [];
for (let at = 8; at < png.length;) {
  const length = png.readUInt32BE(at);
  const type = png.toString("latin1", at + 4, at + 8);
  const body = png.subarray(at + 8, at + 8 + length);
  if (type === "IHDR") {
    width = body.readUInt32BE(0); height = body.readUInt32BE(4);
    [depth, colour, , , interlace] = body.subarray(8, 13);
  } else if (type === "IDAT") {
    data.push(body);
  }
  at += 12 + length;
}
if (depth !== 8 || ![2, 6].includes(colour) || interlace !== 0) {
  throw new Error(`unsupported PNG: depth ${depth}, colour type ${colour}, interlace ${interlace}`);
}
const bpp = colour === 6 ? 4 : 3;
const stride = width * bpp;
const raw = inflateSync(Buffer.concat(data));
if (raw.length !== height * (stride + 1)) throw new Error("the PNG's data is not its size");
const pixels = Buffer.alloc(height * stride);
for (let row = 0; row < height; row++) {
  const filter = raw[row * (stride + 1)];
  for (let i = 0; i < stride; i++) {
    const x = raw[row * (stride + 1) + 1 + i];
    const left = i >= bpp ? pixels[row * stride + i - bpp] : 0;
    const up = row > 0 ? pixels[(row - 1) * stride + i] : 0;
    const upLeft = row > 0 && i >= bpp ? pixels[(row - 1) * stride + i - bpp] : 0;
    let predicted;
    switch (filter) {
      case 0: predicted = 0; break;
      case 1: predicted = left; break;
      case 2: predicted = up; break;
      case 3: predicted = (left + up) >> 1; break;
      case 4: {
        const p = left + up - upLeft;
        const [pa, pb, pc] = [Math.abs(p - left), Math.abs(p - up), Math.abs(p - upLeft)];
        predicted = pa <= pb && pa <= pc ? left : pb <= pc ? up : upLeft;
        break;
      }
      default: throw new Error(`PNG filter ${filter}`);
    }
    pixels[row * stride + i] = (x + predicted) & 255;
  }
}
const [r, g, b] = readPalette("/opt/vhd/design/tokens.css").water;
let water = 0;
for (let i = 0; i < pixels.length; i += bpp) {
  if (pixels[i] === r && pixels[i + 1] === g && pixels[i + 2] === b) water++;
}
console.log(JSON.stringify({ width, height, water_share: water / (width * height) }));
JS
); then
  cat "$tmp/node.err" >&2
  diagnose "the base image could not be read: $read_out"
fi
if ! jq -e --argjson size "$size" --argjson min "$WATER_MIN" --argjson max "$WATER_MAX" \
  '.width == $size and .height == $size and .water_share >= $min and .water_share <= $max' <<< "$read_out" >/dev/null; then
  diagnose "the base image is $read_out, not ${size} by ${size} pixels with a water share from $WATER_MIN to $WATER_MAX"
fi
echo "accept: the web map served a ${size} by ${size} base image, version $version, $(jq -r '.water_share * 1000 | round / 10' <<< "$read_out")% water, and $(jq '.biomes_above_water | length' <<< "$exported") biomes above the water level in its export"
