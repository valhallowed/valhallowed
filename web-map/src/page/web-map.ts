// The web map's page: the live players of one world over its world render, polled from /api/players
// and /api/worlds (B11). A classic script, not a module: Leaflet 1.x is the global L, which
// /leaflet/leaflet.js defines, worldToLatLng and renderBounds are world-coordinates.ts's, and the
// page's content security policy allows only scripts the web map serves itself.

// contracts/web-map.players.schema.json
interface LivePlayer {
  name: string | null;
  x: number;
  y: number;
  z: number;
}
interface LiveWorld {
  name: string;
  players: LivePlayer[];
}
interface LivePlayers {
  contract: 1;
  worlds: LiveWorld[];
}

// contracts/web-map.worlds.schema.json
interface WorldRender {
  size: number;
  half_width: number;
  version: string;
}
interface WorldGeometry {
  name: string;
  render: WorldRender | null;
}
interface Worlds {
  contract: 1;
  worlds: WorldGeometry[];
}

// The plugin's sender rounds every 5 s, so a faster poll finds nothing new.
const POLL_MS = 5000;
// Longer than the web map's longest read (web-map/src/server/players.ts), so a slow answer is
// shown, not dropped. The next poll waits for this one, so they never overlap.
const POLL_TIMEOUT_MS = 8000;
// A lone player, or two close together, would otherwise fill the view.
const FIT_MAX_ZOOM = 1;
const FIT_PADDING = 48;
// One unit is a metre from the world's centre; zoom 0 is a metre to a pixel.
const MIN_ZOOM = -6;
const MAX_ZOOM = 4;
// With a render the limits follow its pixel (renderZoom is the zoom that shows one to a screen
// pixel): at most 8 screen pixels to a render pixel, and a fit to players no closer than 2.
const RENDER_MAX_ZOOM_ABOVE_NATIVE = 3;
const RENDER_FIT_ZOOM_ABOVE_NATIVE = 1;

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (found === null) {
    throw new Error(`the page has no #${id}`);
  }
  return found as T;
}

const picker = element<HTMLSelectElement>("world");
const statusLine = element<HTMLElement>("status");
const map = L.map(element("map"), {
  crs: L.CRS.Simple,
  minZoom: MIN_ZOOM,
  maxZoom: MAX_ZOOM,
  attributionControl: false,
});
map.setView([0, 0], 0);
const markers = L.layerGroup().addTo(map);

// Each world's render as /api/worlds last gave it. A world without one, or a page that cannot read
// /api/worlds, draws its players on the plain background.
let geometry = new Map<string, WorldRender>();
// How close a fit to players may zoom: FIT_MAX_ZOOM, or its render's.
let fitMaxZoom = FIT_MAX_ZOOM;
// The image on the map: which world and which version of its render, so it loads again only when
// either changes.
let base: { world: string; version: string; layer: L.ImageOverlay } | undefined;

let worlds: LiveWorld[] = [];
// The names the picker lists. It is rebuilt only when they change: rebuilt on every poll, it
// would close under a viewer who has it open.
let listed = "";
let picked: string | undefined;
// The world the view was last fitted to: it fits again only for another world, or after its
// players were gone.
let fitted: string | undefined;
let timer: number | undefined;
let polling = false;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isLivePlayer(value: unknown): value is LivePlayer {
  return (
    isRecord(value) &&
    (value.name === null || typeof value.name === "string") &&
    [value.x, value.y, value.z].every((part) => typeof part === "number" && Number.isFinite(part))
  );
}

function isLiveWorld(value: unknown): value is LiveWorld {
  return (
    isRecord(value) &&
    typeof value.name === "string" &&
    Array.isArray(value.players) &&
    value.players.length > 0 &&
    value.players.every(isLivePlayer)
  );
}

// The page is B11's receiver: it checks the shape of everything it draws from before it draws.
function isLivePlayers(value: unknown): value is LivePlayers {
  return isRecord(value) && value.contract === 1 && Array.isArray(value.worlds) && value.worlds.every(isLiveWorld);
}

function isWorldGeometry(value: unknown): value is WorldGeometry {
  if (!isRecord(value) || typeof value.name !== "string") {
    return false;
  }
  const render = value.render;
  return (
    render === null ||
    (isRecord(render) &&
      typeof render.size === "number" &&
      Number.isInteger(render.size) &&
      render.size > 0 &&
      typeof render.half_width === "number" &&
      Number.isFinite(render.half_width) &&
      render.half_width > 0 &&
      typeof render.version === "string" &&
      /^[0-9a-f]{16}$/.test(render.version))
  );
}

function isWorlds(value: unknown): value is Worlds {
  return isRecord(value) && value.contract === 1 && Array.isArray(value.worlds) && value.worlds.every(isWorldGeometry);
}

// A player's name is whatever they chose: it goes in as text, never as markup.
function label(name: string): HTMLElement {
  const text = document.createElement("span");
  text.textContent = name;
  return text;
}

