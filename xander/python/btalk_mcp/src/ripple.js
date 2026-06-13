'use strict';
// Ripple 工单 API(纯 HTTP,cookie 入参)。逻辑移植自 ai_helper ripple-agent.js,输出结构化对象。
const https = require('https');
const { identityHeaders } = require('./headers');
const BASE = 'https://ripple.blibee.com';

function api(pathUrl, cookie, body = null, method = 'POST') {
  return new Promise((resolve, reject) => {
    const u = new URL(pathUrl);
    const data = body ? JSON.stringify(body) : null;
    const req = https.request({
      hostname: u.hostname, path: u.pathname + u.search, method,
      headers: {
        ...identityHeaders(),
        accept: '*/*', 'content-type': 'application/json;charset=UTF-8',
        higher_priority_type: 'inner', Cookie: cookie, Referer: 'https://ripple.blibee.com/ripple/pc/',
        ...(data ? { 'content-length': Buffer.byteLength(data) } : {}),
      }, timeout: 20000,
    }, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d || '{}')); } catch (e) { reject(new Error('ripple 响应解析失败')); } });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('ripple 请求超时')); });
    if (data) req.write(data);
    req.end();
  });
}

// ---- 纯函数(可单测) ----
function parseCascade(field) {
  const rules = field.showCascadeRuels || [];
  return rules.length ? { parentField: rules[0].parentField, keyword: rules[0].cascadeKeyWords } : null;
}
/** 级联匹配:keyword 可能是逗号分隔的多个触发值(OR);父字段值含其中任一即触发 */
function cascadeMatch(parentValue, keyword) {
  if (parentValue == null) return false;
  const pv = String(parentValue);
  return String(keyword).split(',').map((k) => k.trim()).filter(Boolean).some((k) => pv.includes(k));
}
function isFieldVisible(field, userInputs) {
  if (field.visible === false) return false;
  const c = parseCascade(field);
  if (!c) return true;
  return cascadeMatch(userInputs[c.parentField], c.keyword);
}
function parseFieldOptions(field) {
  if (field.source && field.source.source && field.source.sourceType === 4) {
    return field.source.source.split(',').map((it) => {
      const i = it.indexOf(':');
      return i > 0 ? { value: it.slice(0, i).trim(), label: it.slice(i + 1).trim() } : { value: it.trim(), label: it.trim() };
    });
  }
  for (const k of ['options', 'selectOptions', 'defaultValues']) {
    if (Array.isArray(field[k]) && field[k].length) return field[k].map((o) => ({ value: o.value, label: o.label || o.value }));
  }
  return [];
}
/** 按表单字段校验用户输入:未知字段名 / RADIO·SELECT 值不在 options / 级联字段父项未触发就填了 */
function validateFieldValues(fields, userInputs) {
  const errors = [];
  const byName = {};
  for (const f of fields) byName[f.name] = f;
  for (const name of Object.keys(userInputs || {})) {
    const f = byName[name];
    if (!f) { errors.push(`未知字段「${name}」——表单里没有这个字段(见 --show-form 的 fields)`); continue; }
    const v = userInputs[name];
    // RADIO/SELECT:值必须是 options 里的 value(或 label)
    const opts = parseFieldOptions(f);
    if ((f.elementType === 'RADIO' || f.elementType === 'SELECT') && opts.length) {
      if (!opts.some((o) => String(o.value) === String(v) || o.label === v)) {
        errors.push(`字段「${f.title || name}」的值"${v}"无效,只能填: ${opts.map((o) => o.value + '=' + o.label).join(' | ')}`);
      }
    }
    // 级联字段:父字段没选触发项却填了它
    const c = parseCascade(f);
    if (c && !cascadeMatch(userInputs[c.parentField], c.keyword)) {
      errors.push(`字段「${f.title || name}」是级联字段,仅当「${c.parentField}」选了含"${c.keyword}"的值时才需填,当前父项未触发`);
    }
  }
  return errors;
}

