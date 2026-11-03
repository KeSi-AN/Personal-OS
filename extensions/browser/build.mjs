// Bundles the extension into dist/ (load that folder with "Load unpacked").
//   node build.mjs          build dist/
//   node build.mjs --test   also build tests into dist-test/ for `node --test`
import { build } from "esbuild";
import { copyFileSync, mkdirSync, readdirSync, rmSync } from "node:fs";

const common = { bundle: true, target: "chrome116", logLevel: "warning", legalComments: "none" };

rmSync("dist", { recursive: true, force: true });
mkdirSync("dist", { recursive: true });
await build({ ...common, entryPoints: ["src/background.ts"], outfile: "dist/background.js", format: "esm" });
// Content scripts are classic scripts, not modules.
await build({ ...common, entryPoints: ["src/content.ts"], outfile: "dist/content.js", format: "iife" });
copyFileSync("manifest.json", "dist/manifest.json");

if (process.argv.includes("--test")) {
  rmSync("dist-test", { recursive: true, force: true });
  const tests = readdirSync("tests").filter((f) => f.endsWith(".test.ts")).map((f) => `tests/${f}`);
  await build({ ...common, entryPoints: tests, outdir: "dist-test", format: "esm", platform: "node",
                target: "node20", outExtension: { ".js": ".mjs" } });
}
console.log("built dist/");
