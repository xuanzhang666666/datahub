const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

test('MCP tools delegate IM operations to btalkd instead of importing the native SDK', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../src/mcp/tools.js'), 'utf8');
  assert.match(source, /createDaemonHandlers/);
  assert.match(source, /require\('\.\.\/cli\/client'\)/);
  assert.doesNotMatch(source, /require\('\.\.\/index'\)/);
  assert.doesNotMatch(source, /await b\./);
});
