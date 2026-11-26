'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { buildEditPlan, applyRebuiltSummary } = require('../history.cjs');
const ID = '11111111-1111-4111-8111-111111111111';
const assistant = (messageId, response) => ({ status: 'CORTEX_STEP_STATUS_DONE', plannerResponse: { messageId, response, thinking: 'PRIVATE_THINKING' } });
const checkpoint = (summary, end) => ({ status: 'CORTEX_STEP_STATUS_DONE', checkpoint: { sessionSummary: summary, includedStepIndexEnd: end } });
const rule = (messageId, original, text) => ({ key: ID + '/' + messageId, cascadeId: ID, messageId, original, text });
const request = summary => ({ requestId: 'agent/' + ID + '/turn', request: { labels: { cascade_id: ID },
  cachedContent: 'old-cache', contents: [{ role: 'user', parts: [{ text: '<CONTEXT_SUMMARY>' + summary + '</CONTEXT_SUMMARY>' }] },
    { role: 'user', parts: [{ text: 'Continue the task.' }] }] } });

test('native content identity supports direct rewriting and survives compaction', () => {
  const step = { status: 'CORTEX_STEP_STATUS_DONE', metadata: { modelApiContentId: 'native-content-a' },
    plannerResponse: { response: 'OLD_NATIVE_REPLY' } };
  const edit = rule('content:native-content-a', 'OLD_NATIVE_REPLY', '**EDITED_NATIVE_REPLY**');
  const direct = buildEditPlan([step], [edit], request('unrelated'));
  const body = { requestId: 'agent/' + ID + '/turn', request: { contents: [{ role: 'model', parts: [{ text: 'OLD_NATIVE_REPLY' }] }] } };
  require('../proxy.cjs').rewriteRequest(body, direct.rules);
  assert.equal(body.request.contents[0].parts[0].text, '**EDITED_NATIVE_REPLY**');
  const compacted = buildEditPlan([step, checkpoint('OLD_SUMMARY', 1)], [edit], request('OLD_SUMMARY'));
  assert.deepEqual(compacted.compaction.records, [{ step: 0, kind: 'assistant', messageId: 'content:native-content-a',
    edited: true, text: '**EDITED_NATIVE_REPLY**' }]);
  const ambiguous = { ...step, plannerResponse: { response: 'OLD_NATIVE_REPLY' } };
  assert.throws(() => buildEditPlan([step, ambiguous], [edit], request('unrelated')), /无法可靠应用/);
});

test('recognizes native summary preamble and preserves checkpoint summary tags', () => {
  for (const summary of ['<summary>native summary</summary>', '<summary>truncated native summary']) {
    const body = request('unused');
    body.request.contents[0].parts[0].text = '<CONTEXT_SUMMARY>\nThe following is a summary of the conversation history that has been truncated to fit within the context window:\n\n' + summary + '\n</CONTEXT_SUMMARY>';
    const plan = buildEditPlan([assistant('a', 'old'), checkpoint(summary, 1)], [rule('a', 'old', 'new')], body);
    assert.equal(plan.rules.length, 0);
    assert.equal(plan.compaction.originalSummary, summary);
    applyRebuiltSummary(body, plan.compaction, 'corrected');
    assert.ok(body.request.contents[0].parts[0].text.includes('corrected'));
    assert.ok(!body.request.contents[0].parts[0].text.includes(summary));
  }
});

test('does not match a checkpoint quoted inside unrelated summary prose', () => {
  const body = request('unrelated prefix <summary>old summary</summary> unrelated suffix');
  const plan = buildEditPlan([assistant('a', 'old'), checkpoint('old summary', 1)], [rule('a', 'old', 'new')], body);
  assert.equal(plan.compaction, undefined);
  const quotedHeading = request('user quotation\n# Previous Session Summary:\nold summary');
  assert.equal(buildEditPlan([assistant('a', 'old'), checkpoint('old summary', 1)],
    [rule('a', 'old', 'new')], quotedHeading).compaction, undefined);
});

test('a complete checkpoint field wins over newer heading or wrapper substrings', () => {
  for (const summary of ['# Previous Session Summary:\nabc', '<summary>abc</summary>']) {
    const body = request(summary);
    const steps = [assistant('a', 'old'), checkpoint(summary, 1), checkpoint('abc', 2)];
    const plan = buildEditPlan(steps, [rule('a', 'old', 'new')], body);
    assert.equal(plan.compaction.checkpoint, 1);
    applyRebuiltSummary(body, plan.compaction, 'corrected');
    assert.ok(!body.request.contents[0].parts[0].text.includes(summary));
  }
});

test('rebuild plan applies edits before summarization and does not reuse an unsafe summary', () => {
  const steps = [assistant('a', 'OLD_UNIQUE_MARKER'), checkpoint('STALE_SUMMARY', 1), assistant('b', 'recent')];
  const before = structuredClone(steps);
  const plan = buildEditPlan(steps, [rule('a', 'OLD_UNIQUE_MARKER', '**NEW_UNIQUE_MARKER**')], request('STALE_SUMMARY'));
  const serialized = JSON.stringify(plan.compaction.records);
  assert.equal(plan.rules.length, 0);
  assert.ok(serialized.includes('**NEW_UNIQUE_MARKER**'));
  assert.ok(!serialized.includes('OLD_UNIQUE_MARKER'));
  assert.ok(!serialized.includes('STALE_SUMMARY'));
  assert.ok(!serialized.includes('PRIVATE_THINKING'));
  assert.deepEqual(steps, before);
});

