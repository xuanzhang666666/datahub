'use strict';

const os = require('os');
const path = require('path');

function defaultAppDataPath() {
  const home = process.env.BTALK_HOME || path.join(os.homedir(), '.btalk');
  return path.join(home, process.env.BTALK_ENV || 'prod');
}

function unwrap(response) {
  if (!response || !response.ok) {
    throw new Error((response && response.error) || 'btalkd request failed');
  }
  return response.data || {};
}

function historyMessages(data) {
  const parsed = typeof data === 'string' ? JSON.parse(data) : data;
  if (Array.isArray(parsed)) return parsed;
  return (parsed && parsed.message) || [];
}

function createDaemonHandlers({
  request,
  flattenConversations,
  formatTime,
  renderBody,
  localPathOf,
  appDataPath = defaultAppDataPath(),
}) {
  async function sessions() {
    return unwrap(await request({ cmd: 'sessions' }));
  }

  async function history(conversationId, count) {
    return historyMessages(
      unwrap(
        await request({
          cmd: 'history',
          target: conversationId.toLowerCase(),
          count,
        }),
      ),
    );
  }

  return {
    async list_conversations() {
      return flattenConversations(await sessions()).map((conversation) => ({
        id: conversation.id,
        type: conversation.chatType,
        name: conversation.name || conversation.cnName || conversation.fullname || '',
        unread: conversation.unread_msg_cont || 0,
      }));
    },

    async search_contact({ q }) {
      const query = q.toLowerCase();
      return flattenConversations(await sessions())
        .filter(
          (conversation) =>
            (conversation.id || '').toLowerCase().includes(query) ||
            (conversation.name || '').toLowerCase().includes(query) ||
            (conversation.cnName || '').toLowerCase().includes(query) ||
            (conversation.fullname || '').toLowerCase().includes(query),
        )
        .map((conversation) => ({
          id: conversation.id,
          name: conversation.name || conversation.cnName || conversation.fullname || '',
        }));
    },

    async fetch_history({ conversation_id, count }) {
      return (await history(conversation_id, count || 20)).map((message) => ({
        id: message.id,
        time: formatTime(message),
        from: message.fromID,
        text: renderBody(message.body, message),
        type: message.msgType,
        conversation: message.conversationID,
      }));
    },

    async send_message({ to, text }) {
      const data = unwrap(
        await request({ cmd: 'send', to: to.toLowerCase(), body: text }),
      );
      return { ok: true, id: data.id };
    },

    async send_file({ to, file_path }) {
      const data = unwrap(
        await request({ cmd: 'send-file', to, filePath: file_path }),
      );
      return { ok: true, id: data.id };
    },

    async get_image_path({ conversation_id, message_id }) {
      const messages = await history(conversation_id, 50);
      const files = messages.filter(
        (message) => message.msgType === 3 || message.msgType === 5,
      );
      const message = message_id
        ? files.find((candidate) => candidate.id === message_id)
        : files[files.length - 1];
      if (!message) return { found: false };
      return {
        found: true,
        id: message.id,
        type: message.msgType === 3 ? 'image' : 'file',
        local_path: localPathOf(message, appDataPath),
      };
    },
  };
}

module.exports = { createDaemonHandlers };
