'use strict';
// SSO Cookie 获取:用登录态 sign(=btalk.token)+ deviceId 模拟扫码,polling 取 Set-Cookie。
// 纯 https,无 CDP、无浏览器窗口。CDP 版靠 window.btalk.token 当 sign,daemon 直接有 btalk.token。
const https = require('https');
const { identityHeaders } = require('./headers');

const QUERY_QR = 'https://usercenter-api.blibee.com/usercenter/qr/queryQrCode/v1?widthPt=300&terminal=WEB&syscode=';
const SCAN_QR = 'https://usercenter-api.blibee.com/usercenter/im/qr/scanQrCode/v1';
const POLLING = [
  'https://usercenter-api.blibee.com/usercenter/qr/pollingQrCode/v1',
  'https://usercenter-api.bianlifeng.com/usercenter/qr/pollingQrCode/v1',
];
const CHONGXIAO = 'https://chongxiao.corp.bianlifeng.com/web/Home';

function getJSON(url, headers) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { ...identityHeaders(), ...(headers || {}) }, timeout: 15000 }, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d || '{}')); } catch { resolve({}); } });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('请求超时')); });
  });
}
function getSetCookie(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: identityHeaders(), timeout: 15000 }, (res) => {
      res.on('data', () => {});
      res.on('end', () => {
        const sc = res.headers['set-cookie'] || [];
        resolve(sc.map((c) => c.split(';')[0].trim()).filter(Boolean));
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('请求超时')); });
  });
}

/** {sign, deviceId} → cookie 串。sign 即 btalk.token */
async function getCookie({ sign, deviceId }) {
  if (!sign || !deviceId) throw new Error('wsso 需要 sign(btalk.token)+deviceId');
  const qr = await getJSON(QUERY_QR);
  const token = (qr.data && qr.data.token) || qr.token;
  if (!token) throw new Error('未获取二维码 token');
  const h = { token: sign, deviceid: deviceId };
  await getJSON(`${SCAN_QR}?token=${encodeURIComponent(token)}`, h);
  await getJSON(`${SCAN_QR}?token=${encodeURIComponent(token)}&verify=true`, h);
  const all = [];
  for (const base of POLLING) {
    try { all.push(...await getSetCookie(`${base}?token=${encodeURIComponent(token)}`)); } catch {}
  }
  try { all.push(...await getSetCookie(CHONGXIAO)); } catch {}
  if (!all.length) throw new Error('未获取到 Cookie(可能扫码未完成或登录态失效)');
  return all.join('; ');
}

module.exports = { getCookie };
