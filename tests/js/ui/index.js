'use strict';
// Lets `node --test tests/js/ui/` work on Node 22 (a directory argument resolves
// to this index module). Equivalent: node --test 'tests/js/ui/*.test.cjs'
const fs = require('node:fs');
const path = require('node:path');
for (const f of fs.readdirSync(__dirname).filter((n) => n.endsWith('.test.cjs')).sort()) {
  require(path.join(__dirname, f));
}
