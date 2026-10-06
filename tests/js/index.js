"use strict";
// Entry point so that `node --test tests/js/` works on Node versions that do not
// expand directory arguments (Node 22 resolves the directory to this file).
// Each *.test.cjs file can also be run on its own: node --test tests/js/*.test.cjs
const fs = require("node:fs");
const path = require("node:path");

for (const f of fs.readdirSync(__dirname).sort()) {
  if (/\.test\.c?js$/.test(f)) require(path.join(__dirname, f));
}
