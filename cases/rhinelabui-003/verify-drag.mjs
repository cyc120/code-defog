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
  const before = await stats();
  const x = 1050;
  const y = 324;
  await page.mouse.move(x, y);
  await page.mouse.down();
  const held = await stats();
  assert.equal(held.holdingArchive, true, "Pointer must start an archive drag");
  const projection = held.dragProjection.lane;
  const dragLanes = Number(process.env.DRAG_LANES ?? "0.8");
  await page.mouse.move(x + projection.x * dragLanes, y + projection.y * dragLanes);
  await page.waitForTimeout(160);
  const during = await stats();
  await page.mouse.up();
  const afterUp = await stats();
  await page.waitForFunction(() => !window.rhine.stats().archiveMomentum, null, {
    timeout: 12000,
  });
  await page.waitForTimeout(1800);
  const settled = await stats();
  console.log(JSON.stringify({
    before: { selectedCell: before.selectedCell, columnCamera: before.columnCamera },
    held: { selectedCell: held.selectedCell, columnCamera: held.columnCamera },
    during: {
      selectedCell: during.selectedCell,
      columnCamera: during.columnCamera,
      dragTrack: during.dragTrack,
    },
    projection,
    dragLanes,
    afterUp: {
      selectedCell: afterUp.selectedCell,
      columnCamera: afterUp.columnCamera,
      momentumPhase: afterUp.archiveMomentum?.phase ?? null,
    },
    settled: {
      selectedCell: settled.selectedCell,
      columnCamera: settled.columnCamera,
      momentumPhase: settled.archiveMomentum?.phase ?? null,
    },
    pageErrors,
  }, null, 2));
  assert.deepEqual(pageErrors, []);
  assert.equal(during.selectedCell.lane, before.selectedCell.lane + Math.round(dragLanes),
    "Projected drag distance must select the corresponding physical lane");
  assert.equal(settled.selectedCell.lane, before.selectedCell.lane + Math.round(dragLanes),
    "A slow release must settle on the dragged physical lane");
  assert.ok(Math.abs(settled.columnCamera - settled.selectedCell.lane * 5.2) < 0.1,
    "The settled camera track must align with the selected lane");
} finally {
  await browser.close();
}
