'use strict';
// btalkd:守护进程,唯一持有 SDK 实例(连一次、init 一次、独占 ~/.btalk),
// 通过 Unix socket 对外服务。瘦客户端(src/cli/client.js)发请求、订阅消息。
//
// 为什么:每条 CLI 命令各起一个进程重连+重init+退出,导致噪音/BTSearch崩/退出段错误/DB锁。
// daemon 模型一次性根治:单进程串行访问、持久连接、客户端不碰 native addon。
const net = require('net');
const fs = require('fs');
const path = require('path');
const { createBtalk, login, qrcodeLoginFlow, waitConnected, credFile } = require('./index');
const { dataDir, reportDeviceInfo } = require('./device');

function paths() {
  const dir = dataDir();
  return { dir, sock: path.join(dir, 'btalkd.sock'), pid: path.join(dir, 'btalkd.pid'), log: path.join(dir, 'btalkd.log') };
}

/** 读图片尺寸(内联解析 PNG/JPEG 头,无外部依赖);失败回退 200x200 */
function imageSize(file) {
  try {
    const b = fs.readFileSync(file);
    if (b.length >= 24 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) {
      return { width: b.readUInt32BE(16), height: b.readUInt32BE(20) };
    }
    if (b[0] === 0xff && b[1] === 0xd8) {
      for (let i = 2; i < b.length - 9;) {
        if (b[i] !== 0xff) { i++; continue; }
        const m = b[i + 1];
        const len = b.readUInt16BE(i + 2);
        if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) {
          return { height: b.readUInt16BE(i + 5), width: b.readUInt16BE(i + 7) };
        }
        i += 2 + len;
      }
    }
  } catch {}
  return { width: 200, height: 200 };
}

