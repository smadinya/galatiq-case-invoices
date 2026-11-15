// Screenshots of the running dashboard (a dev tool; see scripts/ui_screenshots.py, which starts the server).
//
//   node scripts/ui_screenshots.mjs --base http://127.0.0.1:8599 --out DIR [--modes light,dark]
//        [--widths 1440,390] --shot name=/path [--shot ...]
//
// Environment: PW_NODE_MODULES (a node_modules folder that has playwright) and PW_CHROME (a chromium
// headless shell). Without them the newest ~/.npm/_npx/*/node_modules with playwright and the newest
// ~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell are used.
// Exit status 1 when any page shows a Streamlit exception.
import fs from "fs";
import os from "os";
import path from "path";
import { createRequire } from "module";

function newest(dirs) {
  return dirs.sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0];
}

function findModules() {
  if (process.env.PW_NODE_MODULES) return process.env.PW_NODE_MODULES;
  const root = path.join(os.homedir(), ".npm", "_npx");
  const found = fs.existsSync(root)
    ? fs.readdirSync(root).map((d) => path.join(root, d, "node_modules")).filter((d) => fs.existsSync(path.join(d, "playwright")))
    : [];
  if (!found.length) throw new Error("Playwright not found. Set PW_NODE_MODULES, or run: npx --yes playwright --version");
  return newest(found);
}

function findChrome() {
  if (process.env.PW_CHROME) return process.env.PW_CHROME;
  const root = path.join(os.homedir(), ".cache", "ms-playwright");
  const found = fs.existsSync(root)
    ? fs.readdirSync(root).filter((d) => d.startsWith("chromium_headless_shell-"))
        .map((d) => path.join(root, d, "chrome-linux", "headless_shell")).filter((p) => fs.existsSync(p))
    : [];
  if (!found.length) throw new Error("No chromium headless shell found. Set PW_CHROME.");
  return found.sort((a, b) => (parseInt(b.match(/shell-(\d+)/)[1]) - parseInt(a.match(/shell-(\d+)/)[1])))[0];
}

function parseArgs(argv) {
  const args = { modes: "light,dark", widths: "1440,390", shots: [] };
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (key === "--shot") args.shots.push(argv[++i]);
    else if (key.startsWith("--")) args[key.slice(2)] = argv[++i];
  }
  if (!args.base || !args.out || !args.shots.length) {
    throw new Error("usage: --base URL --out DIR --shot name=/path [--shot ...] [--modes light,dark] [--widths 1440,390]");
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
const require = createRequire(findModules() + "/");
const { chromium } = require("playwright");
fs.mkdirSync(args.out, { recursive: true });

const browser = await chromium.launch({ executablePath: findChrome(), headless: true });
let failed = false;
try {
  for (const mode of args.modes.split(",")) {
    const context = await browser.newContext({ colorScheme: mode, viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    for (const width of args.widths.split(",").map(Number)) {
      await page.setViewportSize({ width, height: 900 });
      for (const shot of args.shots) {
        const at = shot.indexOf("=");
        const name = shot.slice(0, at), urlPath = shot.slice(at + 1);
        await page.goto(args.base + urlPath, { waitUntil: "networkidle" });
        await page.waitForSelector('[data-testid="stApp"]', { timeout: 30000 });
        await page.waitForFunction(() => !document.querySelector('[data-testid="stStatusWidget"]'), null, { timeout: 30000 }).catch(() => {});
        await page.waitForTimeout(800);
        const file = path.resolve(args.out, `${name}-${mode}-${width}.png`);
        await page.screenshot({ path: file, fullPage: true });
        const exceptions = await page.locator('[data-testid="stException"]').count();
        if (exceptions) failed = true;
        console.log(`${exceptions ? "EXCEPTION " : ""}${file}`);
      }
    }
    await context.close();
  }
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
