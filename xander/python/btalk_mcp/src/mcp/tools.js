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
    count: z.number().int().min(1).max(200).optional().describe('拉取条数，默认 20，最多 200'),
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
  btalk_version: z.object({}),
  user_lookup: z.object({
    action: z.enum(['me', 'get', 'search']).describe('me=当前登录用户,get=按 uid 查询,search=按关键词搜索'),
    uid: z.string().optional().describe('action=get 时必填'),
    keyword: z.string().optional().describe('action=search 时必填'),
    mobile: z.boolean().optional().describe('是否返回手机号，默认 false；涉及隐私，需显式开启'),
  }),
  group_manage: z.object({
    action: z.enum(['create', 'invite', 'kick', 'list', 'info', 'leave']).describe('群操作'),
    group_id: z.string().optional().describe('invite/kick/info/leave 时的群 id'),
    members: z.array(z.string()).optional().describe('create/invite/kick 的成员 uid 或姓名列表'),
    name: z.string().optional().describe('create 时可选群名'),
  }),
  otp: z.object({}),
  wsso_cookie: z.object({ reset: z.boolean().optional().describe('是否强制刷新 SSO Cookie，默认 false') }),
  ripple_list: z.object({
    tab: z.number().int().min(1).max(4).optional().describe('1待处理,2已发起,3我关注,4已处理，默认 1'),
    page: z.number().int().min(1).optional().describe('页码，默认 1'),
    size: z.number().int().min(1).max(200).optional().describe('每页条数，默认由 CLI 决定'),
    category: z.string().optional().describe('flowCode 流程类别代码，可选'),
    keywords: z.string().optional().describe('关键词搜索任务名/编码，可选'),
    start_date: z.string().optional().describe('开始日期 YYYY-MM-DD'),
    end_date: z.string().optional().describe('结束日期 YYYY-MM-DD'),
    time_type: z.enum(['CREATE_TIME', 'HANDLE_TIME', 'FOLLOW_TIME']).optional().describe('时间类型过滤'),
    status: z.string().optional().describe('状态过滤，可逗号多选，如 NEW_ORDER,PROCESSING,FINISHED,SUSPEND,PENDING'),
  }),
  ripple_category: z.object({
    tab: z.number().int().min(1).max(4).optional().describe('1待处理,2已发起,3我关注,4已处理，默认 1'),
  }),
  ripple_show: z.object({
    flow_order_id: z.string().describe('流程工单号 flowOrderId'),
  }),
  ripple_short_url: z.object({
    flow_order_id: z.string().describe('流程工单号 flowOrderId'),
  }),
  ripple_resolve: z.object({
    token_or_url: z.string().describe('Ripple 短链或 token'),
  }),
  ripple_group_chat: z.object({
    flow_order_id: z.string().describe('流程工单号 flowOrderId；返回 imGroupNo，可作为群会话 id'),
  }),
  ripple_flow_search: z.object({
    keyword: z.string().describe('流程模板关键词，用于查 flowCode'),
  }),
  ripple_create_order: z.object({
    flow_code: z.string().describe('流程模板 flowCode'),
    show_form: z.boolean().optional().describe('只查看发起表单字段，不保存/提交'),
    from: z.string().optional().describe('父单号；用于下达子流程'),
    title: z.string().optional().describe('工单标题'),
    fields: z.string().optional().describe('表单字段 JSON 字符串，如 {"field":"value"}；默认保存草稿'),
    submit: z.boolean().optional().describe('是否真正提交；默认 false，只保存草稿，便于人工审核'),
  }),
  ripple_action: z.object({
    flow_order_id: z.string().describe('流程工单号 flowOrderId'),
    operation: z.string().describe('操作类型或别名，如 ACCEPT/领取、APPROVE/处理、FEEDBACK/反馈、ASSIGN/转交'),
    show_form: z.boolean().optional().describe('只查看表单字段，不提交（默认 false）'),
    auto: z.boolean().optional().describe('使用默认值自动提交（默认 false）'),
    draft: z.boolean().optional().describe('只保存动作草稿不提交，桌面端可载入审核后提交'),
    fields: z.string().optional().describe('表单字段 JSON 字符串，如 {"alertReason":"888"}，不填则为空'),
  }),
  fetch_internal: z.object({
    url: z.string().url().describe('内部 http(s) URL，会自动带 wsso cookie 和身份 header'),
    method: z.enum(['GET', 'POST']).optional().describe('HTTP 方法，默认 GET'),
    data: z.string().optional().describe('POST 数据，JSON 字符串或普通文本'),
    headers: z.record(z.string(), z.string()).optional().describe('额外请求头，会转换为 btalk fetch -H "k: v"'),
  }),
  pc_helper: z.object({
    action: z.enum(['to-me', 'status', 'stop']).describe('个人电脑助手操作；config/_run 不通过 MCP 暴露'),
    text: z.string().optional().describe('to-me 文本内容'),
    images: z.array(z.string()).optional().describe('to-me 图片路径或 URL 列表'),
    file: z.string().optional().describe('to-me 文件路径'),
    header: z.string().optional().describe('to-me 可选头部文本'),
    footer: z.string().optional().describe('to-me 可选尾部文本'),
    ext: z.string().optional().describe('to-me 可选 ext JSON 字符串'),
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
  { name: 'btalk_version', description: '查看 btalk CLI 与 Node 版本，确认 MCP 是否已升级到新版。', inputSchema: z.toJSONSchema(schemas.btalk_version) },
  { name: 'user_lookup', description: '查询当前用户、按 uid 查用户、或按关键词搜索用户；手机号默认不返回。', inputSchema: z.toJSONSchema(schemas.user_lookup) },
  { name: 'group_manage', description: '群聊管理：建群、拉人、踢人、列表、详情、退群。成员支持 uid 或姓名。', inputSchema: z.toJSONSchema(schemas.group_manage) },
  { name: 'otp', description: '获取 6 位动态口令，常用于登录跳板机等需要二次验证的场景。', inputSchema: z.toJSONSchema(schemas.otp) },
  { name: 'wsso_cookie', description: '获取或强制刷新公司内部系统 SSO Cookie。Ripple 工单命令报响应解析失败/401 时用 reset=true。', inputSchema: z.toJSONSchema(schemas.wsso_cookie) },
  { name: 'ripple_list', description: '查询 Ripple/蜂利器流程工单列表，默认查待处理。可按 tab/category/keywords/日期/状态过滤。', inputSchema: z.toJSONSchema(schemas.ripple_list) },
  { name: 'ripple_category', description: '查询 Ripple 某个 tab 下的流程类别统计，返回 flowCode 和数量。', inputSchema: z.toJSONSchema(schemas.ripple_category) },
  { name: 'ripple_show', description: '查看 Ripple 工单详情，包括表单数据、处理历史、当前节点和可用操作。', inputSchema: z.toJSONSchema(schemas.ripple_show) },
  { name: 'ripple_short_url', description: '生成 Ripple 工单短链，可分享给他人或用于快速打开工单。', inputSchema: z.toJSONSchema(schemas.ripple_short_url) },
  { name: 'ripple_resolve', description: '解析 Ripple 短链或 token，反查 flowOrderId 和工单概要。', inputSchema: z.toJSONSchema(schemas.ripple_resolve) },
  { name: 'ripple_group_chat', description: '发起或进入 Ripple 工单群聊，返回 imGroupNo，可直接作为群会话 id。', inputSchema: z.toJSONSchema(schemas.ripple_group_chat) },
  { name: 'ripple_flow_search', description: '搜索 Ripple 流程模板，获取发起工单需要的 flowCode。', inputSchema: z.toJSONSchema(schemas.ripple_flow_search) },
  { name: 'ripple_create_order', description: '按 flowCode 发起 Ripple 工单；默认只保存草稿，submit=true 才真正提交。', inputSchema: z.toJSONSchema(schemas.ripple_create_order) },
  { name: 'ripple_action', description: '执行 Ripple 工单操作：领取/处理/反馈/转交等。支持 show_form 查看字段、draft 保存草稿、auto 或 fields 提交。', inputSchema: z.toJSONSchema(schemas.ripple_action) },
  { name: 'fetch_internal', description: '用 btalk wsso cookie 访问内部 HTTP 接口，适合调用公司内网页面/API。', inputSchema: z.toJSONSchema(schemas.fetch_internal) },
  { name: 'pc_helper', description: '个人电脑助手：给自己发消息、查看监听状态或停止监听。', inputSchema: z.toJSONSchema(schemas.pc_helper) },
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
      conversationId: conversation_id.toLowerCase(), pageSize: count ?? 20, isGroupChat: !!is_group, forceFetchRemote: true,
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
  async btalk_version() {
    return runBtalk(['version']);
  },
  async user_lookup({ action, uid, keyword, mobile }) {
    const args = ['user', action];
    if (action === 'get') {
      if (!uid) throw new Error('user_lookup action=get 需要 uid');
      args.push(uid);
    } else if (action === 'search') {
      if (!keyword) throw new Error('user_lookup action=search 需要 keyword');
      args.push(keyword);
    }
    if (mobile) args.push('--mobile');
    return runBtalk(args);
  },
  async group_manage({ action, group_id, members, name }) {
    const args = ['group', action];
    if (action === 'create') {
      if (name) args.push('--name', name);
      for (const member of members || []) args.push(member);
    } else if (action === 'invite' || action === 'kick') {
      if (!group_id) throw new Error(`group_manage action=${action} 需要 group_id`);
      args.push(group_id);
      for (const member of members || []) args.push(member);
    } else if (action === 'info' || action === 'leave') {
      if (!group_id) throw new Error(`group_manage action=${action} 需要 group_id`);
      args.push(group_id);
    }
    return runBtalk(args);
  },
  async otp() {
    return runBtalk(['otp']);
  },
  async wsso_cookie({ reset }) {
    const args = ['wsso'];
    if (reset) args.push('--reset');
    return runBtalk(args);
  },
  async ripple_list({ tab, page, size, category, keywords, start_date, end_date, time_type, status }) {
    const args = ['ripple', 'list', '--tab', String(tab ?? 1), '--page', String(page ?? 1)];
    if (size) args.push('--size', String(size));
    if (category) args.push('--category', category);
    if (keywords) args.push('--keywords', keywords);
    if (start_date) args.push('--start-date', start_date);
    if (end_date) args.push('--end-date', end_date);
    if (time_type) args.push('--time-type', time_type);
    if (status) args.push('--status', status);
    return runBtalk(args);
  },
  async ripple_category({ tab }) {
    return runBtalk(['ripple', 'category', '--tab', String(tab ?? 1)]);
  },
  async ripple_show({ flow_order_id }) {
    return runBtalk(['ripple', 'show', String(flow_order_id)]);
  },
  async ripple_short_url({ flow_order_id }) {
    return runBtalk(['ripple', 'short_url', String(flow_order_id)]);
  },
  async ripple_resolve({ token_or_url }) {
    return runBtalk(['ripple', 'resolve', token_or_url]);
  },
  async ripple_group_chat({ flow_order_id }) {
    return runBtalk(['ripple', 'group_chat', String(flow_order_id)]);
  },
  async ripple_flow_search({ keyword }) {
    return runBtalk(['ripple', 'flow_search', keyword]);
  },
  async ripple_create_order({ flow_code, show_form, from, title, fields, submit }) {
    const args = ['ripple', 'create_order', flow_code];
    if (show_form) args.push('--show-form');
    if (from) args.push('--from', from);
    if (title) args.push('--title', title);
    const fieldsObj = typeof fields === 'string' ? JSON.parse(fields || '{}') : (fields || {});
    for (const [k, v] of Object.entries(fieldsObj)) args.push('--field', `${k}=${v}`);
    if (submit) args.push('--submit');
    return runBtalk(args);
  },
  async ripple_action({ flow_order_id, operation, show_form, auto, draft, fields }) {
    const args = ['ripple', 'action', String(flow_order_id), operation];
    if (show_form) args.push('--show-form');
    if (auto) args.push('--auto');
    if (draft) args.push('--draft');
    const fieldsObj = typeof fields === 'string' ? JSON.parse(fields || '{}') : (fields || {});
    for (const [k, v] of Object.entries(fieldsObj)) args.push('--field', `${k}=${v}`);
    return runBtalk(args);
  },
  async fetch_internal({ url, method, data, headers }) {
    const args = ['fetch', url];
    if (method && method !== 'GET') args.push('-X', method);
    if (data !== undefined) args.push('-d', data);
    for (const [k, v] of Object.entries(headers || {})) args.push('-H', `${k}: ${v}`);
    return runBtalk(args);
  },
  async pc_helper({ action, text, images, file, header, footer, ext }) {
    const args = ['pc_helper', action];
    if (action === 'to-me') {
      if (header) args.push('--header', header);
      if (footer) args.push('--footer', footer);
      if (ext) args.push('--ext', ext);
      for (const image of images || []) args.push('--image', image);
      if (file) args.push('--file', file);
      if (text) args.push(text);
    }
    return runBtalk(args);
  },
};

const handlers = {};
for (const k of Object.keys(rawHandlers)) {
  handlers[k] = async (args) => rawHandlers[k](schemas[k].parse(args || {}));
}

module.exports = { descriptors, handlers };