function validateRequired(fields, userInputs) {
  const miss = [];
  for (const f of fields) {
    if (!isFieldVisible(f, userInputs) || !f.required) continue;
    const has = userInputs[f.name] || f.defaultValue || (f.defaultValues && f.defaultValues.length);
    if (!has) miss.push({ name: f.name, title: f.title });
  }
  return miss;
}
function normalizeValues(v) {
  if (v === null || v === undefined) return [];
  if (v === '') return [{ value: '', label: '' }];
  if (Array.isArray(v)) return v.map((x) => (typeof x === 'object' ? x : { value: x, label: x }));
  if (typeof v === 'object') return [v];
  return [{ value: v, label: v }];
}
function buildFormVariables(fields, userInputs) {
  return fields.filter((f) => f.visible !== false).map((field) => {
    const name = field.name;
    if (!isFieldVisible(field, userInputs)) return { name, values: [], visible: false, readonly: field.readonly === 1 };
    let value = userInputs[name];
    if (value === undefined || value === null) {
      if (field.defaultValues && field.defaultValues.length) {
        return { name, values: field.defaultValues.map((v) => ({ value: v.value ?? v, label: v.label ?? v.value ?? v })), visible: true, readonly: field.readonly === 1 };
      }
      if (field.defaultValue !== undefined) {
        const m = parseFieldOptions(field).find((o) => o.value === field.defaultValue);
        value = { value: field.defaultValue, label: (m && m.label) || field.defaultLabel || field.defaultValue };
      } else value = '';
    }
    if (typeof value === 'string' && (field.elementType === 'RADIO' || field.elementType === 'SELECT')) {
      const m = parseFieldOptions(field).find((o) => o.label === value || o.value === value);
      value = m ? { value: m.value, label: m.label } : { value, label: value };
    }
    return { name, values: normalizeValues(value), visible: true, readonly: field.readonly === 1 };
  });
}

// ---- API 流程 ----
async function list(cookie, { tab = 1, page = 1, size = 20, flowCodes, keyWords } = {}) {
  const map = { 1: 1, 2: 2, 3: 4, 4: 3 };
  const body = { page: { pageSize: size, pageNo: page }, userViewType: map[tab] || tab, timeQueryType: 'CREATE_TIME', terminal: 'PC', apiVersion: 'v7' };
  if (flowCodes) body.flowCodes = String(flowCodes);   // 按流程类别 flowCode 筛选(逗号分隔可多个)
  if (keyWords) body.keyWords = String(keyWords);      // 关键词搜索(任务名/编码)
  if (tab === 3 || tab === 4) {
    const now = new Date();
    body.endDate = now.toISOString().slice(0, 10);
    body.startDate = new Date(now - 60 * 86400000).toISOString().slice(0, 10);
    if (tab === 3) body.timeQueryType = 'HANDLE_TIME';
  }
  const res = await api(`${BASE}/ripple/feedback/user/flow/order/query/order_list/v1?_time=${Date.now()}`, cookie, body);
  if (res.status !== 0) throw new Error(res.msg || res.message || '查询失败');
  const orders = (res.data && res.data.flowOrders && res.data.flowOrders.data) || [];
  const pg = (res.data && res.data.flowOrders && res.data.flowOrders.page) || {};
  return {
    total: pg.totalSize || orders.length, page, totalPages: pg.totalPage || 1,
    orders: orders.map((o) => ({
      id: o.flowOrderId,
      name: o.flowOrderName || '',
      category: o.categoryName || '',                        // 流程类别/模版名(list 不返回 flowCode 代码,需 show)
      status: o.orderStatusName || '',
      node: o.taskNodeName || '',
      handler: (o.handler && (o.handler.nameCN || o.handler.displayName)) || (o.formatAssignee && o.formatAssignee.formatAssignee) || '',
      initiator: (o.initiator && (o.initiator.nameCN || o.initiator.displayName || o.initiator.account)) || '',
      createTime: o.createTime || '',                        // 发起时间
      deadline: o.deadLineTime || (o.topFormatTime && o.topFormatTime.time) || '',   // 截止时间
      timeLeft: (o.bottomFormatTime && o.bottomFormatTime.time)
        ? ((o.bottomFormatTime.timeType === 'overTime' ? '超时' : '剩余') + o.bottomFormatTime.time)
        : '',                                                // 超时/剩余(对齐桌面端列表)
    })),
  };
}
/** 流程类别统计:某 tab 下按流程类别(含 flowCode)分组的工单数 + 节点分布 */
async function category(cookie, { tab = 1 } = {}) {
  const map = { 1: 1, 2: 2, 3: 4, 4: 3 };
  const body = { page: { pageSize: 50, pageNo: 1 }, userViewType: map[tab] || tab, timeQueryType: 'CREATE_TIME', terminal: 'PC', apiVersion: 'v6' };
  const res = await api(`${BASE}/ripple/feedback/user/flow/order/query/order_category/v1`, cookie, body);
  if (res.status !== 0) throw new Error(res.msg || res.message || '查询流程类别失败');
  const d = res.data || {};
  const cnt = (label) => { const m = String(label || '').match(/\((\d+)\)\s*$/); return m ? Number(m[1]) : 0; };
  return {
    total: d.totalCount || 0,
    categories: (d.flowCategoryVoList || []).map((c) => ({
      flowCode: c.value || '',
      name: c.name || '',
      count: cnt(c.label),
      nodes: (c.nodeCategoryVoList || []).map((n) => ({ name: n.name || '', node: n.value || '', count: cnt(n.label) })),
    })),
  };
}

