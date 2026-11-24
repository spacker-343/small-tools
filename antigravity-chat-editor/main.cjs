'use strict';
const { app, ipcMain, webContents, webFrameMain } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const https = require('node:https');
const { createProxy } = require('./proxy.cjs');
const { buildEditPlan, refreshRule } = require('./history.cjs');
const LOCAL = path.join(__dirname, '.local');
const EDITS = path.join(LOCAL, 'edits.json');
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CHANNEL = 'agy-chat-editor:invoke';
function atomicJSON(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = file + '.tmp'; fs.writeFileSync(temporary, JSON.stringify(value, null, 2), { mode: 0o600 }); fs.renameSync(temporary, file);
}
function start(languageServer) {
  if (globalThis.__agyChatEditorRuntime) throw new Error('请先卸载旧的运行时实例');
  let rules = fs.existsSync(EDITS) ? JSON.parse(fs.readFileSync(EDITS, 'utf8')) : [];
  if (!Array.isArray(rules)) throw new Error('编辑规则文件损坏');
  let relay, activation, activatedPid, upstream, saving = false, stopped = false;
  let last = { state: 'ready' }; const listeners = [];
  function trusted(frame) {
    try { return new URL(frame.url).origin === new URL(languageServer.getLsBaseUrl()).origin; } catch { return false; }
  }
  const call = (method, data) => new Promise((resolve, reject) => {
    const request = https.request(languageServer.getLsBaseUrl() + '/exa.language_server_pb.LanguageServerService/' + method,
      { method: 'POST', rejectUnauthorized: false, headers: { 'content-type': 'application/json', 'connect-protocol-version': '1',
        'x-codeium-csrf-token': languageServer.getLsCsrfToken() } }, response => {
        const chunks = []; let bytes = 0;
        response.on('data', chunk => { bytes += chunk.length; if (bytes > 32 * 1024 * 1024) request.destroy(new Error('会话数据过大')); else chunks.push(chunk); });
        response.on('end', () => {
          try {
            const data = JSON.parse(Buffer.concat(chunks).toString() || '{}');
            if (response.statusCode !== 200) throw new Error(data.message || `本地接口 HTTP ${response.statusCode}`);
            resolve(data);
          } catch (error) { reject(error); }
        });
      });
    request.setTimeout(20000, () => request.destroy(new Error('本地服务响应超时')));
    request.on('error', reject); request.end(JSON.stringify(data));
  });
  async function activate() {
    if (stopped) throw new Error('编辑器已停用');
    const handle = languageServer.getLsProcess();
    if (!handle?.pid || !languageServer.getLsPort()) throw new Error('本地会话服务尚未就绪');
    if (relay && activatedPid === handle.pid) return;
    if (activation) return activation;
    activation = (async () => {
      const at = handle.spawnargs.indexOf('--cloud_code_endpoint');
      const original = at >= 0 ? handle.spawnargs[at + 1] : undefined;
      if (!original || new URL(original).protocol !== 'https:') throw new Error('无法确认原始模型服务地址');
      upstream = original;
      if (!relay) {
        relay = createProxy({ upstream, getRules: () => rules, summaryCacheFile: path.join(LOCAL, 'summary-cache.json'),
          getRulesForRequest: async (body, snapshot) => {
          const cascadeId = body.request?.labels?.cascade_id || /^agent\/([0-9a-f-]{36})\//i.exec(body.requestId || '')?.[1];
          const active = snapshot.filter(rule => rule.cascadeId === cascadeId);
          if (!active.length) return [];
          const currentSteps = await getSteps(cascadeId);
          return buildEditPlan(currentSteps, active, body);
        }, onEvidence: value => {
          if (value.state === 'forwarded' && !value.evidence?.length) return;
          last = value;
          try { atomicJSON(path.join(LOCAL, 'last-forward.json'), value); }
          catch (error) { console.error('Unable to write local editor receipt:', error.message); }
        } });
        await new Promise((resolve, reject) => { relay.once('error', reject); relay.listen(0, '127.0.0.1', resolve); });
      }
      await call('SetCloudCodeURL', { url: `http://127.0.0.1:${relay.address().port}` });
      activatedPid = handle.pid;
    })().finally(() => { activation = undefined; });
    return activation;
  }
  async function getSteps(cascadeId) {
    const all = [];
    while (all.length < 100000) {
      const result = await call('GetCascadeTrajectorySteps', { cascadeId, stepOffset: all.length });
      if (!result.steps?.length) return all;
      all.push(...result.steps);
    }
    throw new Error('会话过大，无法可靠定位回复');
  }
  async function save(input) {
    if (saving) throw new Error('上一条编辑正在保存');
    const cascadeId = input?.viewCascadeId || input?.cascadeId;
    if (!UUID.test(cascadeId || '') || !input.messageId || !Number.isInteger(input.index) || typeof input.original !== 'string' ||
        typeof input.text !== 'string' || !input.text.trim() || Buffer.byteLength(input.original) > 1024 * 1024 ||
        Buffer.byteLength(input.text) > 1024 * 1024) throw new Error('无效消息');
    saving = true;
    try {
      await activate(); const steps = await getSteps(cascadeId);
      const matches = steps.map((step, index) => ({ step, index })).filter(({ step }) =>
        step.plannerResponse?.messageId === input.messageId && step.plannerResponse?.response === input.original);
      if (matches.length !== 1 || matches[0].step.status !== 'CORTEX_STEP_STATUS_DONE') throw new Error('消息已变化或尚未完成，请重新打开编辑器');
      const target = matches[0].index;
      const segment = steps.slice(0, target).findLastIndex(step => step.checkpoint) + 1;
      const matching = steps.map((step, index) => ({ step, index })).filter(({ step, index }) => index >= segment && step.plannerResponse?.response === input.original);
      const key = cascadeId + '/' + input.messageId;
      const rule = { key, cascadeId, sourceCascadeId: input.cascadeId, messageId: input.messageId, index: input.index,
        original: input.original, text: input.text, occurrence: matching.findIndex(item => item.index === target),
        matchCount: matching.length, savedAt: Date.now() };
      const next = rules.filter(item => item.key !== key); if (input.text !== input.original) next.push(rule);
      if (fs.existsSync(EDITS)) {
        const backups = path.join(LOCAL, 'backups/edits'); fs.mkdirSync(backups, { recursive: true }); fs.copyFileSync(EDITS, path.join(backups, Date.now() + '.json'));
      }
      atomicJSON(EDITS, next); rules = next; last = { state: 'saved', key, at: Date.now() };
      return { ok: true, rule: input.text === input.original ? null : rule, rules };
    } finally { saving = false; }
  }
  ipcMain.removeHandler(CHANNEL);
  ipcMain.handle(CHANNEL, async (event, data) => {
    if (!trusted(event.senderFrame) || !trusted(event.sender.mainFrame)) throw new Error('禁止在非 Antigravity 页面调用');
    if (data?.action === 'save') return save(data.request);
    if (data?.action === 'list') { await activate(); return { ok: true, rules, last }; }
    if (data?.action === 'status') return { ok: true, state: 'live', last, active: !!relay && activatedPid === languageServer.getLsProcess()?.pid };
    throw new Error('未知编辑器操作');
  });
  const script = () => fs.readFileSync(path.join(__dirname, 'renderer.js'), 'utf8');
  const attach = contents => {
    const inject = () => {
      if (trusted(contents.mainFrame)) {
        void activate().catch(error => { last = { state: 'error', error: error.message }; }); contents.executeJavaScript(script()).catch(console.error);
      }
    };
    const child = (_event, isMain, processId, routingId) => {
      if (isMain) return; const frame = webFrameMain.fromId(processId, routingId);
      if (frame && trusted(frame) && trusted(contents.mainFrame)) frame.executeJavaScript(script()).catch(console.error);
    };
    contents.on('did-finish-load', inject); contents.on('did-frame-finish-load', child);
    listeners.push(() => { if (!contents.isDestroyed()) { contents.off('did-finish-load', inject); contents.off('did-frame-finish-load', child); } });
    if (!contents.isLoading()) inject();
  };
  const created = (_event, contents) => attach(contents); app.on('web-contents-created', created);
  for (const contents of webContents.getAllWebContents()) attach(contents);
  const runtime = { activate, save, getRules: () => rules, getLast: () => last,
    async dispose() {
      stopped = true; app.off('web-contents-created', created); listeners.forEach(remove => remove()); ipcMain.removeHandler(CHANNEL);
      if (relay) { await call('SetCloudCodeURL', { url: upstream }); relay.close(); }
      delete globalThis.__agyChatEditorRuntime;
    },
  };
  globalThis.__agyChatEditorRuntime = runtime; return runtime;
}
module.exports = { start, refreshRule };
