'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const React = require('react');
const { createRoot } = require('react-dom/client');
const { flushSync } = require('react-dom');
const { findResponse } = require('../renderer.js');
const script = fs.readFileSync(path.join(__dirname, '../renderer.js'), 'utf8');

test('reads Markdown and identity from the correct React owner', () => {
  const dom = new JSDOM('<div id="root"></div>', { pretendToBeVisual: true, runScripts: 'outside-only' });
  const previousWindow = global.window, previousDocument = global.document;
  global.window = dom.window; global.document = dom.window.document;
  try {
    const step = { status: 3, metadata: { modelApiContentId: 'content-also-present', sourceTrajectoryStepInfo: { stepIndex: 7 } },
      step: { case: 'plannerResponse', value: { response: '**原始**\n\n代码', messageId: 'bot-a' } } };
    function Response({ step }) {
      return React.createElement('div', { 'data-bubble-side': 'agent', 'data-step-index': 7 }, '原始 代码');
    }
    const root = createRoot(document.querySelector('#root'));
    flushSync(() => root.render(React.createElement(Response, { step })));
    const bubble = document.querySelector('[data-bubble-side]');
    assert.deepEqual(findResponse(bubble, 7), { index: 7, messageId: 'bot-a', original: '**原始**\n\n代码' });
    assert.equal(findResponse(bubble, 8), null);
    root.unmount();
  } finally { dom.window.close(); global.window = previousWindow; global.document = previousDocument; }
});

test('injects bubble button, edits source, and sends the replacement to the native bridge', async () => {
  const dom = new JSDOM('<div data-bubble-side="agent" data-step-index="7">rendered message</div>',
    { pretendToBeVisual: true, runScripts: 'outside-only' });
  const w = dom.window, calls = [];
  w.agyChatEditor = { invoke: async data => {
    calls.push(data);
    return { ok: true, rules: data.action === 'save' ? [{ key: 'undefined/bot-a', text: data.request.text }] : [] };
  } };
  const bubble = w.document.querySelector('div');
  bubble.__reactFiber$test = { memoizedProps: { step: { status: 3,
    metadata: { sourceTrajectoryStepInfo: { stepIndex: 7 } },
    step: { case: 'plannerResponse', value: { response: '**original**', messageId: 'bot-a' } } } } };
  w.eval(script);
  bubble.querySelector('.agy-edit-button').click();
  const textarea = w.document.querySelector('textarea');
  assert.equal(textarea.value, '**original**');
  textarea.value = '新回复';
  assert.equal(textarea.closest('[data-step-index]'), bubble);
  w.document.querySelector('.agy-edit-dialog button').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  const save = calls.find(call => call.action === 'save');
  assert.equal(save.request.text, '新回复');
  assert.equal(save.request.original, '**original**');
  assert.equal(save.request.messageId, 'bot-a');
  assert.equal(w.document.querySelector('.agy-markdown p').textContent, '新回复');
  assert.equal(w.document.querySelector('.agy-edit-dialog'), null);
  assert.equal(w.document.querySelector('.agy-edit-notice'), null);
  w.eval(script);
  assert.equal(w.document.querySelectorAll('.agy-edit-button').length, 1);
  w.dispatchEvent(new w.Event('pagehide'));
  dom.window.close();
});

test('only reports a save failure inline and keeps background failures silent', async () => {
  const dom = new JSDOM('<div data-step-index="7">body</div><div class="agy-edit-notice">old success</div>',
    { pretendToBeVisual: true, runScripts: 'outside-only' });
  const w = dom.window;
  const bubble = w.document.querySelector('[data-step-index]');
  bubble.__reactFiber$test = { memoizedProps: { step: { status: 3,
    metadata: { sourceTrajectoryStepInfo: { stepIndex: 7 } },
    step: { case: 'plannerResponse', value: { response: 'original', messageId: 'bot-a' } } } } };
  w.agyChatEditor = { invoke: async data => { throw new Error(data.action === 'save' ?
    "Error invoking remote method 'agy-chat-editor:invoke': Error: 服务未就绪" : 'background failure'); } };
  w.eval(script); await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(w.document.querySelector('.agy-edit-notice'), null);
  bubble.querySelector('.agy-edit-button').click();
  assert.equal(bubble.querySelector('.agy-edit-dialog p').hidden, true);
  await bubble.querySelector('.agy-edit-dialog button').onclick();
  assert.equal(bubble.querySelector('.agy-edit-dialog p').textContent, '保存失败：服务未就绪');
  assert.equal(bubble.querySelector('.agy-edit-dialog p').hidden, false);
  assert.equal(w.document.querySelector('.agy-edit-notice'), null);
  w.dispatchEvent(new w.Event('pagehide')); w.close();
});