async function detail(cookie, orderId) {
  const res = await api(`${BASE}/ripple/feedback/user/flow/order/query/order_detail/vx`, cookie, { terminal: 'PC', apiVersion: 'v4', flowOrderId: Number(orderId) });
  if (res.status !== 0) throw new Error(res.msg || res.message || '查询详情失败');
  const d = res.data, tasks = d.taskOrderList || [];
  const cur = tasks.find((t) => t.taskStatus === 'DOING') || tasks.find((t) => t.taskStatus === 'NEW_ORDER') || tasks[tasks.length - 1];
  const fmtH = (h) => !h ? '' : (Array.isArray(h) ? h.map((x) => x.displayName || x.nameCN || x.code).join(', ') : (h.displayName || h.nameCN || h.code || ''));
  // 把 variableGroups 的表单变量摊平成 [{label,value}](意见/原因/分配给/审批意见等)
  const fmtForm = (vgs) => {
    const arr = [];
    for (const g of (vgs || [])) for (const v of (g.formVariables || [])) {
      const val = (v.values || []).map((x) => x.label || x.value).filter((x) => x != null && x !== '').join(', ');
      if (val) arr.push({ label: v.title || v.name || '', value: val });
    }
    return arr;
  };
  return {
    raw: d, taskOrderId: cur && cur.taskOrderId, taskStatus: cur && cur.taskStatus,
    name: d.flowOrderName || '', flowCode: d.flowCode, flowName: d.flowName || '', category: d.categoryName || '',
    initiator: fmtH(d.initiator), createTime: d.createTime || '',
    orderStatus: d.orderStatus, orderResult: d.orderResult, node: cur && (cur.taskName || cur.taskNodeName || cur.taskNodeId),
    handlers: fmtH(cur && cur.handler),
    // 处理进度(对齐桌面端):每条 = 处理人/部门/角色/操作/时间/内容
    tasks: tasks.map((t) => ({
      node: t.taskName || t.taskNodeName || t.taskNodeId || '',
      operate: t.operateName || '',                    // 催办/分配/同意/申请/驳回
      status: t.taskStatusName || t.taskStatus || '',
      handler: fmtH(t.handler),
      dept: t.deptName || '',
      role: t.roleType || '',
      time: t.handlerTime || '',
      form: fmtForm(t.variableGroups),                 // 意见/原因/分配给等 [{label,value}]
    })),
    formData: fmtForm(d.variableGroups),
    operations: (d.operateButtons || []).map((b) => ({ type: b.name || b.operateType || b.type || '', title: b.alias || b.title || b.name || '' })).filter((o) => o.type),
  };
}
async function actionForm(cookie, orderId, taskOrderId, operateType) {
  const res = await api(`${BASE}/ripple/feedback/user/flow/order/query/order_action_form/v3?_time=${Date.now()}`, cookie, { terminal: 'PC', taskOrderId, flowOrderId: Number(orderId), operateType });
  return (res.status === 0 && res.data && res.data.fieldGroups && res.data.fieldGroups[0] && res.data.fieldGroups[0].fields) || [];
}
const HANDLE = {
  ACCEPT: 'accept/vx', APPROVE: 'approve/vx2', TRANSFER: 'transfer/vx', ASSIGN: 'transfer/vx',
  REJECT: 'reject/vx', FEEDBACK: 'feedback/vx',
};
async function submit(cookie, orderId, taskOrderId, operateType, formVariables) {
  const seg = HANDLE[operateType] || `${operateType.toLowerCase()}/vx`;
  const body = { apiVersion: 'v2', terminal: 'PC', taskOrderId, flowOrderId: Number(orderId), formGroup: [{ index: 0, multi: false, formVariables, seq: 1 }] };
  if (operateType === 'APPROVE') { body.nodeAssigneeMap = {}; body.ignoreValidateTip = false; }
  const res = await api(`${BASE}/ripple/feedback/user/flow/order/handle/${seg}?_time=${Date.now()}`, cookie, body);
  if (res.status !== 0) throw new Error(res.msg || res.message || `执行 ${operateType} 失败`);
  return res.data;
}
function norm(t) { return ({ CLAIM: 'ACCEPT', PROCESS: 'APPROVE', COMPLETE: 'APPROVE' }[String(t).toUpperCase()]) || String(t).toUpperCase(); }