async function main() {
  const P = paths();
  fs.mkdirSync(P.dir, { recursive: true });
  const flog = (m) => { try { fs.appendFileSync(P.log, `${new Date().toISOString()} ${m}\n`); } catch {} };

  flog('DAEMON_BOOT pid=' + process.pid);
  const pkg = require('../package.json');
  // 生命周期状态(status 命令读它):starting→connecting→logging_in→ready / no_creds / *_failed
  let state = 'starting';
  let btalk = null;
  let bootError = null;
  let loggedIn = false;
  const subs = new Set();   // watch 订阅者

  // 给消息 body 的 image/file/url 段补完整 URL(base 懒取,boot 期间 btalk 可能还没就绪)
  const COMBINE = require('./core/const').MSG_TYPE.COMBINE; // 9998 合并转发卡片
  const enrich = (m) => {
    const base = String((btalk && btalk.config && (btalk.config.fezzHost || btalk.config.diskHost)) || '').replace(/\/$/, '');
    const fullUrl = (v) => (!v ? '' : (/^https?:\/\//.test(v) ? v : base + '/' + String(v).replace(/^\//, '')));
    const enrichSegs = (body) => {
      if (!Array.isArray(body)) return;
      for (const seg of body) {
        if (seg && seg.value && (seg.type === 'image' || seg.type === 'file' || seg.type === 'url')) {
          seg.url = seg.type === 'url' ? seg.value : fullUrl(seg.value);
        }
      }
    };
    if (m) enrichSegs(m.body);
    // 合并转发卡片:解开 ext.forwardMsgList(daemon 才有 fezzHost),并给转发里的图片/文件段补 url
    if (m && Number(m.msgType) === COMBINE && !Array.isArray(m.forwardMsgList)) {
      const f = require('./forward').decodeForwardList(m.ext);
      if (f) {
        m.forwardTitle = f.title;
        const walk = (list) => {
          for (const it of (list || [])) {
            enrichSegs(it && it.body);
            if (it && Array.isArray(it.forwardMsgList)) walk(it.forwardMsgList);
          }
        };
        walk(f.list);
        m.forwardMsgList = f.list;
      }
    }
    return m;
  };

  // SSO Cookie:用 btalk.token(sign)+ deviceId 模拟扫码取得,24h 文件缓存。download/ripple/fetch 复用。
  let ssoCookieCached = false;
  // 探针:拿一个轻量内部接口校验 cookie 是否还有效(失效宁可误判,reset 安全)
  async function validateSsoCookie(cookie) {
    try {
      const url = 'https://ripple.blibee.com/ripple/feedback/user/flow/order/auto/rule/check/auth/v1?terminal=PC';
      const resp = await fetch(url, { headers: { ...require('./headers').identityHeaders(), accept: '*/*', Cookie: cookie, Referer: 'https://ripple.blibee.com/ripple/pc/', higher_priority_type: 'inner' } });
      if (resp.status !== 200) return false;
      const j = await resp.json().catch(() => null);
      return !!(j && j.status === 0);
    } catch { return false; }
  }
  async function fetchFreshCookie() {
    const cf = path.join(P.dir, 'sso_cookie.json');
    const deviceId = require('./core/lib/deviceid').get();
    const cookie = await require('./wsso').getCookie({ sign: btalk.token, deviceId });
    try { fs.writeFileSync(cf, JSON.stringify({ cookie, ts: Date.now() }), { mode: 0o600 }); } catch {}
    ssoCookieCached = false;
    return cookie;
  }
  async function getSsoCookie(reset) {
    if (reset) return fetchFreshCookie();
    const cf = path.join(P.dir, 'sso_cookie.json');
    try {
      const c = JSON.parse(fs.readFileSync(cf, 'utf8'));
      const age = Date.now() - c.ts;
      if (c.cookie && age < 24 * 3600 * 1000) {
        // 超过 20h 的 cookie 接近过期:先探测有效性,失效则立即重取
        if (age > 20 * 3600 * 1000) {
          if (await validateSsoCookie(c.cookie)) { ssoCookieCached = true; return c.cookie; }
          flog('SSO_STALE age=' + Math.round(age / 3600000) + 'h 失效,重新获取');
          return fetchFreshCookie();
        }
        ssoCookieCached = true;
        return c.cookie;
      }
    } catch {}
    return fetchFreshCookie();
  }

  // 判定单聊/群聊:用 getConversationById 的 conversationType(===1 为群);
  // 查不到(未建会话的陌生 id)再用形态兜底(32 位 hex = 群 id)。无需用户指定。
  async function detectChatType(to) {
    try {
      const c = await btalk.getConversationById(String(to));
      if (c && c.conversationType === 1) return 'groupchat';
      if (c) return 'chat';
    } catch {}
    return require('./cli/format').isGroupId(to) ? 'groupchat' : 'chat';
  }

  // 离线全文搜索(借鉴桌面端 offlineSearchHub):native 异步回调,按 sequence 路由收集结果。
  // 全局 api.offlineSearch(seq, categories, query);会话内 api.offlineSearchInConversation(seq, convId, query);
  // 结果走 api.onOfflineSearch(seq, category, result) 回调。category: 1=USER 2=GROUP 3=MESSAGE 4=CONVERSATION
  let offlineSearchReady = false;
  let searchSeq = 1;
  const pendingSearch = new Map();
  function ensureOfflineSearch() {
    if (offlineSearchReady) return true;
    if (!btalk || !btalk.api || typeof btalk.api.onOfflineSearch !== 'function') return false;
    btalk.api.onOfflineSearch((sequence, category, result) => {
      const p = pendingSearch.get(sequence);
      if (!p) return;
      let parsed; try { parsed = JSON.parse(result); } catch { parsed = null; }
      if (parsed != null) p.results.push({ category, data: parsed });
      clearTimeout(p.timer);   // debounce:收到结果后等 400ms 看还有没有后续批次,再 resolve
      p.timer = setTimeout(() => { pendingSearch.delete(sequence); p.resolve(p.results); }, 400);
    });
    offlineSearchReady = true;
    return true;
  }
  function offlineSearchMessages({ keyword, conversationId }) {
    if (!ensureOfflineSearch() || typeof btalk.api.offlineSearch !== 'function') {
      return Promise.reject(new Error('native addon 不支持离线搜索(offlineSearch 缺失)'));
    }
    const seq = searchSeq++;
    return new Promise((resolve) => {
      const p = { results: [], resolve, timer: setTimeout(() => { pendingSearch.delete(seq); resolve([]); }, 6000) };
      pendingSearch.set(seq, p);
      try {
        if (conversationId) btalk.api.offlineSearchInConversation(seq, String(conversationId), String(keyword));
        else btalk.api.offlineSearch(seq, JSON.stringify([3]), String(keyword));   // 3 = MESSAGE
      } catch (e) { clearTimeout(p.timer); pendingSearch.delete(seq); resolve([]); }
    });
  }

  // 启动序列(socket 已提前 listen,故 boot 期间 status/help/version 可答;需 btalk/登录的命令 await bootReady)
  const bootReady = (async () => {
    state = 'connecting';
    // Node 版本自检:native addon 是 ABI 127(Node 22),版本不对会加载崩,提前给清晰错误
    if (process.versions.modules !== '127') {
      throw new Error(`Node 版本不对:当前 Node ${process.versions.node}(ABI ${process.versions.modules}),daemon 需 Node 22(ABI 127)。请 nvm use 切到 Node 22 后重启 daemon。`);
    }
    btalk = await createBtalk();
    btalk.on('message', (msgs) => {
      const arr = [].concat(msgs || []);
      if (!subs.size) return;
      for (const m of arr) {
        const line = JSON.stringify({ event: 'message', msg: enrich(m) }) + '\n';
        for (const s of subs) { try { s.write(line); } catch {} }
      }
    });
    // 每次启动带最新设备信息上报 /app/gid(资产盘点;best-effort)
    reportDeviceInfo().then((g) => flog('DEVICE_REPORT ' + (g ? 'ok' : 'skip'))).catch(() => {});
    if (fs.existsSync(credFile())) {
      try {
        state = 'logging_in';
        await login(btalk);
        await waitConnected(btalk);
        loggedIn = true; state = 'ready'; flog('AUTO_LOGIN_OK');
      } catch (e) {
        state = /xmpp|连接|timeout|超时/i.test((e && e.message) || '') ? 'connect_failed' : 'login_failed';
        flog('AUTO_LOGIN_FAIL ' + (e && e.message));
      }
    } else {
      state = 'no_creds'; flog('NO_CREDS 等待 login 命令');
    }
  })();
  bootReady.catch((e) => { state = 'start_failed'; bootError = (e && e.message) || String(e); flog('START_FAIL ' + bootError); });

  try { fs.unlinkSync(P.sock); } catch {}
  const server = net.createServer((sock) => {
    let buf = '';
    sock.on('data', (d) => {
      buf += d;
      let i;
      while ((i = buf.indexOf('\n')) >= 0) { const line = buf.slice(0, i); buf = buf.slice(i + 1); if (line.trim()) handle(line, sock); }
    });
    sock.on('error', () => {});
    sock.on('close', () => subs.delete(sock));
  });

  async function handle(line, sock) {
    let req; try { req = JSON.parse(line); } catch { return; }
    const id = req.id;
    const reply = (o) => { try { sock.write(JSON.stringify({ id, ...o }) + '\n'); } catch {} };
    const NEEDS_LOGIN = ['sessions', 'send', 'send-file', 'history', 'subscribe', 'user', 'resolve', 'get-conv', 'download', 'wsso', 'otp', 'group', 'fetch', 'message-search', 'notify'];
    try {
      // 需要 btalk 实例或登录态的命令,先等启动序列尘埃落定(boot 期间发的命令不会误判未登录)
      if (NEEDS_LOGIN.includes(req.cmd) || req.cmd === 'login' || req.cmd === 'logout') {
        try { await bootReady; } catch {}
        if (!btalk && req.cmd !== 'logout') {
          const hint = /abi|version|modules|self-register|node\.node|ELF|prebuild/i.test(bootError || '') ? ' —— 多半是 Node 版本不对,请 nvm use 切到 Node 22 后重启 daemon(btalk quit 再重试)' : '';
          return reply({ ok: false, error: `daemon 启动失败(${state}): ${bootError || '未知'}${hint}` });
        }
      }
      if (NEEDS_LOGIN.includes(req.cmd) && !loggedIn) {
        return reply({ ok: false, error: '未登录,请先 btalk login' });
      }
      switch (req.cmd) {
        case 'ping':
          reply({ ok: true, data: { version: pkg.version, pid: process.pid, loggedIn, uid: (loggedIn && btalk && btalk.sender) || null } });
          break;
        case 'state':
          reply({ ok: true, data: { state, loggedIn, uid: (loggedIn && btalk && btalk.sender) || null, version: pkg.version, pid: process.pid } });
          break;
        case 'logout': {
          try { if (btalk && typeof btalk.logout === 'function') await btalk.logout(); } catch {}
          try { fs.unlinkSync(credFile()); } catch {}
          loggedIn = false; state = 'no_creds';
          flog('LOGOUT');
          reply({ ok: true, data: { loggedIn: false } });
          break;
        }
        case 'login': {
          state = 'logging_in';
          let pack;
          if (req.qrcode) {
            // 扫码登录:二维码 ASCII 流式推给客户端显示,等用户手机端扫码确认
            pack = await qrcodeLoginFlow(btalk, {
              onShow: (ascii) => { try { sock.write(JSON.stringify({ event: 'qrcode', ascii }) + '\n'); } catch {} },
            });
          } else {
            pack = await login(btalk, { username: req.username, password: req.password });
          }
          await waitConnected(btalk);
          loggedIn = true; state = 'ready';
          flog('LOGIN_OK ' + pack.user);
          reply({ ok: true, data: { user: pack.user, cnName: pack.cnName } });
          break;
        }
        case 'user': {
          // 手机号属隐私:getUserInfo 默认不含(桌面端也是「点击查看」+ 审计埋点),
          // 仅当显式 req.mobile 时才单独拉(对齐桌面端 viewPhone 的显式意图)。
          const withMobile = async (info, uid) => {
            if (req.mobile && info && !info.mobile && uid) {
              try { const m = await btalk.getUserMobile(uid); if (m) info.mobile = typeof m === 'string' ? m : (m.mobile || m.phone || ''); } catch {}
            }
            return info;
          };
          // 陌生用户首次查 getUserInfo 只返回本地部分缓存(同时异步从服务端拉全);
          // 关键字段(empno)缺失则轮询重查,直到 SDK 填充或超时(~1.6s)
          const fetchFull = async (uid) => {
            let info = await btalk.getUserInfo(uid);
            for (let i = 0; i < 4 && info && (info.empno == null || info.empno === ''); i++) {
              await new Promise((r) => setTimeout(r, 400));
              info = await btalk.getUserInfo(uid);
            }
            return info;
          };
          if (req.sub === 'me') {
            const info = await withMobile(await fetchFull(btalk.sender || ''), btalk.sender);
            reply({ ok: true, data: info });
          } else if (req.sub === 'get') {
            const uid = String(req.uid || '');
            const info = await withMobile(await fetchFull(uid), uid);
            reply({ ok: true, data: info });
          } else if (req.sub === 'search') {
            const r = await btalk.searchAllByKeyword(String(req.keyword || ''));
            const users = ((r && r.users && r.users.data) || []).slice(0, 30);
            reply({ ok: true, data: { users } });
          } else {
            reply({ ok: false, error: 'user 子命令: me|get <uid>|search <关键词>' });
          }
          break;
        }
        case 'resolve': {
          const r = await btalk.searchAllByKeyword(String(req.keyword || ''));
          const users = ((r && r.users && r.users.data) || []);
          const groups = ((r && r.groups && r.groups.data) || []);
          reply({ ok: true, data: { users, groups } });
          break;
        }
        case 'group': {
          const ids = [].concat(req.ids || []);
          if (req.sub === 'create') {
            if (!ids.length) { reply({ ok: false, error: '建群需要至少一个初始成员' }); break; }
            const ret = await btalk.createGroup({ name: req.name || '', ids, desc: req.desc || '' });
            reply({ ok: true, data: ret });
          } else if (req.sub === 'invite') {
            const ret = await btalk.addMembers(String(req.groupId || ''), ids);
            reply({ ok: true, data: ret });
          } else if (req.sub === 'kick') {
            const ret = await btalk.removeMembers(String(req.groupId || ''), ids);
            reply({ ok: true, data: ret });
          } else if (req.sub === 'list') {
            const ret = await btalk.getJoinedGroups();
            reply({ ok: true, data: ret });
          } else if (req.sub === 'info') {
            const ret = await btalk.fetchGroupInfo(String(req.groupId || ''));
            reply({ ok: true, data: ret });
          } else if (req.sub === 'leave') {
            const ret = await btalk.leaveGroup(String(req.groupId || ''));
            reply({ ok: true, data: ret });
          } else {
            reply({ ok: false, error: 'group 子命令: create|invite|kick|list|info|leave' });
          }
          break;
        }
        case 'get-conv': {
          const conv = await btalk.getConversationById(String(req.id || ''));
          reply({ ok: true, data: conv });
          break;
        }
        case 'message-search': {
          const results = await offlineSearchMessages({ keyword: req.keyword, conversationId: req.conversationId });
          let messages = [];
          for (const x of results) if (Array.isArray(x.data)) messages = messages.concat(x.data);
          if (req.conversationId) messages = messages.filter((m) => (m.conversationID || m.toID) === String(req.conversationId));
          reply({ ok: true, data: { messages } });
          break;
        }
        case 'wsso': {
          const cookie = await getSsoCookie(!!req.reset);
          reply({ ok: true, data: { cookie, cached: ssoCookieCached } });
          break;
        }
        case 'fetch': {
          const url = String(req.url || '');
          if (!/^https?:\/\//.test(url)) { reply({ ok: false, error: 'fetch 需要完整 http(s) URL' }); break; }
          const cookie = await getSsoCookie(false);
          const method = String(req.method || (req.data ? 'POST' : 'GET')).toUpperCase();
          const headers = { ...require('./headers').identityHeaders(), accept: '*/*', Cookie: cookie, higher_priority_type: 'inner', ...(req.headers || {}) };
          if (!headers.Referer && !headers.referer) { try { headers.Referer = new URL(url).origin + '/'; } catch {} }
          if (req.data && !headers['content-type'] && !headers['Content-Type']) headers['content-type'] = 'application/json;charset=UTF-8';
          const resp = await fetch(url, { method, headers, body: req.data != null ? req.data : undefined });
          const text = await resp.text();
          let body; try { body = JSON.parse(text); } catch { body = text; }
          reply({ ok: true, data: { status: resp.status, contentType: resp.headers.get('content-type'), body } });
          break;
        }
        case 'otp': {
          const deviceId = require('./core/lib/deviceid').get();
          const code = await require('./otp').genOtp({ username: btalk.sender, deviceId, sign: btalk.token });
          reply({ ok: true, data: { otp: code } });
          break;
        }
        case 'download': {
          const url = String(req.url || '');
          if (!/^https?:\/\//.test(url)) { reply({ ok: false, error: 'download 需要完整 http(s) URL' }); break; }
          const cookie = await getSsoCookie(false);
          const resp = await fetch(url, { headers: { ...require('./headers').identityHeaders(), Cookie: cookie, Referer: 'https://btalk.blibee.com/', 'Accept-Encoding': 'identity', 'User-Agent': 'btalk-cli' } });
          if (!resp.ok) { reply({ ok: false, error: `下载失败 HTTP ${resp.status}` }); break; }
          const buf = Buffer.from(await resp.arrayBuffer());
          const outPath = req.output || path.join(P.dir, 'downloads', String(req.name || (url.split('/').pop().split('?')[0]) || 'file.bin'));
          fs.mkdirSync(path.dirname(outPath), { recursive: true });
          fs.writeFileSync(outPath, buf);
          reply({ ok: true, data: { path: outPath, bytes: buf.length, contentType: resp.headers.get('content-type') } });
          break;
        }
        case 'notify': {
          // pc_helper:以电脑助手(rbt_pc_helper)身份发消息给登录者自己。支持 文本/图/图文混排/文件/ext。cookie 不出 daemon。
          const to = req.to || (btalk && btalk.sender);
          if (!to) { reply({ ok: false, error: '未登录,拿不到登录者 uid' }); break; }
          const pc = require('./pc-helper');
          const cookie = await getSsoCookie(false);
          const text = req.body != null ? String(req.body) : '';
          const header = req.header ? String(req.header) : '';
          const footer = req.footer ? String(req.footer) : '';
          const images = [].concat(req.images || []).filter(Boolean);
          const file = req.file ? String(req.file) : '';

          // 文件:蜂盘上传 → 原生 Msg_Type=5 优先,被拒/失败降级为下载链接
          if (file) {
            const u = await require('./files').uploadToDisk(btalk, { toID: to, filePath: file, chatType: 'chat' });
            try {
              const r = await pc.sendAsRobot({ to, body: u.fileBody, cookie, msgType: '5', ext: u.ext });
              reply({ ok: true, data: { to, robot: pc.ROBOT.en, ret: !!r.ret, kind: 'file', name: u.name } });
            } catch (e5) {
              const linkBody = (header ? header + '\n' : '') + `📎 ${u.name}\n` + pc.buildUrlObj(u.url);
              const r = await pc.sendAsRobot({ to, body: linkBody, cookie, msgType: '1' });
              reply({ ok: true, data: { to, robot: pc.ROBOT.en, ret: !!r.ret, kind: 'file-link', name: u.name, degraded: true, error5: e5.message } });
            }
            break;
          }

          // 图片 / 图文混排
          let body; let msgType = '1';
          if (images.length) {
            const objs = [];
            for (const img of images) {
              if (/^https?:\/\//.test(img)) objs.push(pc.buildImgObj(img, 200, 200));
              else { const up = await pc.uploadImage(img); objs.push(pc.buildImgObj(up.path, up.width, up.height)); }
            }
            const hasText = !!(text || header || footer);
            if (!hasText && objs.length === 1) { body = objs[0]; msgType = '3'; }   // 纯单图
            else {
              const parts = [];
              if (header) parts.push(header);
              parts.push(...objs);
              const foot = footer || text;
              if (foot) parts.push(foot);
              body = parts.join('\n'); msgType = '1';
            }
          } else {
            body = (header ? header + '\n' : '') + text + (footer ? '\n' + footer : '');
          }
          if (!body) { reply({ ok: false, error: 'notify 需要正文/图/文件之一' }); break; }
          const r = await pc.sendAsRobot({ to, body, cookie, msgType, ext: req.ext });
          reply({ ok: true, data: { to, robot: pc.ROBOT.en, ret: !!r.ret, msgType } });
          break;
        }
        case 'sessions': {
          const raw = await btalk.getAllConversationsFromNewSDK();
          reply({ ok: true, data: typeof raw === 'string' ? JSON.parse(raw) : raw });
          break;
        }
        case 'send': {
          const to = String(req.to).toLowerCase();
          const chatType = await detectChatType(to);
          // 纯文本快路径(向后兼容旧客户端)
          if (!req.images && !req.atUserList && !req.header && !req.footer) {
            let text = req.body || '';
            if (req.sign) text = require('./cli/format').signatureFor(text, { self: btalk.sender, to });
            await btalk.commit({ toID: to, body: text, chatType });
            reply({ ok: true });
            break;
          }
          // 结构化:header + 多图 + footer,@ 进 ext
          const Btalk = btalk.constructor;
          const segs = [];
          if (req.header) segs.push({ type: 'text', value: req.header + '\n' });
          for (const img of (req.images || [])) {
            const dim = imageSize(img);
            const { promise } = await Btalk.upload({ type: 'img', file: img });
            const up = await promise;
            segs.push({ type: 'image', value: up.url, width: dim.width, height: dim.height });
          }
          let footer = req.footer || req.body || '';
          if (req.sign) footer = require('./cli/format').signatureFor(footer, { self: btalk.sender, to });
          if (footer) segs.push({ type: 'text', value: (segs.length ? '\n' : '') + footer });
          // @ 用户:作为文本段的 atUser(body2Raw 收集进 ext.atUserList);@所有人标识是大写 ALL(对齐桌面端)
          const ats = [].concat(req.atUserList || []).map((u) => (String(u).toLowerCase() === 'all' ? 'ALL' : u));
          if (ats.length) {
            const atText = ats.map((u) => (u === 'ALL' ? '@所有人 ' : '@' + u + ' ')).join('');
            segs.unshift({ type: 'text', value: atText, atUser: ats[0] });
            await btalk.commit({ toID: to, body: segs, chatType, ext: JSON.stringify({ atUserList: ats }) });
          } else {
            await btalk.commit({ toID: to, body: segs, chatType });
          }
          reply({ ok: true });
          break;
        }
        case 'send-file': {
          const chatType = await detectChatType(String(req.to).toLowerCase());
          const ret = await require('./files').sendFile(btalk, { toID: req.to, filePath: req.filePath, chatType });
          reply({ ok: true, data: ret });
          break;
        }
        case 'history': {
          const isGroupChat = (await detectChatType(String(req.target).toLowerCase())) === 'groupchat';
          // 翻页:before=上页最早消息 time → 拉更早。native direction=0 是从 startTime 往旧拉(≤),
          // 故 startTime=before-1 即拉 < before 的最近一批(更早一页);不传 before 则默认拉最近。
          const before = Number(req.before) || 0;
          const raw = await btalk.fetchHistoryMessages({
            conversationId: String(req.target).toLowerCase(), pageSize: req.count || 20, isGroupChat,
            startMessageTimestamp: before ? before - 1 : undefined,
          });
          const data = typeof raw === 'string' ? JSON.parse(raw) : (raw || {});
          if (Array.isArray(data.message)) data.message.forEach(enrich);
          reply({ ok: true, data });
          break;
        }
        case 'subscribe':
          subs.add(sock);
          reply({ ok: true, data: { subscribed: true } });
          break;
        case 'stop':
          reply({ ok: true });
          cleanup(); process.exit(0);
          break;
        default:
          reply({ ok: false, error: 'unknown cmd: ' + req.cmd });
      }
    } catch (e) { reply({ ok: false, error: (e && e.message) || String(e) }); }
  }

  function cleanup() { try { fs.unlinkSync(P.sock); } catch {} try { fs.unlinkSync(P.pid); } catch {} }
  server.on('error', (e) => { flog('SERVER_ERR ' + e.message); process.exit(1); });
  server.listen(P.sock, () => { fs.writeFileSync(P.pid, String(process.pid)); flog('DAEMON_READY sock=' + P.sock); });
  for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => { cleanup(); process.exit(0); });
}

if (require.main === module || process.env.BTALKD_RUN) {
  main().catch((e) => { try { fs.appendFileSync(paths().log, `DAEMON_FAIL ${(e && e.message) || e}\n`); } catch {}; process.exit(1); });
}
module.exports = { main, paths };
