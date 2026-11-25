'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { Readable } = require('node:stream');
const https = require('node:https');
const { createProxy } = require('../proxy.cjs');
const ID = '11111111-1111-4111-8111-111111111111';

function fakeUpstream(t, captured) {
  t.mock.method(https, 'request', (_options, callback) => {
    const outgoing = new EventEmitter(); outgoing.setTimeout = () => outgoing;
    outgoing.end = bytes => {
      captured.push(JSON.parse(bytes.toString()));
      const response = Readable.from(['{"ok":true}']); response.statusCode = 200; response.headers = { 'content-type': 'application/json' };
      callback(response);
    };
    outgoing.destroy = () => {}; return outgoing;
  });
}
async function listen(server) { await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); return 'http://127.0.0.1:' + server.address().port; }

test('native background generation without matching rules is forwarded byte for byte', async t => {
  const captured = []; fakeUpstream(t, captured);
  const body = { requestId: 'native-background-check', request: { contents: [{ role: 'user', parts: [{ text: 'native check' }] }] } };
  const server = createProxy({ upstream: 'https://example.com', getRules: () => [{ cascadeId: ID }], onEvidence() {},
    getRulesForRequest: async () => [] });
  try {
    const url = await listen(server);
    const response = await fetch(url + '/v1internal:generateContent', { method: 'POST', body: JSON.stringify(body) });
    assert.equal(response.status, 200); await response.text();
    assert.deepEqual(captured, [body]);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('conflicting identities are blocked even when rule preparation would return empty', async t => {
  const captured = []; fakeUpstream(t, captured);
  const server = createProxy({ upstream: 'https://example.com', getRules: () => [{ cascadeId: ID }], onEvidence() {},
    getRulesForRequest: async () => [] });
  try {
    const url = await listen(server);
    for (const body of [
      { requestId: 'agent/' + ID + '/turn', request: { labels: { cascade_id: '22222222-2222-4222-8222-222222222222' } } },
      { requestId: 'agent/malformed/turn', request: {} }
    ]) {
      const response = await fetch(url + '/v1internal:streamGenerateContent', { method: 'POST', body: JSON.stringify(body) });
      assert.equal(response.status, 409); await response.text();
    }
    assert.equal(captured.length, 0);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('Fork ACL sync and native GET endpoint preserve methods and payloads with edits enabled', async t => {
  const captured = [];
  t.mock.method(https, 'request', (options, callback) => {
    const request = new EventEmitter(); request.setTimeout = () => request;
    request.end = bytes => {
      captured.push({ method: options.method, path: options.path, body: bytes.toString() });
      const response = Readable.from(['{}']); response.statusCode = 200; response.headers = {};
      callback(response);
    };
    request.destroy = () => {}; return request;
  });
  const server = createProxy({ upstream: 'https://example.com', getRules: () => [{ cascadeId: ID }], onEvidence() {},
    getRulesForRequest: async () => { throw new Error('must not inspect controls'); } });
  try {
    const url = await listen(server);
    for (const entry of [{ method: 'POST', path: '/v1internal:writeTrajectoryAcls', body: '{"trajectory":"test"}' },
      { method: 'GET', path: '/v1internal/cascadeNuxes?page_size=1000', body: '' }]) {
      const response = await fetch(url + entry.path, { method: entry.method, ...(entry.method === 'POST' ? { body: entry.body } : {}) });
      assert.equal(response.status, 200); await response.text();
      assert.deepEqual(captured.at(-1), entry);
    }
    const denied = await fetch(url + '/v1internal/privateEndpoint');
    assert.equal(denied.status, 403); await denied.text();
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('listExperiments passes through while edit rules exist', async t => {
  const captured = []; fakeUpstream(t, captured);
  const server = createProxy({ upstream: 'https://example.com', getRules: () => [{ key: 'edited' }], onEvidence() {},
    getRulesForRequest: async () => { throw new Error('configuration request must not inspect chat history'); } });
  try {
    const url = await listen(server);
    const response = await fetch(url + '/v1internal:listExperiments', { method: 'POST', body: '{"project":"test"}' });
    assert.equal(response.status, 200); assert.deepEqual(await response.json(), { ok: true });
    assert.deepEqual(captured, [{ project: 'test' }]);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('a compacted request forwards only after replacing its old summary and direct reply', async t => {
  const captured = [], evidence = []; fakeUpstream(t, captured);
  const direct = { key: ID + '/b', cascadeId: ID, original: 'DIRECT_OLD', text: 'DIRECT_NEW', occurrence: 0, matchCount: 1 };
  const server = createProxy({ upstream: 'https://example.com', getRules: () => [direct], onEvidence: item => evidence.push(item),
    getRulesForRequest: async () => ({ rules: [direct], compaction: { originalSummary: 'STALE_SUMMARY', checkpoint: 2,
      edits: [{ key: ID + '/a', messageId: 'a', text: '**EDITED_HISTORY**' }] } }),
    summaryEngine: { rebuild: async () => ({ summary: 'Corrected handoff.', cached: true }) } });
  try {
    const url = await listen(server);
    const body = { requestId: 'agent/' + ID + '/turn', request: { labels: { cascade_id: ID }, contents: [
      { role: 'user', parts: [{ text: '<CONTEXT_SUMMARY><summary>STALE_SUMMARY</summary></CONTEXT_SUMMARY>' }] }, { role: 'model', parts: [{ text: 'DIRECT_OLD' }] }] } };
    const response = await fetch(url + '/v1internal:streamGenerateContent?alt=sse', { method: 'POST', body: JSON.stringify(body) });
    assert.equal(response.status, 200); await response.text();
    const sent = JSON.stringify(captured[0]);
    assert.ok(!sent.includes('STALE_SUMMARY')); assert.ok(sent.includes('**EDITED_HISTORY**'));
    assert.ok(sent.includes('DIRECT_NEW')); assert.ok(!sent.includes('DIRECT_OLD'));
    assert.deepEqual(evidence.at(-1).evidence.map(item => item.state), ['summary-rewritten', 'rewritten']);
  } finally { await new Promise(resolve => server.close(resolve)); }
});
