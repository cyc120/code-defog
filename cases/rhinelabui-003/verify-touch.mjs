import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const modulePath = process.env.PLAYWRIGHT_MODULE;
if (!modulePath) throw new Error("Set PLAYWRIGHT_MODULE to the installed Playwright directory");
const { chromium } = await import(pathToFileURL(resolve(modulePath, "index.mjs")).href);
const browser = await chromium.launch({ channel: "msedge", headless: true });
const results = [];

try {
  for (const [width, height] of [[390, 844], [844, 390]]) {
    for (const mode of ["small", "lane", "fling"]) {
      const context = await browser.newContext({
        viewport: { width, height },
        isMobile: true,
        hasTouch: true,
      });
      try {
        const page = await context.newPage();
        const pageErrors = [];
        page.on("pageerror", (error) => pageErrors.push(error.message));
        const cdp = await context.newCDPSession(page);
        await page.goto(`${process.env.REVIEW_URL ?? "http://127.0.0.1:5204"}/?scene=archive`);
        await page.waitForFunction(
          () => window.rhine?.stats().ready && window.rhine.stats().extraction >= 0.399,
          null,
          { timeout: 60000 },
        );
        await page.waitForTimeout(2200);
        const stats = () => page.evaluate(() => window.rhine.stats());
        const landscape = width > height;
        const x = width * (landscape ? 0.4 : 0.65);
        const y = height * (landscape ? 0.52 : 0.3);
        const touch = async (type, px, py) => cdp.send("Input.dispatchTouchEvent", {
          type,
          touchPoints: type === "touchEnd" ? [] : [{ x: px, y: py, id: 1 }],
        });
        const before = await stats();
        await touch("touchStart", x, y);
        const held = await stats();
        assert.equal(held.holdingArchive, true, `${width}x${height}: touch must start browsing`);
        const projection = held.dragProjection.lane;
        const amount = mode === "small" ? 0.2 : 0.8;
        if (mode === "fling") {
          for (let i = 1; i <= 4; i++) {
            await page.waitForTimeout(8);
            await touch("touchMove", x + projection.x * amount * i / 4,
              y + projection.y * amount * i / 4);
          }
        } else {
          await touch("touchMove", x + projection.x * amount, y + projection.y * amount);
          await page.waitForTimeout(160);
        }
        const during = await stats();
        await touch("touchEnd");
        const released = await stats();
        if (mode === "fling") {
          assert.equal(released.archiveMomentum?.phase, "coasting");
          assert.ok(Math.abs(released.archiveMomentum.value.lane - released.columnCamera / 5.2) < 0.05);
          await page.waitForTimeout(250);
          const coasting = await stats();
          assert.ok(coasting.columnCamera > released.columnCamera + 0.3);
        } else {
          assert.equal(during.selectedCell.lane, before.selectedCell.lane + Math.round(amount));
        }
        await page.waitForFunction(() => !window.rhine.stats().archiveMomentum, null, {
          timeout: 12000,
        });
        await page.waitForTimeout(1800);
        const settled = await stats();
        assert.ok(Math.abs(settled.columnCamera - settled.selectedCell.lane * 5.2) < 0.1);
        if (mode !== "fling") {
          assert.equal(settled.selectedCell.lane, before.selectedCell.lane + Math.round(amount));
        }
        assert.deepEqual(pageErrors, []);
        const result = {
          viewport: `${width}x${height}`,
          mode,
          duringLane: during.selectedCell.lane,
          releasedPhase: released.archiveMomentum?.phase ?? null,
          settledLane: settled.selectedCell.lane,
          settledCamera: settled.columnCamera,
          status: "passed",
        };
        results.push(result);
        console.log(JSON.stringify(result));
      } finally {
        await context.close();
      }
    }
  }
} finally {
  await browser.close();
}
