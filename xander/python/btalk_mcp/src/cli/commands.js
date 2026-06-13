'use strict';
// 瘦客户端命令:不加载 native addon,只跟 btalkd 守护进程通信。所有 stdout 输出都是 JSON(一行一个)。
// 交互提示(login 的账号/密码)走 stderr,保证 stdout 纯 JSON。
const readline = require('node:readline');
const client = require('./client');
const fmt = require('./format');

function out(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }
function fail(error, code = 1) { out({ ok: false, error: (error && error.message) || String(error) }); process.exit(code); }

// ---- 交互输入(仅 login 用,提示打到 stderr)----
let _rl = null;
function ask(q, { mute = false } = {}) {
  return new Promise((resolve) => {
    if (!_rl) _rl = readline.createInterface({ input: process.stdin, output: process.stderr });
    const rl = _rl;
    rl._writeToOutput = mute ? (s) => { if (s.includes(q)) rl.output.write(q); } : (s) => rl.output.write(s);
    rl.question(q, (a) => resolve(a));
  });
}
function closeAsk() { if (_rl) { _rl.close(); _rl = null; } }

async function cmdLogin(_, flags = {}) {
  if (flags.qrcode || flags.qr) {
    // 扫码登录:二维码打到 stderr(保持 stdout 纯 JSON),手机端蜂利器扫码
    const data = await client.loginQrcode((ascii) => {
      process.stderr.write('\n' + ascii + '\n请用手机蜂利器扫码登录...\n');
    });
    out({ ok: true, ...(data || {}) });
    process.exit(0);
  }
  if (flags.sms) return fail('daemon 模式暂不支持短信登录(用 --qrcode 扫码,或账密)');
  const username = process.env.BTALK_USER || (await ask('账号: '));
  const password = process.env.BTALK_PASS || (await ask('密码: ', { mute: true }));
  closeAsk();
  const r = await client.request({ cmd: 'login', username, password });
  if (!r.ok) return fail(r.error);
  out({ ok: true, ...(r.data || {}) });
  process.exit(0);
}

// 会话命令组(对齐桌面端语义):list=会话列表 / get=详情 / search=搜会话(联系人+群组) / <id|中文名>=历史
async function cmdConversation([sub, ...rest], flags = {}) {
  if (sub === 'list') return cmdSessions(flags);
  if (sub === 'get') return cmdGet(rest, flags);
  if (sub === 'search') {
    const kw = rest.join(' ');
    if (!kw) return fail('用法: btalk conversation search <关键词>', 2);
    const r = await client.request({ cmd: 'resolve', keyword: kw });
    if (!r.ok) return fail(r.error);
    const d = r.data || {};
    const conversations = [
      ...(d.users || []).map((u) => ({ id: u.uid || u.userId || '', name: u.cnName || u.name || '', type: 'single' })),
      ...(d.groups || []).map((g) => ({ id: g.id || g.groupId || '', name: g.name || g.groupName || '', type: 'group' })),
    ];
    out({ ok: true, conversations });
    return process.exit(0);
  }
  if (sub) return cmdHistory([sub], flags);   // conversation <会话id|中文名> → 历史消息
  return fail('用法: btalk conversation list | get <id> | search <关键词> | <会话id|中文名> [-n N]', 2);
}

async function cmdUser([sub, arg], flags = {}) {
  if (!sub || !['me', 'get', 'search'].includes(sub)) {
    return fail('用法: btalk user me | get <uid> | search <关键词>（加 --mobile 才返回手机号）', 2);
  }
  const req = { cmd: 'user', sub, mobile: !!flags.mobile };
  if (sub === 'get') { if (!arg) return fail('用法: btalk user get <uid>', 2); req.uid = arg; }
  if (sub === 'search') { if (!arg) return fail('用法: btalk user search <关键词>', 2); req.keyword = arg; }
  const r = await client.request(req);
  if (!r.ok) return fail(r.error);
  const d = r.data || {};
  if (sub === 'search') out({ ok: true, users: (d.users || []).map(fmt.userOut) });
  else out({ ok: true, user: fmt.userOut(d) });
  process.exit(0);
}

