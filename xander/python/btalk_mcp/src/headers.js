'use strict';
// 统一的身份标识 header。所有出站 HTTP 请求带上,便于服务端按设备/用户识别与审计。
// 取值来自进程内全局:global.device.gid(createBtalk 注入)、global.btalk.sender(登录后)。
const os = require('os');

const PID = process.env.BTALK_PID || '41022'; // btalk-headless 产品号

let _ua;
function userAgent() {
  if (_ua) return _ua;
  let uname = '';
  try { uname = require('child_process').execSync('uname -a', { timeout: 1000, encoding: 'utf8' }).trim(); }
  catch { uname = `${os.platform()} ${os.release()}`; }
  _ua = `btalk-headless/${uname}`;
  return _ua;
}

/** 构造身份 header 对象。未登录时省略 http_ws_user_id。 */
function identityHeaders() {
  const gid = (global.device && global.device.gid) || '';
  const uid = (global.btalk && global.btalk.sender) || '';
  const h = {
    http_deviceid: `udid=${gid}`,
    http_ws_gid: `udid=${gid}`,
    http_ws_pid: PID,
    http_user_agent: userAgent(),
  };
  if (uid) h.http_ws_user_id = uid;
  return h;
}

module.exports = { identityHeaders, PID };
