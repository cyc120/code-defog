import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const modulePath = process.env.PLAYWRIGHT_MODULE;
if (!modulePath) throw new Error("Set PLAYWRIGHT_MODULE to the installed Playwright directory");
const { chromium } = await import(pathToFileURL(resolve(modulePath, "index.mjs")).href);
const browser = await chromium.launch({ channel: "msedge", headless: true });

try {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto(`${process.env.REVIEW_URL ?? "http://127.0.0.1:5204"}/?scene=archive`);
  await page.waitForFunction(
    () => window.rhine?.stats().ready && window.rhine.stats().extraction >= 0.399,
    null,
    { timeout: 60000 },
  );
  await page.waitForTimeout(2200);
  const stats = () => page.evaluate(() => window.rhine.stats());
  const x = 1050;
  const y = 324;
  await page.mouse.move(x, y);
  await page.mouse.down();
  const held = await stats();
  assert.equal(held.holdingArchive, true);
  const projection = held.dragProjection.lane;
  for (let i = 1; i <= 4; i++) {
    await page.waitForTimeout(8);
    await page.mouse.move(x + projection.x * 0.8 * i / 4, y + projection.y * 0.8 * i / 4);
  }
  await page.mouse.up();
  const released = await stats();
  await page.waitForTimeout(250);
  const coasting = await stats();
  await page.waitForFunction(() => !window.rhine.stats().archiveMomentum, null, {
    timeout: 12000,
  });
  await page.waitForTimeout(1800);
  const settled = await stats();
  console.log(JSON.stringify({
    released: {
      selectedCell: released.selectedCell,
      columnCamera: released.columnCamera,
      momentum: released.archiveMomentum,
    },
    coasting: {
      selectedCell: coasting.selectedCell,
      columnCamera: coasting.columnCamera,
      momentum: coasting.archiveMomentum,
    },
    settled: {
      selectedCell: settled.selectedCell,
      columnCamera: settled.columnCamera,
      momentum: settled.archiveMomentum,
    },
    pageErrors,
  }, null, 2));
  assert.deepEqual(pageErrors, []);
  assert.equal(released.archiveMomentum?.phase, "coasting");
  assert.ok(Math.abs(released.archiveMomentum.value.lane - released.columnCamera / 5.2) < 0.05);
  assert.ok(coasting.columnCamera > released.columnCamera + 0.5);
  assert.ok(Math.abs(settled.columnCamera - settled.selectedCell.lane * 5.2) < 0.1);
} finally {
  await browser.close();
}
