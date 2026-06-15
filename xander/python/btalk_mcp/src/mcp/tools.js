'use strict';
const { z } = require('zod');
const { execFile } = require('child_process');

const { createBtalk, login, waitConnected } = require('../index');
const { sendFile } = require('../files');
const { flattenConversations, renderBody, formatTime, localPathOf } = require('../cli/commands');

let _btalkPromise = null;
function btalk() {
  if (!_btalkPromise) {
    _btalkPromise = (async () => {
      const b = await createBtalk();
      const w = waitConnected(b);
      await login(b);
      await w;
      return b;
    })().catch((e) => {
      _btalkPromise = null;
      throw e;
    });
  }
  return _btalkPromise;
}

function runBtalk(args) {
  return new Promise((resolve, reject) => {
    execFile('btalk', args, { timeout: 60000, maxBuffer: 10 * 1024 * 1024 }, (err, stdout, stderr) => {
      const text = String(stdout || stderr || '').trim();
      let parsed = null;
      if (text) {
        try { parsed = JSON.parse(text.split('\n').filter(Boolean).pop()); } catch (_) {}
      }
      if (err && !parsed) return reject(new Error((stderr || err.message || '').trim()));
      if (!parsed) return resolve({ ok: !err, stdout: text, stderr: String(stderr || '').trim() });
      if (parsed.ok === false) return reject(new Error(parsed.error || JSON.stringify(parsed)));
      resolve(parsed);
    });
  });
}

const schemas = {
  list_conversations: z.object({}),
  search_contact: z.object({ q: z.string().describe('联系人姓名/uid 子串') }),
  fetch_history: z.object({
    conversation_id: z.string().describe('会话 id 或 uid(会话不存在则按 uid 拉)'),
    count: z.number().int().min(1).max(200).default(20),
    is_group: z.boolean().optional().describe('是否群聊，默认 false'),
  }),
  send_message: z.object({
    to: z.string().describe('uid 或会话 id'),
    text: z.string(),
    is_group: z.boolean().optional().describe('是否群聊，默认 false'),
  }),
  send_file: z.object({
    to: z.string(),
    file_path: z.string().describe('绝对路径'),
    is_group: z.boolean().optional().describe('是否群聊，默认 false'),
  }),
  get_image_path: z.object({
    conversation_id: z.string(),
    message_id: z.string().optional().describe('指定消息 id;不传则取最新一条图片/文件消息'),
  }),

  btalk_status: z.object({}),
  wsso_cookie: z.object({ reset: z.boolean().default(false).describe('是否强制刷新 SSO Cookie') }),
  ripple_list: z.object({
    tab: z.number().int().min(1).max(4).default(1).describe('1待处理,2已发起,3我关注,4已处理'),
    page: z.number().int().min(1).default(1),
    category: z.string().optional().describe('flowCode 流程类别代码，可选'),
    keywords: z.string().optional().describe('关键词搜索任务名/编码，可选'),
  }),
  ripple_show: z.object({
    flow_order_id: z.union([z.string(), z.number()]).describe('流程工单号 flowOrderId'),
  }),
  ripple_action: z.object({
    flow_order_id: z.string().describe('流程工单号 flowOrderId'),
    operation: z.string().describe('操作类型或别名，如 ACCEPT/领取、APPROVE/处理、FEEDBACK/反馈、ASSIGN/转交'),
    show_form: z.boolean().optional().describe('只查看表单字段，不提交（默认 false）'),
    auto: z.boolean().optional().describe('使用默认值自动提交（默认 false）'),
    fields: z.string().optional().describe('表单字段 JSON 字符串，如 {"alertReason":"888"}，不填则为空'),
  }),
  fetch_internal: z.object({
    url: z.string().url().describe('内部 http(s) URL，会自动带 wsso cookie 和身份 header'),
    method: z.enum(['GET', 'POST']).default('GET'),
    data: z.string().optional().describe('POST 数据，JSON 字符串或普通文本'),
  }),
};

const descriptors = [
  { name: 'list_conversations', description: '列出当前账号的所有会话(置顶+普通)', inputSchema: z.toJSONSchema(schemas.list_conversations) },
  { name: 'search_contact', description: '按 q 子串过滤会话列表', inputSchema: z.toJSONSchema(schemas.search_contact) },
  { name: 'fetch_history', description: '拉取会话历史消息', inputSchema: z.toJSONSchema(schemas.fetch_history) },
  { name: 'send_message', description: '发送文本消息', inputSchema: z.toJSONSchema(schemas.send_message) },
  { name: 'send_file', description: '上传并发送文件/图片(走蜂盘,100MB 上限)', inputSchema: z.toJSONSchema(schemas.send_file) },
  { name: 'get_image_path', description: '取图片/文件消息的本地缓存路径(用于 OCR/进一步处理)', inputSchema: z.toJSONSchema(schemas.get_image_path) },
  { name: 'btalk_status', description: '查看 btalk daemon 登录与运行状态。排查 MCP/btalk 是否可用时先调用。', inputSchema: z.toJSONSchema(schemas.btalk_status) },
  { name: 'wsso_cookie', description: '获取或强制刷新公司内部系统 SSO Cookie。Ripple 工单命令报响应解析失败/401 时用 reset=true。', inputSchema: z.toJSONSchema(schemas.wsso_cookie) },
  { name: 'ripple_list', description: '查询 Ripple/蜂利器流程工单列表，默认查待处理。可按 tab/category/keywords 过滤。', inputSchema: z.toJSONSchema(schemas.ripple_list) },
  { name: 'ripple_show', description: '查看 Ripple 工单详情，包括表单数据、处理历史、当前节点和可用操作。', inputSchema: z.toJSONSchema(schemas.ripple_show) },
  { name: 'ripple_action', description: '执行 Ripple 工单操作：领取 ACCEPT、处理 APPROVE、反馈 FEEDBACK、转交 ASSIGN。可 show_form 查看字段，或 fields 填表提交。', inputSchema: z.toJSONSchema(schemas.ripple_action) },
  { name: 'fetch_internal', description: '用 btalk wsso cookie 访问内部 HTTP 接口，适合调用公司内网页面/API。', inputSchema: z.toJSONSchema(schemas.fetch_internal) },
];

