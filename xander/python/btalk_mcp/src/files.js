'use strict';
const fs = require('fs');
const path = require('path');

const MAX_BYTES = 100 * 1024 * 1024; // 100MB(与 btalk-chat 对齐)
const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']);

function formatSize(bytes) {
  return `${(bytes / 1024).toFixed(2)}KB`;
}

function buildFileBody({ md5, name, sizeBytes, url }) {
  return JSON.stringify({
    FILEID: md5,
    FILEMD5: md5,
    FileSize: formatSize(sizeBytes),
    FileName: name,
    HttpUrl: url,
  });
}

/** 校验文件,返回 { name, sizeBytes, isImage }。 */
function checkFile(filePath) {
  const stat = fs.statSync(filePath);
  if (stat.isDirectory()) throw new Error(`不是文件: ${filePath}`);
  if (stat.size > MAX_BYTES) throw new Error(`文件超过 100MB 限制: ${filePath}`);
  return {
    name: path.basename(filePath),
    sizeBytes: stat.size,
    isImage: IMAGE_EXTS.has(path.extname(filePath).toLowerCase()),
  };
}

/**
 * 上传 + 发送文件消息(对齐 ai_helper btalk-chat 的单一蜂盘路径)。
 * 不区分图片/文件——服务端按 FileName 后缀展示;后续若要单独走图片消息,
 * 在 isImage 分支里改 msgType=3 + 不同 body 结构(M3 议题)。
 */
/**
 * 上传文件到蜂盘并返回元信息(不发送)。供 sendFile(SDK 发)与 pc_helper notify(机器人代发)共用。
 * @returns {Promise<{url,md5,name,sizeBytes,uploadToken,fileBody,ext}>}
 */
async function uploadToDisk(btalk, { toID, filePath, chatType = 'chat' }) {
  const info = checkFile(filePath);
  const Btalk = btalk.constructor;
  const toLower = String(toID).toLowerCase();
  // 蜂盘上传需要先从 disk server 拿 uploadToken(对齐 ai_helper btalk-chat 的双步 check+key)
  const uploadToken = await fetchUploadToken(btalk, { toID: toLower, chatType, filePath, info });
  // Btalk.upload 是 Btalk 类的静态属性(见 src/core/index.js 尾部 attach)
  const { promise } = await Btalk.upload.uploadByDisk({ type: 'file', file: filePath, uploadToken });
  const { url, md5 } = await promise;
  const fileBody = buildFileBody({ md5, name: info.name, sizeBytes: info.sizeBytes, url });
  const ext = uploadToken ? JSON.stringify({ fileUploadInfo: { uploadToDiskToken: uploadToken } }) : '';
  return { url, md5, name: info.name, sizeBytes: info.sizeBytes, uploadToken, fileBody, ext };
}

async function sendFile(btalk, { toID, filePath, chatType = 'chat' }) {
  const u = await uploadToDisk(btalk, { toID, filePath, chatType });
  return btalk.commitDiskFileMsg({
    toID: String(toID).toLowerCase(),
    chatType,
    body: u.fileBody,
    msgType: 5,
    ext: u.ext,
  });
}

/** 调蜂盘双步接口拿 uploadToken(对齐 ai_helper btalk-chat 实现)。 */
async function fetchUploadToken(btalk, { toID, chatType, filePath, info }) {
  const diskHost = btalk.config && btalk.config.diskHost;
  if (!diskHost) throw new Error('config.diskHost 缺失,无法走蜂盘上传');
  const msgFromUid = (btalk.curUserConfig && btalk.curUserConfig.uid) || btalk.sender;
  const md5sum = await md5OfFile(filePath);
  const fileItem = {
    eventId: 'send-file-' + Date.now(),
    filePath, fileExt: require('path').extname(info.name).replace(/^\./, ''),
    fileName: info.name, md5: md5sum,
    convType: chatType, convId: toID, msgFromUid, msgTime: Date.now(),
  };
  const headers = {
    ...require('./headers').identityHeaders(),
    'Content-Type': 'application/json',
    token: btalk.token,
    deviceId: require('./core/lib/deviceid.js').get(),
  };

  // 1. check 接口
  const checkRes = await fetch(`${diskHost}/btalk/disk/core/im/file/upload/check/v1`, {
    method: 'POST', headers, body: JSON.stringify({ fileList: [fileItem] }),
  }).then((r) => r.json()).catch(() => null);
  if (checkRes && checkRes.status === 0 && checkRes.data && checkRes.data.canUpload && checkRes.data.token) {
    return checkRes.data.token;
  }

  // 2. key 接口(check 没直接给 token 时)
  const keyRes = await fetch(`${diskHost}/btalk/disk/core/im/file/upload/key/v1`, {
    method: 'POST', headers,
    body: JSON.stringify({ params: { convInfo: { convId: toID, convType: chatType }, fileList: [fileItem] } }),
  }).then((r) => r.json()).catch(() => null);
  if (keyRes && keyRes.status === 0 && keyRes.data) return keyRes.data;

  throw new Error(`获取蜂盘上传 token 失败: ${JSON.stringify(keyRes || checkRes)}`);
}

function md5OfFile(filePath) {
  return new Promise((resolve, reject) => {
    require('md5-file')(filePath, (err, h) => (err ? reject(err) : resolve(h)));
  });
}

module.exports = { sendFile, uploadToDisk, checkFile, buildFileBody, formatSize, MAX_BYTES };
