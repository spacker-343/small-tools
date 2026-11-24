'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { rewriteRequest, routeKind } = require('../proxy.cjs');
const ID = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const rule = (extra = {}) => ({ key: ID + '/bot-a', cascadeId: ID, original: 'old', text: 'new', occurrence: 0, matchCount: 1, ...extra });
function request(id = ID) { return { requestId: 'agent/' + id + '/turn', request: { labels: { cascade_id: id }, contents: [
  { role: 'user', parts: [{ text: 'old' }] }, { role: 'model', parts: [{ text: 'old', thoughtSignature: 'signature' }, { text: 'old', thought: true }] },
] } }; }
test('replaces only the selected assistant body and clears its obsolete signature', () => {
  const body = request(); body.request.cachedContent = 'cached';
  const result = rewriteRequest(body, [rule()]);
  assert.equal(body.request.contents[0].parts[0].text, 'old');
  assert.deepEqual(body.request.contents[1].parts, [{ text: 'new' }, { text: 'old', thought: true }]);
  assert.equal(body.request.cachedContent, undefined);
  assert.equal(result.evidence[0].state, 'rewritten');
});
test('does not modify another conversation with identical text', () => {
  const body = request(OTHER), original = structuredClone(body);
  assert.equal(rewriteRequest(body, [rule()]).changed, false); assert.deepEqual(body, original);
});
test('uses occurrence to distinguish repeated identical replies', () => {
  const body = request(); body.request.contents.push({ role: 'model', parts: [{ text: 'old' }] });
  rewriteRequest(body, [rule({ occurrence: 1, matchCount: 2 })]);
  assert.equal(body.request.contents[1].parts[0].text, 'old');
  assert.equal(body.request.contents[2].parts[0].text, 'new');
});
test('preserves mapping when identical replies are appended later', () => {
  const body = request(); body.request.contents.push({ role: 'model', parts: [{ text: 'old' }] });
  rewriteRequest(body, [rule({ matchCount: 2 })]); assert.equal(body.request.contents[1].parts[0].text, 'new');
  assert.equal(body.request.contents[2].parts[0].text, 'old');
});
test('blocks ambiguous truncation rather than editing the wrong reply', () => {
  assert.throws(() => rewriteRequest(request(), [rule({ occurrence: 1, matchCount: 2 })]), /重复回复/);
});
test('rejects unknown or inconsistent request identity', () => {
  assert.throws(() => rewriteRequest({ request: {} }, [rule()]), /无法识别/);
  const body = request(); body.request.labels.cascade_id = OTHER;
  assert.throws(() => rewriteRequest(body, [rule()]), /不一致/);
});
test('blocks omitted context rather than trusting a possibly stale summary', () => {
  const body = request(); body.request.contents = [];
  assert.throws(() => rewriteRequest(body, [rule()]), /截断、压缩或包装/);
});
test('rewrites a whole response split across text parts', () => {
  const body = request(); body.request.contents[1].parts = [{ text: 'o', thoughtSignature: 'a' }, { text: 'ld' }];
  rewriteRequest(body, [rule()]); assert.deepEqual(body.request.contents[1].parts, [{ text: 'new' }]);
});
test('preserves message order when split and whole copies coexist', () => {
  const body = request(); body.request.contents[1].parts = [{ text: 'o' }, { text: 'ld' }];
  body.request.contents.push({ role: 'model', parts: [{ text: 'old' }] });
  rewriteRequest(body, [rule({ matchCount: 2 })]);
  assert.deepEqual(body.request.contents[1].parts, [{ text: 'new' }]);
  assert.equal(body.request.contents[2].parts[0].text, 'old');
});
test('blocks wrapped old text and unrecognized generation routes', () => {
  const body = request(); body.request.contents[1].parts = [{ text: '<reply>old</reply>' }];
  assert.throws(() => rewriteRequest(body, [rule()]), /包装/);
  assert.throws(() => routeKind('/v1internal:futureModelPath', true), /尚未适配/);
  assert.equal(routeKind('/v1internal:fetchAvailableModels', true), 'control');
  assert.equal(routeKind('/v1internal:listExperiments', true), 'control');
  assert.equal(routeKind('/v1internal:streamGenerateContent?alt=sse', true), 'generation');
});
