import { fileURLToPath } from "node:url";

// The repo's directories the image copies under /opt/vhd. Compiled tests run from dist/test, two
// levels below web-map/, which sits beside contracts/ and design/.
export const CONTRACTS = fileURLToPath(new URL("../../../contracts/", import.meta.url));
export const DESIGN = fileURLToPath(new URL("../../../design/", import.meta.url));
export const PAGE = fileURLToPath(new URL("../page/", import.meta.url));
export const LEAFLET = fileURLToPath(new URL("../../node_modules/leaflet/dist/", import.meta.url));
export const PUBLIC = fileURLToPath(new URL("../../public/", import.meta.url));
// The synthesized map export of contracts/examples/valid, and the image the renderer's output is
// compared with. Neither reaches the final image: its build stage copies the first for the tests,
// and the final stage copies only the compiled service and page.
export const EXAMPLES = fileURLToPath(new URL("../../../contracts/examples/valid/", import.meta.url));
export const GOLDEN = fileURLToPath(new URL("../../src/test/golden/", import.meta.url));
