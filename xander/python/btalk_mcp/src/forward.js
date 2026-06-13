'use strict';
// 合并转发卡片(msgType 9998 / COMBINE)解码:把 ext.forwardMsgList 还原成可读的子消息列表。
// 纯 JS、零 native(只依赖 zlib + core/bodyParser),可独立单测。
const zlib = require('zlib');
const { raw2Body } = require('./core/bodyParser');
const { MSG_TYPE } = require('./core/const');

// ⚠️ 关键:forwardMsgList = base64 → zlib deflate,但以 Z_SYNC_FLUSH 收尾(尾字节 00 00 ff ff,
// 没有 final block)。严格 inflateSync 必报 `unexpected end of file`(Z_BUF_ERROR),
// 必须带 finishFlush: Z_SYNC_FLUSH 才能容错解出。桌面端/各渲染器丢内容就是栽在这。
const SYNC_FLUSH = { finishFlush: zlib.constants.Z_SYNC_FLUSH };

function inflateTolerant(buf) {
  try { return zlib.inflateSync(buf, SYNC_FLUSH); } catch (_) {
    return zlib.inflateRawSync(buf, SYNC_FLUSH);
  }
}

/**
 * 解开合并转发卡片的 ext.forwardMsgList。
 * @param {string|object} ext 消息的 ext(JSON 串或已解析对象)
 * @returns {{title:string, list:Array}|null} 失败一律返回 null(不抛,避免污染正常消息流)
 *   list 每条已经 raw2Body 成 body 段数组;嵌套 COMBINE 会递归挂到该条的 forwardMsgList。
 */
function decodeForwardList(ext) {
  let e;
  try { e = typeof ext === 'string' ? JSON.parse(ext) : (ext || {}); } catch (_) { return null; }
  const b64 = e && e.forwardMsgList;
  if (!b64 || typeof b64 !== 'string') return null;
  let arr;
  try {
    const out = inflateTolerant(Buffer.from(b64, 'base64'));
    arr = JSON.parse(out.toString('utf8'));
  } catch (_) { return null; }
  if (!Array.isArray(arr)) return null;
  const list = arr.map((item) => {
    const m = raw2Body(item) || item; // obj→段(仅 TEXT/IMAGE/OTHER 生效),其余原样
    if (m && Number(m.msgType) === MSG_TYPE.COMBINE) {
      const nested = decodeForwardList(m.ext);
      if (nested) { m.forwardTitle = nested.title; m.forwardMsgList = nested.list; }
    }
    return m;
  });
  return { title: e.oldInfo || '聊天记录', list };
}

module.exports = { decodeForwardList, inflateTolerant };