async function cmdSessions(flags = {}) {
  const r = await client.request({ cmd: 'sessions' });
  if (!r.ok) return fail(r.error);
  let list = fmt.flattenConversations(r.data).map(fmt.sessionOut);   // 本地全量会话(top+normal)
  const total = list.length;
  if (flags.limit) list = list.slice(0, Number(flags.limit));        // 会话列表不分页,--limit 仅截断显示
  out({ ok: true, total, sessions: list });
  process.exit(0);
}

async function cmdRipple([sub, orderId, actionType], flags) {
  const ripple = require('../ripple');
  const cr = await client.request({ cmd: 'wsso', reset: false });
  if (!cr.ok) return fail('取 SSO cookie 失败: ' + cr.error);
  const cookie = cr.data && cr.data.cookie;
  try {
    if (sub === 'category') {
      const data = await ripple.category(cookie, { tab: Number(flags.tab || 1) });
      out({ ok: true, ...data });
    } else if (sub === 'list') {
      const data = await ripple.list(cookie, { tab: Number(flags.tab || 1), page: Number(flags.page || 1), size: Number(flags.size || 50), flowCodes: flags.category, keyWords: flags.keywords });
      out({ ok: true, ...data });
    } else if (sub === 'show') {
      if (!orderId) return fail('用法: btalk ripple show <工单号>', 2);
      const d = await ripple.detail(cookie, orderId);
      out({ ok: true, order: { id: orderId, name: d.name, flow: d.flowName, flowCode: d.flowCode, category: d.category, initiator: d.initiator, createTime: d.createTime, status: d.orderStatus, result: d.orderResult, node: d.node, handlers: d.handlers, formData: d.formData, tasks: d.tasks, operations: d.operations } });
    } else if (sub === 'action') {
      let oid = orderId, op = actionType;
      // 顺序容错:工单号是长数字、操作是字母;写反了(action APPROVE <工单号>)自动纠正
      if (op && /^\d{6,}$/.test(op) && oid && /^[A-Za-z]/.test(oid)) { const t = oid; oid = op; op = t; }
      if (!oid || !op) return fail('用法: btalk ripple action <工单号> <操作> [--show-form|--auto|--field k=v]', 2);
      const userInputs = {};
      for (const kv of (flags.field || [])) { const i = kv.indexOf('='); if (i > 0) userInputs[kv.slice(0, i).trim()] = kv.slice(i + 1).trim(); }
      const res = await ripple.action(cookie, oid, op, { showForm: !!flags.showForm, auto: !!flags.auto, fields: userInputs });
      out({ ok: true, ...res });
    } else {
      return fail('用法: btalk ripple list|show|action', 2);
    }
  } catch (e) { return fail(e.message); }
  process.exit(0);
}

async function cmdOtp() {
  const r = await client.request({ cmd: 'otp' });
  if (!r.ok) return fail(r.error);
  out({ ok: true, otp: r.data && r.data.otp });
  process.exit(0);
}

async function cmdMessageSearch([...words], flags = {}) {
  const keyword = words.join(' ');
  if (!keyword) return fail('用法: btalk message-search <关键词> [--conversation-id <会话id>](默认全局搜)', 2);
  const r = await client.request({ cmd: 'message-search', keyword, conversationId: flags.conversationId });
  if (!r.ok) return fail(r.error);
  const messages = ((r.data && r.data.messages) || []).map(fmt.searchMsgOut);
  out({ ok: true, scope: flags.conversationId ? flags.conversationId : 'global', total: messages.length, messages });
  process.exit(0);
}

async function cmdFetch([url], flags = {}) {
  if (!url) return fail('用法: btalk fetch <url> [-X 方法] [-d 数据] [-H "k: v"](用 wsso cookie 访问内部接口)', 2);
  const headers = {};
  for (const h of (flags.httpHeaders || [])) { const i = h.indexOf(':'); if (i > 0) headers[h.slice(0, i).trim()] = h.slice(i + 1).trim(); }
  const r = await client.request({ cmd: 'fetch', url, method: flags.method, data: flags.data, headers });
  if (!r.ok) return fail(r.error);
  out({ ok: true, ...(r.data || {}) });
  process.exit(0);
}

