import { fileURLToPath } from "node:url";

// The repo's directories the image copies under /opt/vhd. Compiled tests run from dist/test, two
// levels below web-map/, which sits beside contracts/ and design/.
export const CONTRACTS = fileURLToPath(new URL("../../../contracts/", import.meta.url));
export const DESIGN = fileURLToPath(new URL("../../../design/", import.meta.url));
export const PUBLIC = fileURLToPath(new URL("../../public/", import.meta.url));
