'use strict';
// 父进程 bin/btalk-mcp.js 做过滤代理:本进程 stdout 中含 jsonrpc=2.0 的 JSON 行进 MCP 通道,
// 其他全转 stderr。所以这里可以直接用标准 StdioServerTransport,SDK 的 console.log/native printf
// 都会被父进程过滤掉。

// 兜底:nedb 1.8 在并发/竞争场景偶发 ENOENT rename 抛 uncaughtException(数据库写临时文件
// → rename 时找不到 source)。这不影响业务,记录后继续运行。
process.on('uncaughtException', (err) => {
  process.stderr.write(`[btalk-mcp] uncaughtException: ${err.message}\n`);
});
process.on('unhandledRejection', (err) => {
  process.stderr.write(`[btalk-mcp] unhandledRejection: ${err && err.message || err}\n`);
});

const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { ListToolsRequestSchema, CallToolRequestSchema } = require('@modelcontextprotocol/sdk/types.js');

const tools = require('./tools');

async function start() {
  const server = new Server({ name: 'btalk', version: '0.1.0' }, { capabilities: { tools: {} } });

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: tools.descriptors,
  }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const name = req.params.name;
    const args = req.params.arguments || {};
    const handler = tools.handlers[name];
    if (!handler) throw new Error(`unknown tool: ${name}`);
    try {
      const result = await handler(args);
      return { content: [{ type: 'text', text: typeof result === 'string' ? result : JSON.stringify(result, null, 2) }] };
    } catch (e) {
      return { content: [{ type: 'text', text: `Error: ${e.message}` }], isError: true };
    }
  });

  await server.connect(new StdioServerTransport());
  process.stderr.write('[btalk-mcp] listening on stdio\n');
}

module.exports = { start };

if (require.main === module) {
  start().catch((e) => {
    process.stderr.write(`[btalk-mcp] FATAL: ${e.stack || e.message}\n`);
    process.exit(1);
  });
}