async function cmdWsso(_, flags) {
  const r = await client.request({ cmd: 'wsso', reset: !!flags.reset });
  if (!r.ok) return fail(r.error);
  out({ ok: true, cookie: r.data && r.data.cookie, cached: !!(r.data && r.data.cached) });
  process.exit(0);
}

async function cmdDownload([url], flags) {
  if (!url) return fail('用法: btalk download <文件URL> [-o 输出路径]', 2);
  const r = await client.request({ cmd: 'download', url, output: flags.output });
  if (!r.ok) return fail(r.error);
  out({ ok: true, ...(r.data || {}) });
  process.exit(0);
}

async function cmdGet([id]) {
  if (!id) return fail('用法: btalk conversation get <会话id>', 2);
  const r = await client.request({ cmd: 'get-conv', id });
  if (!r.ok) return fail(r.error);
  out({ ok: true, conversation: r.data ? fmt.sessionOut(r.data) : null, raw: r.data });
  process.exit(0);
}

// 把一批 uid/中文名解析成 uid 列表(中文名走 resolve)
async function resolveMembers(names) {
  const ids = [];
  for (const n of names) {
    if (/[^\x00-\x7F]/.test(n)) {
      const rr = await client.request({ cmd: 'resolve', keyword: n });
      const hit = rr.ok && fmt.pickContactMatch((rr.data && rr.data.users) || [], n);
      if (!hit) return { error: `未找到联系人: ${n}` };
      ids.push(hit.uid);
    } else { ids.push(n); }
  }
  return { ids };
}

function groupIdOf(d) {
  if (!d) return '';
  if (Array.isArray(d)) d = d[0] || {};
  return d.id || d.groupId || d.gid || d.group_id || '';
}

async function cmdGroup([sub, ...rest], flags = {}) {
  if (sub === 'create') {
    if (!rest.length) return fail('用法: btalk group create [--name 群名] <成员uid|中文名...>', 2);
    const { ids, error } = await resolveMembers(rest);
    if (error) return fail(error);
    const r = await client.request({ cmd: 'group', sub: 'create', name: flags.name || '', ids });
    if (!r.ok) return fail(r.error);
    out({ ok: true, groupId: groupIdOf(r.data), members: ids, group: r.data });
  } else if (sub === 'invite' || sub === 'kick') {
    const [groupId, ...members] = rest;
    if (!groupId || !members.length) return fail(`用法: btalk group ${sub} <群id> <成员uid|中文名...>`, 2);
    const { ids, error } = await resolveMembers(members);
    if (error) return fail(error);
    const r = await client.request({ cmd: 'group', sub, groupId, ids });
    if (!r.ok) return fail(r.error);
    out({ ok: true, groupId, members: ids });
  } else if (sub === 'list') {
    const r = await client.request({ cmd: 'group', sub: 'list' });
    if (!r.ok) return fail(r.error);
    const groups = (Array.isArray(r.data) ? r.data : []).map((g) => ({ id: groupIdOf(g), name: g.name || g.groupName || g.fullname || '' }));
    out({ ok: true, groups });
  } else if (sub === 'info') {
    const [groupId] = rest;
    if (!groupId) return fail('用法: btalk group info <群id>', 2);
    const r = await client.request({ cmd: 'group', sub: 'info', groupId });
    if (!r.ok) return fail(r.error);
    out({ ok: true, group: r.data });
  } else if (sub === 'leave') {
    const [groupId] = rest;
    if (!groupId) return fail('用法: btalk group leave <群id>', 2);
    const r = await client.request({ cmd: 'group', sub: 'leave', groupId });
    if (!r.ok) return fail(r.error);
    out({ ok: true, left: groupId });
  } else {
    return fail('用法: btalk group create|invite|kick|list|info|leave', 2);
  }
  process.exit(0);
}

