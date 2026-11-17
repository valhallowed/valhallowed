// The base image's colours, read from design/tokens.css (the --map-* tokens), the one palette: the
// image is not themed. No colour is written in this code.

import { readFileSync } from "node:fs";

export type Rgb = readonly [number, number, number];

/** The raster's biome values (contracts/plugin.map-export.schema.json, $defs/biome) and the token
 * that colours each. The Ocean biome (256) is water, so it has no token of its own. */
export const BIOME_TOKENS: ReadonlyMap<number, string> = new Map([
  [1, "meadows"],
  [2, "swamp"],
  [4, "mountain"],
  [8, "black-forest"],
  [16, "plains"],
  [32, "ash-lands"],
  [64, "deep-north"],
  [512, "mistlands"],
]);
export const OCEAN = 256;

export interface Palette {
  water: Rgb;
  /** Land colour by biome value. */
  biomes: ReadonlyMap<number, Rgb>;
}

/** The palette tokens.css states. Throws when a token is missing or is not a #rrggbb colour. */
export function readPalette(tokensPath: string): Palette {
  const css = readFileSync(tokensPath, "utf8");
  const token = (name: string): Rgb => {
    const match = new RegExp(`--map-${name}:\\s*#([0-9a-fA-F]{6})\\s*;`).exec(css);
    if (match?.[1] === undefined) {
      throw new Error(`${tokensPath} has no --map-${name} colour`);
    }
    const value = Number.parseInt(match[1], 16);
    return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
  };
  return {
    water: token("water"),
    biomes: new Map([...BIOME_TOKENS].map(([biome, name]) => [biome, token(name)])),
  };
}
