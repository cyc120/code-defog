import assert from "node:assert/strict";
import { resolve, join } from "node:path";
import { pathToFileURL } from "node:url";

const repository = resolve(process.argv[2] ?? "../RhineLabUI");
const moduleUrl = pathToFileURL(join(repository, "scripts/archive-content.mjs"));
const { loadContent, validateContent, archiveText } = await import(moduleUrl.href);
const content = structuredClone(await loadContent());
delete content.records[0].sections[0].items;

let validation;
try {
  validateContent(content);
  validation = "accepted";
} catch (error) {
  validation = `${error.name}: ${error.message}`;
}

let exportResult = "blocked by validation";
if (validation === "accepted") {
  try {
    archiveText(content.records[0]);
    exportResult = "generated";
  } catch (error) {
    exportResult = `${error.name}: ${error.message}`;
  }
}

console.log(JSON.stringify({ validation, exportResult }, null, 2));
assert.notEqual(validation, "accepted", "缺少 sections.items 的数据应在校验阶段被拒绝");
assert.match(validation, /records\[0\].sections\[0\].items/);