async function cmdSend([target, ...words], flags) {
  if (!target) return fail('用法: btalk send [--image 图] [--at uid] [--at-all] [--sign] <uid|会话id|中文名> [文本]', 2);
  const text = words.join(' ');
  const hasContent = text || (flags.images && flags.images.length);
  if (!hasContent) return fail('需要文本或 --image', 2);
  // 中文名解析(联系人 + 群名)
  let to = target;
  if (/[^\x00-\x7F]/.test(target)) {
    const rr = await client.request({ cmd: 'resolve', keyword: target });
    if (!rr.ok) return fail(rr.error);
    const hit = fmt.pickConversationMatch(rr.data, target);
    if (!hit) return fail(`未找到会话: ${target}`);
    to = hit.id;
  }
  const atUserList = [].concat(flags.at || []);
  if (flags.atAll) atUserList.push('ALL');   // @所有人标识为大写 ALL(对齐桌面端)
  const req = {
    cmd: 'send', to, sign: !!flags.sign,
    body: text,
    images: flags.images && flags.images.length ? flags.images : undefined,
    header: flags.header || undefined,
    footer: flags.footer || undefined,
    atUserList: atUserList.length ? atUserList : undefined,
  };
  const r = await client.request(req);
  if (!r.ok) return fail(r.error);
  out({ ok: true, to: to.toLowerCase() });
  process.exit(0);
}

async function cmdSendFile([target, filePath], flags) {
  if (!target || !filePath) return fail('用法: btalk send-file <uid|会话id> <文件路径>', 2);
  const r = await client.request({ cmd: 'send-file', to: target, filePath });
  if (!r.ok) return fail(r.error);
  out({ ok: true, to: target, id: r.data && r.data.id, file: r.data });
  process.exit(0);
}

async function cmdHistory([target], flags) {
  if (!target) return fail('用法: btalk conversation <会话id|中文名|群名> [-n 条数]', 2);
  let convId = target;
  // 不像 uid/会话id(出现中文或空格)→ 按名字解析(联系人 + 群名)
  if (/[^\x00-\x7F]/.test(target) || /\s/.test(target)) {
    const rr = await client.request({ cmd: 'resolve', keyword: target });
    if (!rr.ok) return fail(rr.error);
    const hit = fmt.pickConversationMatch(rr.data, target);
    if (!hit) return fail(`未找到会话: ${target}`);
    convId = hit.id;
  }
  const r = await client.request({ cmd: 'history', target: convId, count: Number(flags.n || 20), before: flags.before ? Number(flags.before) : undefined });
  if (!r.ok) return fail(r.error);
  const data = r.data || {};
  const messages = (data.message || []).map((m) => fmt.messageOut(m, { raw: !!flags.raw }));
  // 翻页提示:messages 时间升序,取最早一条的 time 作下一页 --before(往更早翻)
  const nextBefore = messages.length ? messages[0].time : null;
  out({ ok: true, target: convId.toLowerCase(), count: messages.length, nextBefore, messages });
  process.exit(0);
}

async function cmdWatch(_, flags) {
  let self = null;
  try { const p = await client.requestNoStart({ cmd: 'ping' }); self = p.data && p.data.uid; } catch {}
  const filter = !flags.all;   // 默认智能过滤,--all 全量
  const sock = await client.subscribe((m) => {
    if (filter && !fmt.watchPass(m, { self })) return;
    out({ event: 'message', ...fmt.messageOut(m, { raw: !!flags.raw }) });
  });
  out({ ok: true, event: 'watching', filter });   // 一行 JSON 表示已订阅;之后每条消息一行 JSON
  process.on('SIGINT', () => { try { sock.end(); } catch {} process.exit(0); });
  // 不退出,持续接收
}

async function cmdDaemon([sub] = []) {
  const { paths } = require('../daemon');
  const P = paths();
  try {
    if (sub === 'stop') {
      await client.requestNoStart({ cmd: 'stop' });
      out({ ok: true, stopped: true });
    } else if (sub === 'log') {
      process.stdout.write(require('fs').readFileSync(P.log, 'utf8'));
    } else if (sub === 'start') {
      await client.ensureDaemon();
      const r = await client.requestNoStart({ cmd: 'ping' });
      out({ ok: true, started: true, ...(r.data || {}) });
    } else {
      // status(默认)
      const r = await client.requestNoStart({ cmd: 'ping' });
      out({ ok: true, running: true, ...(r.data || {}), sock: P.sock, log: P.log });
    }
  } catch (e) {
    out({ ok: false, running: false, error: (e && e.message) || String(e), log: P.log });
  }
  process.exit(0);
}

