// The web map's page: the live players of one world on an empty plane, polled from /api/players
// (B11). A classic script, not a module: Leaflet 1.x is the global L, which /leaflet/leaflet.js
// defines, and the page's content security policy allows only scripts the web map serves itself.

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

// The plugin's sender rounds every 5 s, so a faster poll finds nothing new.
const POLL_MS = 5000;
const POLL_TIMEOUT_MS = 4000;
// A lone player, or two close together, would otherwise fill the view.
const FIT_MAX_ZOOM = 1;
const FIT_PADDING = 48;
// One unit is a metre from the world's centre; zoom 0 is a metre to a pixel.
const MIN_ZOOM = -6;
const MAX_ZOOM = 4;

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

let worlds: LiveWorld[] = [];
let picked: string | undefined;
// The world the view was last fitted to: it fits again only for another world, or after its
// players were gone.
let fitted: string | undefined;
let timer: number | undefined;
let polling = false;

function isLivePlayers(value: unknown): value is LivePlayers {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { contract?: unknown }).contract === 1 &&
    Array.isArray((value as { worlds?: unknown }).worlds)
  );
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

function render(): void {
  markers.clearLayers();
  const world = worlds.find((entry) => entry.name === picked);
  picker.replaceChildren(
    ...worlds.map((entry) => {
      const option = document.createElement("option");
      option.value = entry.name;
      option.textContent = entry.name;
      return option;
    }),
  );
  picker.hidden = worlds.length === 0;
  picker.selectedIndex = world === undefined ? -1 : worlds.indexOf(world);
  if (world === undefined) {
    fitted = undefined;
    say(picked === undefined ? "" : `no live players in ${picked}`);
    return;
  }
  say("");
  const points: L.LatLng[] = [];
  for (const player of world.players) {
    // The plane's vertical is the world's z (north), its horizontal x; y is height.
    const at = L.latLng(player.z, player.x);
    points.push(at);
    const marker = L.circleMarker(at, { radius: 6, weight: 2, fillOpacity: 0.85, className: "player", interactive: false });
    if (player.name !== null) {
      marker.bindTooltip(label(player.name), { permanent: true, direction: "right", offset: [8, 0], className: "player-name" });
    }
    marker.addTo(markers);
  }
  if (fitted !== world.name) {
    map.fitBounds(L.latLngBounds(points), { maxZoom: FIT_MAX_ZOOM, padding: [FIT_PADDING, FIT_PADDING] });
    fitted = world.name;
  }
}

function unavailable(): void {
  markers.clearLayers();
  say("the live players are unavailable");
}

async function poll(): Promise<void> {
  polling = true;
  try {
    const response = await fetch("/api/players", { cache: "no-store", signal: AbortSignal.timeout(POLL_TIMEOUT_MS) });
    if (!response.ok) {
      throw new Error(`it answered ${response.status}`);
    }
    const answer: unknown = await response.json();
    if (!isLivePlayers(answer)) {
      throw new Error("it is not the live players");
    }
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