/** 执行操作:未领取且非 ACCEPT 先自动 ACCEPT 再刷新可用操作 */
async function action(cookie, orderId, type, { showForm = false, auto = false, fields: userInputs = {} } = {}) {
  let det = await detail(cookie, orderId);
  const op = norm(type);
  // --show-form 纯只读:不自动领取(看表单不应改变工单状态);实际提交时才自动领取
  if (!showForm && det.taskStatus === 'NEW_ORDER' && op !== 'ACCEPT') {
    const acceptFields = await actionForm(cookie, orderId, det.taskOrderId, 'ACCEPT');
    await submit(cookie, orderId, det.taskOrderId, 'ACCEPT', buildFormVariables(acceptFields, {}));
    det = await detail(cookie, orderId);
  }
  const opTypes = (det.operations || []).map((o) => o.type);
  if (!showForm && op !== 'ACCEPT' && !opTypes.includes(op)) {
    throw new Error(`操作 ${op} 不可用,可用: ${opTypes.join(', ')}`);
  }
  const fields = await actionForm(cookie, orderId, det.taskOrderId, op);
  if (showForm) {
    const unavailable = op !== 'ACCEPT' && !opTypes.includes(op);
    const visible = fields.filter((f) => f.visible !== false);
    const fieldsOut = visible.map((f) => {
      const o = { name: f.name, title: f.title, type: f.elementType, required: !!f.required };
      const opts = parseFieldOptions(f);
      if (opts.length) o.options = opts;
      const c = parseCascade(f);
      if (c) {
        // 级联:父字段值「包含」keyword 时本字段才出现。triggerOptions 列出具体触发的父字段选项。
        const parent = visible.find((x) => x.name === c.parentField);
        const triggerOpts = parent ? parseFieldOptions(parent).filter((p) => cascadeMatch(p.value, c.keyword)) : [];
        o.showWhen = {
          field: c.parentField,           // 父字段 name
          operator: 'includes',           // 触发条件:父字段的值「包含」keyword
          keyword: c.keyword,
          triggerOptions: triggerOpts,    // [{value,label}] 选了这些父字段选项才需填本字段
          note: `级联:只有当字段「${c.parentField}」填了 ${triggerOpts.map((t) => `${t.value}(${t.label})`).join('、') || ('值含"' + c.keyword + '"的项')} 时,本字段才出现且${f.required ? '必填' : '可填'};否则不要填(填了会被校验拦下)`,
        };
      }
      return o;
    });
    // 示例命令:非级联的必填/有选项字段给个值(级联字段默认不填,触发时再加)
    const ex = fieldsOut
      .filter((f) => !f.showWhen && (f.required || (f.options && f.options.length)))
      .map((f) => `--field "${f.name}=${f.options && f.options.length ? f.options[0].value : '值'}"`);
    return {
      showForm: true,
      operation: op,
      workflow: 'ripple 工单标准处理三步:① btalk ripple show <工单号> 看详情+operations(可用操作) → ② btalk ripple action <工单号> <操作> --show-form 看表单填写规则(就是本响应) → ③ btalk ripple action <工单号> <操作> --field "k=v" 提交。注意:FEEDBACK 只追加反馈不关单;APPROVE 才推进/关单;级联字段(带 showWhen)只有父字段选了 triggerOptions 时才填,否则不填',
      fields: fieldsOut,
      hint: '填法:--field "name=value";RADIO/SELECT 的 value 取 options[].value(不是 label);带 showWhen 的是级联字段——仅当父字段(showWhen.field)的值含 showWhen.keyword(即选了 triggerOptions 里的项)时才填,平时别填;--auto 用默认值跳过手填',
      example: `btalk ripple action ${orderId} ${op} ${ex.join(' ')}`.trim(),
      ...(unavailable ? { note: `操作「${op}」当前不可用(可用: ${opTypes.join(', ') || '无'})。多人待处理工单需先领取(ACCEPT)后 ${op} 表单才出现;--show-form 不会自动领取——可先: btalk ripple action ${orderId} ACCEPT --auto` } : {}),
    };
  }
  // 按表单字段校验用户 --field 输入(未知字段 / RADIO·SELECT 值非法 / 级联字段父项未触发)
  const valErrors = validateFieldValues(fields, userInputs);
  if (valErrors.length) throw new Error('字段校验未通过(以 --show-form 为准):\n  - ' + valErrors.join('\n  - '));
  const miss = validateRequired(fields, userInputs);
  if (miss.length && !auto) throw new Error('缺少必填字段: ' + miss.map((m) => `${m.title}(${m.name})`).join(', ') + ' —— 用 --field 填写或 --auto');
  const result = await submit(cookie, orderId, det.taskOrderId, op, buildFormVariables(fields, userInputs));
  return { done: true, operation: op, flowOrderId: (result && result.flowOrderId) || orderId };
}

module.exports = {
  list, category, detail, actionForm, action,
  isFieldVisible, parseFieldOptions, validateRequired, validateFieldValues, buildFormVariables, normalizeValues, parseCascade,
};
