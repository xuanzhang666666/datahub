const assert = require('node:assert/strict');
const test = require('node:test');

const { createDaemonHandlers } = require('../../src/mcp/daemon_handlers');

function handlersFor(responses) {
  const calls = [];
  const request = async (payload) => {
    calls.push(payload);
    return responses.shift();
  };
  const handlers = createDaemonHandlers({
    request,
    appDataPath: '/var/lib/btalk/prod',
    flattenConversations: ({ normal = [], top = [] }) => [...top, ...normal],
    formatTime: (message) => `time:${message.timestamp}`,
    renderBody: (body) => body.text,
    localPathOf: (message, appDataPath) => `${appDataPath}/${message.id}`,
  });
  return { calls, handlers };
}

test('conversation handlers use the daemon socket instead of a native SDK instance', async () => {
  const { calls, handlers } = handlersFor([
    { ok: true, data: { normal: [{ id: 'zhang.san', chatType: 'chat', cnName: '张三', unread_msg_cont: 2 }], top: [{ id: 'team', chatType: 'groupchat', name: '团队' }] } },
    { ok: true, data: { message: [{ id: 'message-1', timestamp: 42, fromID: 'zhang.san', body: { text: 'hello' }, msgType: 1, conversationID: 'zhang.san' }] } },
  ]);
  assert.deepEqual(await handlers.list_conversations(), [{ id: 'team', type: 'groupchat', name: '团队', unread: 0 }, { id: 'zhang.san', type: 'chat', name: '张三', unread: 2 }]);
  assert.deepEqual(await handlers.fetch_history({ conversation_id: 'Zhang.San', count: 5 }), [{ id: 'message-1', time: 'time:42', from: 'zhang.san', text: 'hello', type: 1, conversation: 'zhang.san' }]);
  assert.deepEqual(calls, [{ cmd: 'sessions' }, { cmd: 'history', target: 'zhang.san', count: 5 }]);
});

test('send and file-path handlers preserve daemon responses without loading the SDK', async () => {
  const { calls, handlers } = handlersFor([
    { ok: true, data: { id: 'sent-message' } },
    { ok: true, data: { id: 'sent-file' } },
    { ok: true, data: { message: [{ id: 'image-1', msgType: 3, body: { text: '' }, conversationID: 'zhang.san' }] } },
  ]);
  assert.deepEqual(await handlers.send_message({ to: 'Zhang.San', text: 'hello', is_group: false }), { ok: true, id: 'sent-message' });
  assert.deepEqual(await handlers.send_file({ to: 'Zhang.San', file_path: '/tmp/a.txt', is_group: false }), { ok: true, id: 'sent-file' });
  assert.deepEqual(await handlers.get_image_path({ conversation_id: 'Zhang.San' }), { found: true, id: 'image-1', type: 'image', local_path: '/var/lib/btalk/prod/image-1' });
  assert.deepEqual(calls, [{ cmd: 'send', to: 'zhang.san', body: 'hello' }, { cmd: 'send-file', to: 'Zhang.San', filePath: '/tmp/a.txt' }, { cmd: 'history', target: 'zhang.san', count: 50 }]);
});

test('daemon failures become MCP failures instead of empty results', async () => {
  const { handlers } = handlersFor([{ ok: false, error: 'database unavailable' }]);
  await assert.rejects(handlers.list_conversations(), /database unavailable/);
});

test('fetch_history normalizes the native array returned by btalkd', async () => {
  const { calls, handlers } = handlersFor([{ ok: true, data: [{ id: 'message-2', timestamp: 43, fromID: 'zhang.san', body: { text: 'from native array' }, msgType: 1, conversationID: 'zhang.san' }] }]);
  assert.deepEqual(await handlers.fetch_history({ conversation_id: 'Zhang.San', count: 1 }), [{ id: 'message-2', time: 'time:43', from: 'zhang.san', text: 'from native array', type: 1, conversation: 'zhang.san' }]);
  assert.deepEqual(calls, [{ cmd: 'history', target: 'zhang.san', count: 1 }]);
});
