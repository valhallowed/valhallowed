#!/usr/bin/env bash
# Acceptance check: the web map's page, loaded in a headless browser through
# the manifest's port, shows a synthetic player sent through intake as its only
# marker, named in its tooltip, and nothing on the page fails: no console
# error, no uncaught exception, no failed request and no content security
# policy violation, which the browser logs as an error. When GET /api/worlds
# lists a world render for the world, the page also draws it under the player:
# the render's image is loaded at the render's size, and the marker lies inside
# the image's bounds. The browser is pinned
# in images/tools/Dockerfile; the script that drives it over the DevTools
# protocol runs on the web map image's own Node. See accept.sh for the ACCEPT_
# variables.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)

# The browser reaches the published port through the host, as a viewer would.
# Labelled with the install's project, so accept.sh's cleanup removes it too.
shell=$(docker run -d --rm --label "com.docker.compose.project=$ACCEPT_PROJECT" \
  --add-host install-host:host-gateway \
  "$("$root/scripts/tool-image.sh" headless-shell)")
trap 'docker rm --force "$shell" >/dev/null 2>&1 || true' EXIT
node_image=$(docker inspect --format '{{.Config.Image}}' "$ACCEPT_WEB_MAP")

# Sent once the browser's image is pulled, so the player is still live, seen
# within the web map's 90 s, when the page loads.
"$root/scripts/accept-player.sh" Steam_accept_page Pagewalker -12.0 28.5 9.0 >/dev/null

if ! report=$(docker run --rm -i --network "container:$shell" --entrypoint node \
  -e PAGE="http://install-host:$ACCEPT_WEB_MAP_PORT/" -e PLAYER=Pagewalker -e WORLD="$ACCEPT_WORLD_NAME" \
  "$node_image" --input-type=module - <<'JS'
const { PAGE: page, PLAYER: player, WORLD: world } = process.env;
const deadline = Date.now() + 30_000;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const problems = [];
let seen;
// A browser that stops answering would leave a reply pending forever: the whole run is bounded.
setTimeout(() => {
  console.log(JSON.stringify({ seen, problems: [...problems, "the browser stopped answering"] }));
  process.exit(1);
}, 60_000).unref();

// The render the world has, if any: the page must draw it when it does. accept-85 holds the
// answer to its contract; here an answer that cannot be read is a problem.
let expected = null;
try {
  const worlds = await (await fetch(new URL("/api/worlds", page))).json();
  expected = worlds.worlds.find((entry) => entry.name === world)?.render ?? null;
} catch (error) {
  problems.push(`GET /api/worlds: ${error.message}`);
}

// The shell takes a moment to listen; a new target is a blank page of its own.
let target;
while (target === undefined) {
  try {
    target = await (await fetch("http://127.0.0.1:9222/json/new?about:blank", { method: "PUT" })).json();
  } catch (error) {
    if (Date.now() > deadline) throw new Error(`the browser never answered: ${error.message}`);
    await sleep(500);
  }
}
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = () => reject(new Error("no DevTools socket")); });
let id = 0;
const replies = new Map();
const send = (method, params = {}) => new Promise((resolve) => {
  replies.set(++id, resolve);
  socket.send(JSON.stringify({ id, method, params }));
});
socket.onmessage = ({ data }) => {
  const { id: replyTo, method, params, result } = JSON.parse(data);
  if (replyTo !== undefined) {
    replies.get(replyTo)?.(result);
    replies.delete(replyTo);
  } else if (method === "Runtime.exceptionThrown") {
    const { exception, text } = params.exceptionDetails;
    problems.push(`uncaught: ${exception?.description ?? text}`);
  } else if (method === "Runtime.consoleAPICalled" && ["error", "assert"].includes(params.type)) {
    problems.push(`console ${params.type}: ${params.args.map((arg) => arg.value ?? arg.description).join(" ")}`);
  } else if (method === "Log.entryAdded" && params.entry.level === "error") {
    problems.push(`${params.entry.source}: ${params.entry.text}${params.entry.url ? ` (${params.entry.url})` : ""}`);
  }
};
await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Page.navigate", { url: page });

const probe = `JSON.stringify({
  markers: document.querySelectorAll("path.player").length,
  names: [...document.querySelectorAll(".leaflet-tooltip")].map((tooltip) => tooltip.textContent),
  status: document.getElementById("status")?.textContent,
  render: (() => {
    const image = document.querySelector("img.leaflet-image-layer.render");
    const marker = document.querySelector("path.player");
    if (!image) return null;
    const box = image.getBoundingClientRect();
    const dot = marker?.getBoundingClientRect();
    const middle = dot && { x: (dot.left + dot.right) / 2, y: (dot.top + dot.bottom) / 2 };
    return {
      loaded: image.complete && image.naturalWidth > 0,
      width: image.naturalWidth,
      height: image.naturalHeight,
      inside: !!middle && middle.x >= box.left && middle.x <= box.right && middle.y >= box.top && middle.y <= box.bottom,
    };
  })(),
})`;
const look = async () => {
  seen = JSON.parse((await send("Runtime.evaluate", { expression: probe, returnByValue: true }))?.result?.value ?? "null");
  if (!(seen?.markers === 1 && seen.names.length === 1 && seen.names[0] === player)) return false;
  // Without a render in the answer the page draws none, and nothing here asks for one.
  return expected === null || (seen.render?.loaded === true && seen.render.width === expected.size &&
    seen.render.height === expected.size && seen.render.inside);
};
let shows = false;
while (!shows && Date.now() < deadline) {
  shows = await look();
  if (!shows) await sleep(500);
}
// The page polls every 5 s: after one more poll the marker must still show, with nothing failed.
if (shows) {
  await sleep(6000);
  shows = await look();
}
socket.close();
console.log(JSON.stringify({ seen, expected, problems }));
process.exit(shows && problems.length === 0 ? 0 : 1);
JS
); then
  echo "accept: the web map's page did not show Pagewalker over the world render cleanly: ${report:-the browser gave no report}" >&2
  exit 1
fi
echo "accept: the web map's page showed a synthetic player's marker with no error or policy violation"
