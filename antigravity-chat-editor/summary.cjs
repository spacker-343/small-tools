'use strict';
const https = require('node:https');
const zlib = require('node:zlib');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { digest } = require('./history.cjs');

const INSTRUCTIONS = `Create a factual conversation handoff summary from the supplied corrected history.
The history has already applied the user's edits to assistant messages. Edited assistant text replaces earlier versions and is authoritative.
Preserve user goals, constraints, decisions, exact identifiers and paths, actual tool outcomes, code state, open questions and next steps.
Do not execute tools, invent work, or quote superseded versions of edited assistant messages. Do not include reasoning traces.
Write a concise structured summary in the language of the conversation. Return only the summary.`;

function chunks(records, limit = 110000) {
  const output = []; let current = '';
  for (const record of records) {
    const text = JSON.stringify(record) + '\n';
    if (current && current.length + text.length > limit) { output.push(current); current = ''; }
    if (text.length > limit) {
      if (current) { output.push(current); current = ''; }
      for (let offset = 0; offset < text.length; offset += limit) output.push(text.slice(offset, offset + limit));
    } else current += text;
  }
  if (current) output.push(current);
  return output.length ? output : ['[]'];
}

function extractText(buffer, headers) {
  const encoding = headers['content-encoding'];
  if (encoding === 'gzip') buffer = zlib.gunzipSync(buffer);
  else if (encoding === 'br') buffer = zlib.brotliDecompressSync(buffer);
  else if (encoding === 'deflate') buffer = zlib.inflateSync(buffer);
  const text = buffer.toString('utf8'), messages = [];
  if (/^\s*[\[{]/.test(text)) {
    const value = JSON.parse(text); messages.push(...(Array.isArray(value) ? value : [value]));
  } else {
    for (const event of text.split(/\r?\n\r?\n/)) {
      const data = event.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
      if (data && data !== '[DONE]') messages.push(JSON.parse(data));
    }
  }
  let result = '';
  for (const message of messages) {
    if (message.error) throw new Error(message.error.message || '摘要模型返回错误');
    const response = message.response || message;
    if (response.error) throw new Error(response.error.message || '摘要模型返回错误');
    for (const part of response.candidates?.[0]?.content?.parts || []) if (!part.thought && typeof part.text === 'string') result += part.text;
  }
  if (!result.trim()) throw new Error('摘要模型未返回可用文本');
  return result.trim();
}

function generate(upstream, template, headers, prompt, signal) {
  const generationConfig = { ...(template.request?.generationConfig || {}), temperature: 0,
    maxOutputTokens: Math.max(8192, template.request?.generationConfig?.maxOutputTokens || 0) };
  for (const key of ['responseSchema', 'responseJsonSchema', 'responseMimeType', 'stopSequences']) delete generationConfig[key];
  const target = new URL(upstream), body = { ...template, requestId: (template.requestId || 'agent') + '/edit-summary/' + crypto.randomUUID(),
    request: { contents: [{ role: 'user', parts: [{ text: prompt }] }],
      systemInstruction: { role: 'user', parts: [{ text: INSTRUCTIONS }] },
      generationConfig,
      labels: { ...(template.request?.labels || {}) } } };
  const bytes = Buffer.from(JSON.stringify(body));
  const outgoingHeaders = { ...headers, host: target.host, 'content-type': 'application/json',
    'content-length': String(bytes.length), 'accept-encoding': 'identity' };
  for (const name of ['connection', 'transfer-encoding', 'content-encoding']) delete outgoingHeaders[name];
  return new Promise((resolve, reject) => {
    const request = https.request({ hostname: target.hostname, port: target.port || 443,
      path: target.pathname.replace(/\/$/, '') + '/v1internal:streamGenerateContent?alt=sse',
      method: 'POST', headers: outgoingHeaders, signal }, response => {
      const buffers = []; let size = 0;
      response.on('data', chunk => { size += chunk.length; if (size > 8 * 1024 * 1024) request.destroy(new Error('摘要响应过大')); else buffers.push(chunk); });
      response.on('error', reject);
      response.on('end', () => {
        try {
          if (response.statusCode !== 200) throw new Error('摘要重建服务 HTTP ' + response.statusCode);
          resolve(extractText(Buffer.concat(buffers), response.headers));
        } catch (error) { reject(error); }
      });
    });
    request.setTimeout(120000, () => request.destroy(new Error('摘要重建超时，请重试')));
    request.on('error', reject); request.end(bytes);
  });
}

function createSummaryEngine({ upstream, cacheFile, generateText = generate, onProgress = () => {} }) {
  let cache = {};
  try { if (cacheFile && fs.existsSync(cacheFile)) cache = JSON.parse(fs.readFileSync(cacheFile, 'utf8')); } catch { cache = {}; }
  if (!cache || typeof cache !== 'object' || Array.isArray(cache)) cache = {};
  const pending = new Map();
  function persist() {
    cache = Object.fromEntries(Object.entries(cache).sort((a, b) => b[1].at - a[1].at).slice(0, 16));
    if (!cacheFile) return;
    try {
      fs.mkdirSync(path.dirname(cacheFile), { recursive: true });
      fs.writeFileSync(cacheFile + '.tmp', JSON.stringify(cache), { mode: 0o600 }); fs.renameSync(cacheFile + '.tmp', cacheFile);
    } catch (error) { console.error('Unable to persist rebuilt summary:', error.message); }
  }
  return {
    async rebuild(plan, { body, headers, signal }) {
      const key = digest(plan.fingerprint + '\n' + body.model);
      if (cache[key]?.summary && cache[key].complete !== false) return { summary: cache[key].summary, cached: true };
      if (pending.has(key)) return pending.get(key);
      const task = (async () => {
        const prefix = Object.values(cache).filter(entry => entry.complete && entry.scope === plan.scope &&
          entry.model === body.model && entry.historyStart === plan.historyStart &&
          Number.isInteger(entry.coveredEnd) && entry.coveredEnd <= plan.coveredEnd &&
          entry.prefixHash === digest(JSON.stringify(plan.records.filter(record => record.step === undefined || record.step < entry.coveredEnd))))
          .sort((a, b) => b.coveredEnd - a.coveredEnd)[0];
        if (prefix && prefix.coveredEnd === plan.coveredEnd) return { summary: prefix.summary, cached: true };
        const remaining = prefix ? plan.records.filter(record => record.step !== undefined && record.step >= prefix.coveredEnd) : plan.records;
        const parts = chunks(remaining);
        const partsHash = digest(JSON.stringify({ parts, prefix: prefix?.summary || '' }));
        const partial = cache[key]?.complete === false && cache[key].partsHash === partsHash ? cache[key] : null;
        let summary = partial?.summary || (prefix ? prefix.summary + '\n\nPreviously edited assistant messages:\n' +
          (prefix.edits || []).map(edit => edit.text).join('\n\n') : '');
        const first = partial?.completedChunks || 0;
        for (let index = first; index < parts.length; index++) {
          const part = parts[index];
          onProgress({ state: 'summary-building', checkpoint: plan.checkpoint, chunk: index + 1, chunks: parts.length });
          const prompt = (summary ? 'Corrected summary of earlier history:\n' + summary + '\n\n' : '') +
            'Next segment of corrected history (JSON records, possibly continued from the previous segment):\n' + part;
          summary = await generateText(upstream, body, headers, prompt, signal);
          cache[key] = { summary, at: Date.now(), complete: false, completedChunks: index + 1, partsHash };
          persist();
        }
        cache[key] = { summary, at: Date.now(), complete: true, checkpoint: plan.checkpoint, model: body.model,
          scope: plan.scope, historyStart: plan.historyStart, coveredEnd: plan.coveredEnd, edits: plan.edits,
          prefixHash: digest(JSON.stringify(plan.records)) };
        persist();
        return { summary, cached: false };
      })();
      pending.set(key, task);
      try { return await task; } finally { pending.delete(key); }
    },
  };
}

module.exports = { createSummaryEngine, extractText, chunks, generate };
