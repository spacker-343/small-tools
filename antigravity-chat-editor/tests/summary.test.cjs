'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createSummaryEngine, extractText, chunks } = require('../summary.cjs');

test('rebuilds once from corrected history, deduplicates callers, and persists the cache', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'agy-summary-'));
  const file = path.join(directory, 'cache.json'); const prompts = [];
  const generateText = async (_upstream, _body, _headers, prompt) => { prompts.push(prompt); return 'Corrected summary.'; };
  const plan = { fingerprint: 'corrected-history-v1', checkpoint: 4, records: [{ kind: 'assistant', edited: true, text: '**NEW_ONLY**' }] };
  const context = { body: { model: 'test-model' }, headers: { authorization: 'SECRET_TOKEN' } };
  try {
    const engine = createSummaryEngine({ upstream: 'https://example.com', cacheFile: file, generateText });
    const [first, second] = await Promise.all([engine.rebuild(plan, context), engine.rebuild(plan, context)]);
    assert.equal(first.summary, 'Corrected summary.'); assert.equal(second.summary, first.summary);
    assert.equal(prompts.length, 1); assert.ok(prompts[0].includes('**NEW_ONLY**'));
    assert.ok(!fs.readFileSync(file, 'utf8').includes('SECRET_TOKEN'));
    const restarted = createSummaryEngine({ upstream: 'https://example.com', cacheFile: file,
      generateText: async () => { throw new Error('cache should have been used'); } });
    assert.equal((await restarted.rebuild(plan, context)).cached, true);
    await engine.rebuild({ ...plan, fingerprint: 'corrected-history-v2' }, context);
    assert.equal(prompts.length, 2);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('stream extraction ignores thinking and accepts SSE or JSON envelopes', () => {
  const first = { response: { candidates: [{ content: { parts: [{ text: 'hidden', thought: true }, { text: 'Fresh ' }] } }] } };
  const second = { response: { candidates: [{ content: { parts: [{ text: 'summary.' }] } }] } };
  assert.equal(extractText(Buffer.from('data: ' + JSON.stringify(first) + '\n\ndata: ' + JSON.stringify(second) + '\n\ndata: [DONE]\n\n'), {}), 'Fresh summary.');
  assert.equal(extractText(Buffer.from(JSON.stringify([first, second])), {}), 'Fresh summary.');
  assert.throws(() => extractText(Buffer.from('{"error":{"message":"failed"}}'), {}), /failed/);
});

test('chunking preserves edited message characters even when a record is large', () => {
  const records = [{ kind: 'assistant', edited: true, text: 'x'.repeat(250) }];
  assert.equal(chunks(records, 100).join(''), JSON.stringify(records[0]) + '\n');
});

test('later compaction reuses a corrected prefix instead of resummarizing the old history', async () => {
  const prompts = [], context = { body: { model: 'test-model' }, headers: {} };
  const engine = createSummaryEngine({ upstream: 'https://example.com', generateText: async (_u, _b, _h, prompt) => { prompts.push(prompt); return 'Corrected prefix state.'; } });
  const first = { fingerprint: 'first', scope: 'same-conversation', historyStart: 0, coveredEnd: 2,
    checkpoint: 2, edits: [{ text: 'EDITED_EXACT' }], records: [{ step: 0, kind: 'user', text: 'EARLIER_USER' },
      { step: 1, kind: 'assistant', text: 'EDITED_EXACT' }] };
  await engine.rebuild(first, context);
  await engine.rebuild({ ...first, fingerprint: 'later', coveredEnd: 4, checkpoint: 4,
    records: [...first.records, { step: 3, kind: 'user', text: 'NEW_USER' }] }, context);
  assert.equal(prompts.length, 2);
  assert.ok(prompts[1].includes('Corrected prefix state.'));
  assert.ok(prompts[1].includes('EDITED_EXACT'));
  assert.ok(prompts[1].includes('NEW_USER'));
  assert.ok(!prompts[1].includes('EARLIER_USER'));
});

test('an interrupted multi-part rebuild resumes from a completed segment', async () => {
  const prompts = []; let failed = false;
  const engine = createSummaryEngine({ upstream: 'https://example.com', generateText: async (_u, _b, _h, prompt) => {
    prompts.push(prompt); if (prompts.length === 2 && !failed) { failed = true; throw new Error('temporary failure'); } return 'Progress summary.';
  } });
  const plan = { fingerprint: 'large-history', checkpoint: 3, records: [{ step: 0, text: 'x'.repeat(150000) }] };
  const context = { body: { model: 'test-model' }, headers: {} };
  await assert.rejects(engine.rebuild(plan, context), /temporary failure/);
  const result = await engine.rebuild(plan, context);
  assert.equal(result.summary, 'Progress summary.'); assert.equal(prompts.length, 3);
  assert.ok(prompts[2].includes('Progress summary.'));
});