function say(line: string): void {
  statusLine.textContent = line;
}

// The picked world's render under its players, or nothing when it has none.
function drawBase(world: string | undefined): void {
  const drawn = world === undefined ? undefined : geometry.get(world);
  if (world === undefined || drawn === undefined) {
    base?.layer.remove();
    base = undefined;
    fitMaxZoom = FIT_MAX_ZOOM;
    map.setMaxZoom(MAX_ZOOM);
    return;
  }
  if (base?.world === world && base.version === drawn.version) {
    return;
  }
  base?.layer.remove();
  const native = renderZoom(drawn.size, drawn.half_width);
  map.setMaxZoom(Math.max(MIN_ZOOM, native + RENDER_MAX_ZOOM_ABOVE_NATIVE));
  fitMaxZoom = native + RENDER_FIT_ZOOM_ABOVE_NATIVE;
  // The query is ignored by the server; it only makes a new version a new URL for the browser.
  const url = `/api/worlds/${encodeURIComponent(world)}/render.png?v=${drawn.version}`;
  // The overlay's corners are the image's edges, at +-half_width, which is where B5 puts them.
  const layer = L.imageOverlay(url, renderBounds(drawn.half_width), { className: "render" });
  layer.addTo(map);
  base = { world, version: drawn.version, layer };
}

function render(): void {
  markers.clearLayers();
  const world = worlds.find((entry) => entry.name === picked);
  // A world's name holds no line break (the contract's pattern), so the joined names tell lists apart.
  const names = worlds.map((entry) => entry.name).join("\n");
  if (names !== listed) {
    picker.replaceChildren(
      ...worlds.map((entry) => {
        const option = document.createElement("option");
        option.value = entry.name;
        option.textContent = entry.name;
        return option;
      }),
    );
    listed = names;
  }
  picker.hidden = worlds.length === 0;
  const index = world === undefined ? -1 : worlds.indexOf(world);
  if (picker.selectedIndex !== index) {
    picker.selectedIndex = index;
  }
  if (world === undefined) {
    fitted = undefined;
    drawBase(undefined);
    say(picked === undefined ? "no live players" : `no live players in ${picked}`);
    return;
  }
  say("");
  drawBase(world.name);
  const points: L.LatLng[] = [];
  for (const player of world.players) {
    // The plane's vertical is the world's z (north), its horizontal x; y is height.
    const at = L.latLng(worldToLatLng(player.x, player.z));
    points.push(at);
    const marker = L.circleMarker(at, { radius: 6, weight: 2, fillOpacity: 0.85, className: "player", interactive: false });
    if (player.name !== null) {
      marker.bindTooltip(label(player.name), { permanent: true, direction: "right", offset: [8, 0] });
    }
    marker.addTo(markers);
  }
  if (fitted !== world.name) {
    map.fitBounds(L.latLngBounds(points), { maxZoom: fitMaxZoom, padding: [FIT_PADDING, FIT_PADDING] });
    fitted = world.name;
  }
}

// What was last read is no longer known to be live, so none of it stays to be picked or drawn.
function unavailable(): void {
  worlds = [];
  render();
  say("the live players are unavailable");
}

// Undefined when /api/worlds cannot be read now: the geometry last read stays.
async function readWorlds(): Promise<Map<string, WorldRender> | undefined> {
  try {
    const response = await fetch("/api/worlds", { cache: "no-store", signal: AbortSignal.timeout(POLL_TIMEOUT_MS) });
    const answer: unknown = response.ok ? await response.json() : undefined;
    if (!isWorlds(answer)) {
      return undefined;
    }
    return new Map(answer.worlds.flatMap((entry) => (entry.render === null ? [] : [[entry.name, entry.render] as const])));
  } catch {
    return undefined;
  }
}

async function poll(): Promise<void> {
  polling = true;
  try {
    // The worlds are the base image only: while they are unreadable the players still show, over
    // the geometry last read.
    const known = readWorlds();
    const response = await fetch("/api/players", { cache: "no-store", signal: AbortSignal.timeout(POLL_TIMEOUT_MS) });
    if (!response.ok) {
      throw new Error(`it answered ${response.status}`);
    }
    const answer: unknown = await response.json();
    if (!isLivePlayers(answer)) {
      throw new Error("it is not the live players");
    }
    geometry = (await known) ?? geometry;
    worlds = answer.worlds;
    picked ??= worlds[0]?.name;
    render();
  } catch {
    unavailable();
  } finally {
    polling = false;
    schedule();
  }
}

// A hidden page does not poll.
function schedule(): void {
  window.clearTimeout(timer);
  timer = document.hidden ? undefined : window.setTimeout(() => void poll(), POLL_MS);
}

picker.addEventListener("change", () => {
  picked = picker.value;
  render();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    window.clearTimeout(timer);
  } else if (!polling) {
    window.clearTimeout(timer);
    void poll();
  }
});
void poll();