async function cmdStatus() {
  const { paths } = require('../daemon');
  try {
    const r = await client.requestNoStart({ cmd: 'state' });
    const d = (r && r.data) || {};
    out({ ok: true, status: fmt.statusLabel(d.state), state: d.state, loggedIn: !!d.loggedIn, uid: d.uid || null, version: d.version, pid: d.pid });
  } catch {
    out({ ok: true, status: fmt.statusLabel('not_running'), state: 'not_running', running: false, log: paths().log });
  }
  process.exit(0);
}

async function cmdVersion() {
  out({ ok: true, version: require('../../package.json').version, node: process.version });
  process.exit(0);
}

async function cmdHelp() {
  out({ ok: true, help: HELP });
  process.exit(0);
}

async function cmdLogout() {
  try {
    await client.requestNoStart({ cmd: 'logout' });
    out({ ok: true, loggedIn: false, daemon: true });
  } catch {
    // daemon 没运行 → 直接删本地凭据
    try { require('fs').unlinkSync(require('../index').credFile()); } catch {}
    out({ ok: true, loggedIn: false, daemon: false });
  }
  process.exit(0);
}

async function cmdQuit() {
  try { await client.requestNoStart({ cmd: 'stop' }); out({ ok: true, stopped: true }); }
  catch { out({ ok: true, stopped: false, note: 'daemon 未运行' }); }
  process.exit(0);
}

async function cmdClear() {
  const dir = require('../device').dataDir();
  try { await client.requestNoStart({ cmd: 'stop' }); } catch {}
  await new Promise((r) => setTimeout(r, 300));   // 等 daemon 退出释放 sock/db 文件
  try { require('fs').rmSync(dir, { recursive: true, force: true }); }
  catch (e) { return fail('清理失败: ' + e.message); }
  out({ ok: true, cleared: true, dir });
  process.exit(0);
}