test('refuses unsupported UI structure instead of editing DOM text', async () => {
  const dom = new JSDOM('<div data-bubble-side="agent" data-step-index="2">unchanged</div>',
    { pretendToBeVisual: true, runScripts: 'outside-only' });
  dom.window.agyChatEditor = { invoke: async () => ({ rules: [] }) };
  dom.window.eval(script);
  assert.equal(dom.window.document.querySelector('.agy-edit-dialog'), null);
  assert.equal(dom.window.document.querySelector('button'), null);
  await new Promise(resolve => setTimeout(resolve, 0));
  dom.window.dispatchEvent(new dom.window.Event('pagehide'));
  dom.window.close();
});

test('supports plain sidebar messages and keeps main and child identities separate', async () => {
  const dom = new JSDOM('<main><div data-step-index="2"></div></main><aside><div data-step-index="2"></div></aside>',
    { pretendToBeVisual: true, runScripts: 'outside-only' });
  const w = dom.window;
  w.agyChatEditor = { invoke: async () => ({ rules: [] }) };
  for (const [i, bubble] of [...w.document.querySelectorAll('[data-step-index]')].entries()) {
    bubble.__reactFiber$test = { memoizedProps: { step: { status: 3,
      metadata: { sourceTrajectoryStepInfo: { stepIndex: 2, cascadeId: `cascade-${i}`, trajectoryId: `trajectory-${i}` } },
      step: { case: 'plannerResponse', value: { response: `response-${i}`, messageId: `bot-${i}` } } } } };
  }
  w.eval(script);
  assert.equal(w.document.querySelectorAll('.agy-edit-button').length, 2);
  const sidebar = w.document.querySelector('aside [data-step-index]');
  sidebar.querySelector('button').click();
  assert.equal(sidebar.querySelector('textarea').value, 'response-1');
  assert.equal(w.document.querySelector('main textarea'), null);
  assert.equal(findResponse(sidebar, 2).cascadeId, 'cascade-1');
  sidebar.querySelectorAll('.agy-edit-dialog button')[1].click();
  assert.equal(sidebar.querySelector('.agy-edit-dialog'), null);
  await new Promise(resolve => setTimeout(resolve, 0));
  w.dispatchEvent(new w.Event('pagehide'));
  w.close();
});

test('edits a completed native reply without messageId using its content identity', async () => {
  const id = '11111111-1111-4111-8111-111111111111', contentId = 'native-content-a';
  const dom = new JSDOM('<div data-testid="conversation-view" data-cascade-id="' + id + '"><div data-step-index="3">native reply</div></div>',
    { pretendToBeVisual: true, runScripts: 'outside-only' });
  const w = dom.window, calls = [], bubble = w.document.querySelector('[data-step-index]');
  bubble.__reactFiber$test = { memoizedProps: { step: { status: 3,
    metadata: { modelApiContentId: contentId, sourceTrajectoryStepInfo: { stepIndex: 3, cascadeId: id } },
    step: { case: 'plannerResponse', value: { response: '**native** reply', modifiedResponse: '**native** reply' } } } } };
  w.agyChatEditor = { invoke: async data => {
    calls.push(data);
    return { rules: data.action === 'save' ? [{ key: id + '/content:' + contentId, text: data.request.text }] : [] };
  } };
  try {
    w.eval(script); await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(findResponse(bubble, 3).messageId, 'content:' + contentId);
    bubble.querySelector('.agy-edit-button').click();
    const textarea = bubble.querySelector('textarea');
    assert.equal(textarea.value, '**native** reply');
    textarea.value = '**updated** reply';
    await bubble.querySelector('.agy-edit-dialog button').onclick();
    assert.equal(calls.find(call => call.action === 'save').request.messageId, 'content:' + contentId);
    assert.equal(bubble.querySelector('.agy-markdown strong').textContent, 'updated');
  } finally { w.dispatchEvent(new w.Event('pagehide')); w.close(); }
});

test('keeps replies without any stable native identity or still generating uneditable', async () => {
  const dom = new JSDOM('<div data-step-index="3"></div>', { pretendToBeVisual: true, runScripts: 'outside-only' });
  const w = dom.window, bubble = w.document.querySelector('div');
  w.agyChatEditor = { invoke: async () => ({ rules: [] }) };
  const step = { status: 3, metadata: { sourceTrajectoryStepInfo: { stepIndex: 3 } },
    step: { case: 'plannerResponse', value: { response: 'native reply' } } };
  bubble.__reactFiber$test = { memoizedProps: { step } };
  try {
    w.eval(script); await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(bubble.querySelector('.agy-edit-button'), null);
    step.status = 8; step.metadata.modelApiContentId = 'native-content-a';
    assert.equal(findResponse(bubble, 3), null);
  } finally { w.dispatchEvent(new w.Event('pagehide')); w.close(); }
});
