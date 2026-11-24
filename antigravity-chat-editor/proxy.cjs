'use strict';
const http = require('node:http');
const https = require('node:https');
const zlib = require('node:zlib');
const crypto = require('node:crypto');
const { applyRebuiltSummary, clearTextCache } = require('./history.cjs');
const { createSummaryEngine } = require('./summary.cjs');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function routeKind(url, hasEdits) {
  if (/^\/v1internal:(?:streamGenerateContent|generateContent)(?:\?|$)/.test(url)) return 'generation';
  if (/^\/v1internal:(?:loadCodeAssist|fetchUserInfo|retrieveUserQuotaSummary|fetchAdminControls|fetchAvailableModels|listExperiments)(?:\?|$)/.test(url)) return 'control';
  if (hasEdits) throw new Error('当前请求路径尚未适配编辑，已阻止发送');
  return 'passthrough';
}

function rewriteRequest(body, rules) {
  const request = body.request;
  const fromId = /^agent\/([0-9a-f-]{36})\//i.exec(body.requestId || '')?.[1];
  const cascadeId = request?.labels?.cascade_id || fromId;
  if (!uuid.test(cascadeId || '')) throw new Error('无法识别模型请求所属会话，已阻止发送');
  if (fromId && fromId !== cascadeId) throw new Error('模型请求会话标识不一致，已阻止发送');
  const active = rules.filter(rule => rule.cascadeId === cascadeId);
  if (!active.length) return { changed: false, evidence: [] };
  if (!Array.isArray(request?.contents)) throw new Error('模型输入结构不兼容，已阻止发送旧内容');
  const groups = new Map(), bodies = [];
  for (const content of request.contents) {
    if (!['model', 'assistant'].includes(content.role)) continue;
    const bodyParts = [];
    for (const part of content.parts || []) {
      if (typeof part.text !== 'string' || part.thought === true) continue;
      if (!groups.has(part.text)) groups.set(part.text, []);
      groups.get(part.text).push({ parts: [part], content }); bodyParts.push(part);
    }
    bodies.push({ parts: bodyParts, content, text: bodyParts.map(part => part.text).join('') });
  }
  const evidence = []; let changed = false;
  for (const rule of active) {
    const exact = groups.get(rule.original) || [], matches = [];
    for (const body of bodies) {
      if (body.parts.length > 1 && body.text === rule.original) matches.push(body);
      else matches.push(...exact.filter(candidate => candidate.content === body.content));
    }
    if (!matches.length) throw new Error('目标回复已被截断、压缩或包装，不能可靠替换，已阻止发送');
    if (matches.length !== rule.matchCount || !matches[rule.occurrence]) throw new Error('重复回复数量或上下文范围发生变化，已阻止发送；请重新保存该条编辑');
    const selected = matches[rule.occurrence];
    for (const [index, part] of selected.parts.entries()) {
      if (index === 0) part.text = rule.text; else delete part.text;
      clearTextCache(part);
    }
    selected.content.parts = selected.content.parts.filter(part => Object.keys(part).length);
    changed = true;
    evidence.push({ key: rule.key, state: 'rewritten', originalHash: hash(rule.original), newHash: hash(rule.text) });
  }
  if (changed) { delete request.cachedContent; delete request.continuationToken; delete request.turnToken; }
  return { changed, evidence };
}

function createProxy({ upstream, getRules, onEvidence, summaryCacheFile,
  summaryEngine = createSummaryEngine({ upstream, cacheFile: summaryCacheFile, onProgress: onEvidence }),
  getRulesForRequest = async (_body, rules) => rules }) {
  const target = new URL(upstream);
  if (target.protocol !== 'https:' || target.username || target.password) throw new Error('上游必须是正常 HTTPS 模型服务');
  const server = http.createServer((incoming, outgoing) => {
    if (incoming.method !== 'POST' || !incoming.url.startsWith('/v1internal:') || !/^127\.0\.0\.1(?::\d+)?$/.test(incoming.headers.host || '')) {
      outgoing.writeHead(403); outgoing.end('Forbidden'); return;
    }
    const chunks = []; let size = 0;
    const controller = new AbortController();
    outgoing.on('close', () => { if (!outgoing.writableFinished) controller.abort(); });
    incoming.on('data', chunk => { size += chunk.length; if (size > 32 * 1024 * 1024) incoming.destroy(new Error('模型请求过大')); else chunks.push(chunk); });
    incoming.on('error', () => { if (!outgoing.headersSent) outgoing.writeHead(413); outgoing.end('Request too large'); });
    incoming.on('end', async () => {
      let body = Buffer.concat(chunks), evidence = [];
      const headers = { ...incoming.headers, host: target.host };
      delete headers.connection; delete headers['transfer-encoding'];
      try {
        const kind = routeKind(incoming.url, getRules().length > 0);
        if (kind === 'generation' && getRules().length) {
          const encoding = headers['content-encoding']; let decoded = body;
          if (encoding === 'gzip') decoded = zlib.gunzipSync(body, { maxOutputLength: 32 * 1024 * 1024 });
          else if (encoding && encoding !== 'identity') throw new Error('不支持此请求压缩格式，已阻止发送');
          const json = JSON.parse(decoded.toString('utf8'));
          const prepared = await getRulesForRequest(json, getRules());
          const plan = Array.isArray(prepared) ? { rules: prepared } : prepared;
          let summaryChanged = false;
          if (plan.compaction) {
            const rebuilt = await summaryEngine.rebuild(plan.compaction, { body: json, headers, signal: controller.signal });
            evidence.push(...applyRebuiltSummary(json, plan.compaction, rebuilt.summary)
              .map(item => ({ ...item, cached: rebuilt.cached })));
            summaryChanged = true;
          }
          const result = rewriteRequest(json, plan.rules); evidence.push(...result.evidence);
          if (result.changed || summaryChanged) { body = Buffer.from(JSON.stringify(json)); if (encoding === 'gzip') body = zlib.gzipSync(body); }
        }
      } catch (error) {
        if (outgoing.destroyed) return;
        onEvidence({ state: 'blocked', path: incoming.url.split('?')[0], error: error.message, at: Date.now() });
        outgoing.writeHead(409, { 'content-type': 'application/json' });
        outgoing.end(JSON.stringify({ error: { code: 409, message: error.message, status: 'FAILED_PRECONDITION' } })); return;
      }
      headers['content-length'] = String(body.length);
      const forward = https.request({ hostname: target.hostname, port: target.port || 443,
        path: target.pathname.replace(/\/$/, '') + incoming.url, method: 'POST', headers }, response => {
        onEvidence({ state: 'forwarded', status: response.statusCode, evidence, at: Date.now() });
        outgoing.writeHead(response.statusCode, response.headers); response.pipe(outgoing);
        response.on('error', () => outgoing.destroy());
      });
      forward.setTimeout(180000, () => forward.destroy(new Error('上游超时')));
      forward.on('error', error => {
        onEvidence({ state: 'upstream-error', error: error.message, evidence, at: Date.now() });
        if (!outgoing.headersSent) outgoing.writeHead(502);
        outgoing.end('Local model relay failed');
      });
      outgoing.on('close', () => { if (!outgoing.writableFinished) forward.destroy(); });
      forward.end(body);
    });
  });
  Object.defineProperty(server, 'editorSummaryEngine', { value: summaryEngine });
  return server;
}
module.exports = { createProxy, rewriteRequest, routeKind, hash };