async function cmdPcHelper([sub, ...rest], flags = {}) {
  if (sub === 'to-me') {
    const body = rest.join(' ');
    const hasContent = body || (flags.images && flags.images.length) || flags.file;
    if (!hasContent) return fail('用法: btalk pc_helper to-me [--image 图]* [--file 文件] [--header h] [--footer f] [--ext json] [文本](电脑助手发消息给登录者自己)', 2);
    const r = await client.request({ cmd: 'notify', body, images: flags.images, file: flags.file, header: flags.header, footer: flags.footer, ext: flags.ext });
    if (!r.ok) return fail(r.error);
    out({ ok: true, ...(r.data || {}) });
    process.exit(0);
  }
  const listeners = require('../pc-helper-listeners');
  const ROBOT = require('../pc-helper').ROBOT.en;
  const pathMod = require('path');
  const fs = require('fs');
  const defaultListener = pathMod.join(__dirname, '..', '..', 'examples', 'echo-listener.js');

  // 隐藏子命令:实际监听进程(由 config 后台 spawn 出来跑此分支,前台常驻)
  if (sub === '_run') {
    await require('../agent-runner').run({ listenerPath: rest[0] || defaultListener });
    return;   // 常驻
  }

  if (sub === 'config') {
    let listenerPath = rest[0];
    // 无参:打印 echo-listener 写法 + 提示,并默认接入它启动
    if (!listenerPath) {
      let sample = ''; try { sample = fs.readFileSync(defaultListener, 'utf8'); } catch {}
      out({ ok: true, hint: '未指定 listener,默认接入 examples/echo-listener.js(回声)。启动后在蜂利器给电脑助手(rbt_pc_helper)发条消息即可收到回声;把 listener 里的 ctx.reply 换成调本机 claude -p / codex / cursor 即成 agent。', sampleListener: defaultListener, sample });
      listenerPath = defaultListener;
    }
    const abs = pathMod.resolve(listenerPath);
    if (!fs.existsSync(abs)) return fail('listener 文件不存在: ' + abs, 2);
    // 防重复:同机器人只允许一个活监听
    const existing = listeners.findByRobot(ROBOT);
    if (existing && !flags.force) return fail(`电脑助手监听已在运行(pid ${existing.pid},listener ${existing.listener})。加 --force 先停旧再起,或先 btalk pc_helper stop。`, 2);
    if (existing) { try { process.kill(existing.pid, 'SIGTERM'); } catch {} listeners.remove(existing.pid); }
    // 后台 spawn _run(detached,stdout/stderr → 日志)
    const { paths } = require('../daemon');
    const dir = paths().dir;
    fs.mkdirSync(dir, { recursive: true });
    const logFile = pathMod.join(dir, 'pc_helper.log');
    const fd = fs.openSync(logFile, 'a');
    const entry = pathMod.join(__dirname, '..', '..', 'bin', 'btalk.js');
    const child = require('child_process').spawn(process.execPath, [entry, 'pc_helper', '_run', abs], { detached: true, stdio: ['ignore', fd, fd], env: process.env });
    child.unref();
    fs.closeSync(fd);
    listeners.add({ pid: child.pid, robot: ROBOT, listener: abs, startedAt: Date.now(), log: logFile });
    out({ ok: true, started: true, pid: child.pid, robot: ROBOT, listener: abs, log: logFile });
    process.exit(0);
  }

  if (sub === 'status') {
    const list = listeners.prune();
    out({
      ok: true, running: list.length,
      listeners: list.map((x) => ({ pid: x.pid, robot: x.robot, listener: x.listener, startedAt: x.startedAt, startedStr: typeof fmt.ts8 === 'function' ? fmt.ts8(x.startedAt) : undefined, log: x.log, alive: true })),
    });
    process.exit(0);
  }

  if (sub === 'stop') {
    const list = listeners.read();
    let stopped = 0;
    for (const x of list) { try { process.kill(x.pid, 'SIGTERM'); stopped++; } catch {} }
    await new Promise((r) => setTimeout(r, 800));   // 等子进程发完下线通知 + 自注销
    listeners.prune();
    out({ ok: true, stopped });
    process.exit(0);
  }

  return fail('用法: btalk pc_helper to-me <内容> | config [listener.js] | status | stop', 2);
}