const rawHandlers = {
  async list_conversations(_args) {
    const b = await btalk();
    return flattenConversations(await b.getAllConversationsFromNewSDK()).map((c) => ({
      id: c.id, type: c.chatType, name: c.name || c.cnName || c.fullname || '', unread: c.unread_msg_cont || 0,
    }));
  },
  async search_contact({ q }) {
    const b = await btalk();
    const list = flattenConversations(await b.getAllConversationsFromNewSDK());
    const ql = q.toLowerCase();
    return list.filter((c) => (c.id || '').toLowerCase().includes(ql) || (c.name || '').toLowerCase().includes(ql) || (c.cnName || '').toLowerCase().includes(ql) || (c.fullname || '').toLowerCase().includes(ql))
               .map((c) => ({ id: c.id, name: c.name || c.cnName || c.fullname || '' }));
  },
  async fetch_history({ conversation_id, count, is_group }) {
    const b = await btalk();
    const raw = await b.fetchHistoryMessages({
      conversationId: conversation_id.toLowerCase(), pageSize: count, isGroupChat: !!is_group, forceFetchRemote: true,
    });
    const data = typeof raw === 'string' ? JSON.parse(raw) : (raw || {});
    return (data.message || []).map((m) => ({
      id: m.id, time: formatTime(m), from: m.fromID, text: renderBody(m.body, m),
      type: m.msgType, conversation: m.conversationID,
    }));
  },
  async send_message({ to, text, is_group }) {
    const b = await btalk();
    const ret = await b.commit({ toID: to.toLowerCase(), body: text, chatType: is_group ? 'groupchat' : 'chat' });
    return { ok: true, id: ret && ret.id };
  },
  async send_file({ to, file_path, is_group }) {
    const b = await btalk();
    const ret = await sendFile(b, { toID: to, filePath: file_path, chatType: is_group ? 'groupchat' : 'chat' });
    return { ok: true, id: ret && ret.id };
  },
  async get_image_path({ conversation_id, message_id }) {
    const b = await btalk();
    const raw = await b.fetchHistoryMessages({ conversationId: conversation_id.toLowerCase(), pageSize: 50, forceFetchRemote: true });
    const data = typeof raw === 'string' ? JSON.parse(raw) : (raw || {});
    const list = (data.message || []).filter((m) => m.msgType === 3 || m.msgType === 5);
    const m = message_id ? list.find((x) => x.id === message_id) : list[list.length - 1];
    if (!m) return { found: false };
    const p = localPathOf(m, b.appDataPath);
    return { found: true, id: m.id, type: m.msgType === 3 ? 'image' : 'file', local_path: p };
  },

  async btalk_status() {
    return runBtalk(['status']);
  },
  async wsso_cookie({ reset }) {
    const args = ['wsso'];
    if (reset) args.push('--reset');
    return runBtalk(args);
  },
  async ripple_list({ tab, page, category, keywords }) {
    const args = ['ripple', 'list', '--tab', String(tab), '--page', String(page)];
    if (category) args.push('--category', category);
    if (keywords) args.push('--keywords', keywords);
    return runBtalk(args);
  },
  async ripple_show({ flow_order_id }) {
    return runBtalk(['ripple', 'show', String(flow_order_id)]);
  },
  async ripple_action({ flow_order_id, operation, show_form, auto, fields }) {
    const args = ['ripple', 'action', String(flow_order_id), operation];
    if (show_form) args.push('--show-form');
    if (auto) args.push('--auto');
    const fieldsObj = typeof fields === 'string' ? JSON.parse(fields || '{}') : (fields || {});
    for (const [k, v] of Object.entries(fieldsObj)) args.push('--field', `${k}=${v}`);
    return runBtalk(args);
  },
  async fetch_internal({ url, method, data }) {
    const args = ['fetch', url];
    if (method && method !== 'GET') args.push('-X', method);
    if (data !== undefined) args.push('-d', data);
    return runBtalk(args);
  },
};

const handlers = {};
for (const k of Object.keys(rawHandlers)) {
  handlers[k] = async (args) => rawHandlers[k](schemas[k].parse(args || {}));
}

module.exports = { descriptors, handlers };