test('uses only a checkpoint before all edits as a safe prefix', () => {
  const steps = [assistant('a', 'first'), checkpoint('SAFE_PREFIX', 1), assistant('b', 'old'),
    checkpoint('UNSAFE_MIDDLE', 3), assistant('c', 'after'), checkpoint('CURRENT_STALE', 5)];
  const plan = buildEditPlan(steps, [rule('b', 'old', 'new')], request('CURRENT_STALE'));
  const history = JSON.stringify(plan.compaction.records);
  assert.ok(history.includes('SAFE_PREFIX')); assert.ok(!history.includes('UNSAFE_MIDDLE'));
  assert.ok(!history.includes('CURRENT_STALE')); assert.ok(history.includes('after'));
});

test('matches the summary actually used by the request despite a newer checkpoint', () => {
  const steps = [assistant('a', 'old'), checkpoint('FIRST_SUMMARY', 1), assistant('b', 'recent'), checkpoint('NEWER_SUMMARY', 3)];
  const plan = buildEditPlan(steps, [rule('a', 'old', 'new')], request('FIRST_SUMMARY'));
  assert.equal(plan.compaction.checkpoint, 1);
});

test('keeps post-summary edits as ordinary direct replacements', () => {
  const steps = [assistant('a', 'old-a'), checkpoint('SUMMARY', 1), assistant('b', 'old-b')];
  const plan = buildEditPlan(steps, [rule('a', 'old-a', 'new-a'), rule('b', 'old-b', 'new-b')], request('SUMMARY'));
  assert.deepEqual(plan.compaction.edits.map(edit => edit.messageId), ['a']);
  assert.equal(plan.rules[0].messageId, 'b');
});

test('replacement removes the stale summary and preserves exact edited Markdown and residual context', () => {
  const body = request('STALE_SUMMARY');
  Object.assign(body.request.contents[0].parts[0], { id: 'old-provider-content-id', partMetadata: { cache: 'old' } });
  const plan = { checkpoint: 2, originalSummary: 'STALE_SUMMARY', edits: [{ key: ID + '/a', messageId: 'a', text: '### New\n\n`exact` **text**' }] };
  const evidence = applyRebuiltSummary(body, plan, 'Rebuilt corrected state.');
  assert.ok(!JSON.stringify(body).includes('STALE_SUMMARY'));
  assert.ok(body.request.contents[0].parts[0].text.includes(plan.edits[0].text));
  assert.equal(body.request.contents[1].parts[0].text, 'Continue the task.');
  assert.equal(body.request.cachedContent, undefined);
  assert.equal(body.request.contents[0].parts[0].id, undefined);
  assert.equal(body.request.contents[0].parts[0].partMetadata, undefined);
  assert.equal(evidence[0].state, 'summary-rewritten');
});

test('a full-history request stays editable when a checkpoint exists but is not used', () => {
  const steps = [assistant('a', 'old'), checkpoint('SUMMARY', 1)];
  const body = request('some unrelated user text');
  const plan = buildEditPlan(steps, [rule('a', 'old', 'new')], body);
  assert.equal(plan.compaction, undefined); assert.equal(plan.rules[0].matchCount, 1);
});

test('a user quote never selects or rewrites a checkpoint summary', () => {
  const steps = [assistant('a', 'old'), checkpoint('SUMMARY', 1), checkpoint('NEWER', 2)];
  const body = request('SUMMARY');
  body.request.contents[1].parts[0].text = 'Please explain SUMMARY and NEWER.';
  body.request.contents.push({ role: 'user', parts: [{ text: '<CONTEXT_SUMMARY>NEWER</CONTEXT_SUMMARY>' }] });
  const plan = buildEditPlan(steps, [rule('a', 'old', 'new')], body);
  assert.equal(plan.compaction.checkpoint, 1);
  const user = structuredClone(body.request.contents.slice(1));
  applyRebuiltSummary(body, plan.compaction, 'corrected');
  assert.deepEqual(body.request.contents.slice(1), user);
  const quoteOnly = request('not a checkpoint');
  quoteOnly.request.contents[1].parts[0].text = 'SUMMARY';
  assert.equal(buildEditPlan(steps, [rule('a', 'old', 'new')], quoteOnly).compaction, undefined);
});

test('checkpoint summaries that contain one another are matched as complete fields', () => {
  const steps = [assistant('a', 'old'), checkpoint('abcdef', 1), checkpoint('abc', 2)];
  const body = request('abcdef');
  body.request.contents[0].parts[0].text = '<CONTEXT_SUMMARY>\nThe following is a summary of the conversation history that has been truncated to fit within the context window:\n\n# Previous Session Summary:\nabcdef\n\n# Conversation Logs\nlogs\n</CONTEXT_SUMMARY>';
  const plan = buildEditPlan(steps, [rule('a', 'old', 'new')], body);
  assert.equal(plan.compaction.checkpoint, 1);
  applyRebuiltSummary(body, plan.compaction, 'corrected');
  assert.ok(!body.request.contents[0].parts[0].text.includes('def'));
});