// JSON 版说明书(help 命令输出)
const HELP = {
  name: '@wnpm/btalk-cli',
  desc: '蜂利器 IM headless CLI(daemon + 瘦客户端;所有子命令 stdout 为一行一个 JSON)',
  commands: {
    login: { usage: 'btalk login', desc: '账密登录(BTALK_USER/BTALK_PASS 环境变量或交互输入)' },
    logout: { usage: 'btalk logout', desc: '退出登录态(清凭据,daemon 保持运行)' },
    status: { usage: 'btalk status', desc: '守护进程状态:未启动/未连接/未登录/连接中/登录中/启动失败/连接失败/登录失败/正常(守护中)' },
    version: { usage: 'btalk version', desc: 'CLI 版本 + node 版本' },
    help: { usage: 'btalk help', desc: '本说明书(JSON)' },
    quit: { usage: 'btalk quit', desc: '守护进程退出' },
    clear: { usage: 'btalk clear', desc: '清理所有本地数据并退出(~/.btalk 全删,再开如同新装)' },
    user: { usage: 'btalk user me|get <uid>|search <关键词> [--mobile]', desc: '用户查询(uid/中文名/工号/职位/部门/领导);手机号属隐私,默认不返回,需显式 --mobile' },
    conversation: { usage: 'btalk conversation list | get <会话id> | search <关键词> | <会话id|中文名|群名> [-n N]', desc: '会话统一入口:list=会话列表 / get=详情 / search=搜会话(联系人+群组) / <id|中文名|群名>=历史消息(群聊自动识别)' },
    'message-search': { usage: 'btalk message-search <关键词> [--conversation-id <会话id>]', desc: '离线全文搜历史消息:默认全局,--conversation-id 限定会话内(借鉴桌面端 offlineSearch)' },
    send: { usage: 'btalk send [--image 图]* [--at uid]* [--at-all] [--header/-footer] [--sign] <对端|中文名> [文本]', desc: '发消息:文本/图/图文混排/@/群聊(群单聊自动识别);--sign 代发签名' },
    group: { usage: 'btalk group create [--name 群名] <成员...> | invite <群id> <成员...> | kick <群id> <成员...> | list | info <群id> | leave <群id>', desc: '群:建群/拉人/踢人/列表/详情/退群(成员支持 uid 或中文名)' },
    'send-file': { usage: 'btalk send-file <对端> <文件>', desc: '发文件(蜂盘,100MB 上限;群单聊自动识别)' },
    watch: { usage: 'btalk watch [--all]', desc: '实时消息;默认智能过滤(忽略机器人/自己,群聊仅@我@所有人),--all 全量' },
    download: { usage: 'btalk download <URL> [-o 路径]', desc: '附件下载(SSO cookie 鉴权)' },
    wsso: { usage: 'btalk wsso [--reset]', desc: 'SSO Cookie(*.bianlifeng.com/*.blibee.com,24h 缓存;超 20h 自动校验失效则重取)' },
    fetch: { usage: 'btalk fetch <url> [-X 方法] [-d 数据] [-H "k: v"]', desc: '用 wsso cookie + 身份 header 访问任意内部接口(GET/POST),返回 status+body' },
    otp: { usage: 'btalk otp', desc: '6 位双因素 OTP' },
    ripple: {
      usage: 'btalk ripple category [--tab 1待处理|2已发起|3我关注|4已处理] | list [--tab 1待处理|2已发起|3我关注|4已处理] [--category <flowCode>] [--keywords <词>] [--page N] | show <工单号> | action <工单号> <操作> [--show-form|--auto|--field k=v]',
      desc: '工单处理(仅本人工单)。标准三步:① list 看待处理工单(默认 tab 1)→ ② show <工单号> 看详情/任务历史/可用操作(ACCEPT领取/APPROVE处理关单/REJECT驳回/FEEDBACK反馈) → ③ action <工单号> <操作> 执行。要点:未领取的多人工单 action 会自动先 ACCEPT 再处理;FEEDBACK 只追加反馈不关单,关单用 APPROVE;--show-form 先看表单字段,--auto 用默认值提交,--field "k=v" 填具体字段',
    },
    pc_helper: { usage: 'btalk pc_helper to-me [--image 图]* [--file 文件] [--header h] [--footer f] [--ext json] [文本] | config [listener.js] [--force] | status | stop', desc: '个人电脑助手(rbt_pc_helper,只私聊本人):to-me=电脑助手发消息给登录者自己(文本/图/多图/图文混排/文件/ext);config=后台启动监听(我从任意端 DM 电脑助手 → 本机 listener 处理 → 回复发回给我;无参默认接 examples/echo-listener.js 并打印写法,换成调 claude -p/codex/cursor 即成 agent);status=查监听实例;stop=停所有监听。上线/下线均 to-me 通知' },
    daemon: { usage: 'btalk daemon start|stop|status|log', desc: '守护进程管理' },
    '(无子命令)': { usage: 'btalk', desc: '进交互 REPL' },
  },
};

module.exports = {
  cmdLogin, cmdLogout, cmdStatus, cmdVersion, cmdHelp, cmdQuit, cmdClear,
  cmdUser, cmdConversation, cmdMessageSearch, cmdGroup, cmdDownload, cmdWsso, cmdFetch, cmdOtp, cmdRipple, cmdSend, cmdSendFile, cmdWatch, cmdDaemon, cmdPcHelper,
  // 向后兼容:格式化助手已迁到 ./format,这里 re-export(test/ 与外部仍从 commands import)
  renderBody: fmt.renderBody, formatTime: fmt.formatTime, localPathOf: fmt.localPathOf, flattenConversations: fmt.flattenConversations,
};
Failed to add the host to the list of known hosts (/home/agent/.ssh/known_hosts).
