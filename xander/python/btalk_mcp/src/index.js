'use strict';
const path = require('path');
const fs = require('fs');
const { getGid, dataDir } = require('./device');
const deviceid = require('./core/lib/deviceid.js');
const pkgJson = require('../package.json');

const ENVS = ['prod', 'beta'];

/**
 * 创建并初始化 Btalk 实例(构造函数内 client 插件会自动跑完 initSDK 全套初始化)。
 * 注意:必须先注入 gid 再 new(plugins/client.js register → initialize → setGid)。
 */
async function createBtalk({ env = process.env.BTALK_ENV || 'prod' } = {}) {
  if (!ENVS.includes(env)) throw new Error(`未知环境: ${env}(可选 ${ENVS.join('/')})`);
  deviceid.init(await getGid());

  const Btalk = require('./core'); // 延迟 require:加载即触发 native addon 装载
  const config = JSON.parse(JSON.stringify(require(`./core/config/${env}.json`)));
  const home = path.join(dataDir(), env);
  fs.mkdirSync(home, { recursive: true });

  const btalk = new Btalk({
    ...config,
    appDataPath: home,
    pkg: {
      version: pkgJson.version,
      'build-config': { env: env === 'prod' ? 'prod' : 'beta', vid: 99990001, pid: 'btalk_headless' },
    },
  });
  // 对齐桌面端 window.btalk / window.device 语义:
  // core 里 fileModule/upload|download 有裸 btalk / global.device 引用
  global.btalk = btalk;
  global.device = deviceid.device;
  return btalk;
}

function credFile() { return path.join(dataDir(), 'credentials.json'); }

/** 账密登录(成功后持久化 token),或免参用已保存凭据登录。 */
async function login(btalk, { username, password } = {}) {
  if (username && password) {
    const pack = await btalk.login({ username, password });
    fs.writeFileSync(credFile(),
      JSON.stringify({ token: pack.userCenterToken, uid: pack.user, cnName: pack.cnName }),
      { mode: 0o600 });
    return pack;
  }
  let saved;
  try {
    saved = JSON.parse(fs.readFileSync(credFile(), 'utf8'));
  } catch {
    throw new Error('没有已保存的登录凭据,请先 btalk login');
  }
  return btalk.login({ token: saved.token, uid: saved.uid, cnName: saved.cnName });
}

/** 等 XMPP 真正连上(login() resolve 只代表发起)。冷启动 + 网络抖动给 45s 容差。 */
function waitConnected(btalk, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('连接 XMPP 超时')), timeoutMs);
    t.unref();
    btalk.on('network-change', (state) => {
      if (state) { clearTimeout(t); resolve(); }
    });
  });
}

const { sendSms, smsLogin } = require('./sms');

/** 短信验证码登录:发码 → 让 askCode 取用户输入 → 调 usercenter mobile/login → btalk.login(token 分支) */
async function smsLoginFlow(btalk, { mobilePhone, askCode }) {
  await sendSms.call(btalk, mobilePhone, undefined, () => deviceid.get());
  const code = await askCode(`已向 ${mobilePhone} 发送验证码,请输入: `);
  const data = await smsLogin.call(btalk, mobilePhone, code, undefined, () => deviceid.get());
  const pack = await btalk.login({ token: data.token, uid: data.uid, cnName: data.cnName });
  fs.writeFileSync(credFile(),
    JSON.stringify({ token: data.token, uid: pack.user, cnName: pack.cnName }),
    { mode: 0o600 });
  return pack;
}

/**
 * 二维码扫码登录(纯终端 / SSH 友好,不依赖任何 GUI):
 *   1. 调 generateQrcode 获取 base64 PNG
 *   2. 解码 PNG → 渲染终端 ASCII QR(默认半块字符)
 *   3. SDK 每 60s 自动刷新二维码,onShow 在每次新二维码时被调一次
 *   4. SDK 轮询服务端,扫码确认后触发 onSuccess(data) 拿到 {token, uid, cnName}
 *   5. 调 btalk.login(token 分支)持久化凭据
 */
async function qrcodeLoginFlow(btalk, { onShow }) {
  let lastBase64 = null;
  const handle = await new Promise((resolve, reject) => {
    let resolved = false;
    const qr = btalk.generateQrcode({
      width: 300,
      listener: (scan) => {
        if (scan.base64 && scan.base64 !== lastBase64) {
          lastBase64 = scan.base64;
          let ascii = '';
          try { ascii = require('./qr').renderQrToAscii(scan.base64); }
          catch (e) { /* 渲染失败不致命,下一轮 60s 后会刷新 */ }
          if (typeof onShow === 'function') onShow(ascii);
        }
        if (scan.status === 'NETWORK_ERROR' || scan.status === 'GID_ERROR') {
          if (!resolved) { resolved = true; reject(new Error(`二维码状态异常: ${scan.status}`)); }
        }
      },
      onSuccess: (data) => {
        if (resolved) return;
        resolved = true;
        try { qr && qr.distory && qr.distory(); } catch {}
        resolve(data);
      },
      onRequestQRError: (e) => {
        if (!resolved) { resolved = true; reject(new Error(`获取二维码失败: ${e.message}`)); }
      },
      onCheckQRStatusError: () => { /* 轮询失败不致命,SDK 自己重试 */ },
    });
  });
  // handle = {token, uid, cnName, ...}
  const pack = await btalk.login({ token: handle.token, uid: handle.uid, cnName: handle.cnName });
  fs.writeFileSync(credFile(),
    JSON.stringify({ token: handle.token, uid: pack.user, cnName: pack.cnName }),
    { mode: 0o600 });
  return pack;
}

module.exports = { createBtalk, login, smsLoginFlow, qrcodeLoginFlow, waitConnected, credFile };
