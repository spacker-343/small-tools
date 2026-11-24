'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const script = fs.readFileSync(path.join(__dirname, '../renderer.js'), 'utf8');

test('renders headings, emphasis, lists, quotes, code, tables, and file links', () => {
  const dom = new JSDOM('', { runScripts: 'outside-only' }); const w = dom.window;
  w.eval(script);
  const source = '### 当前工作区状态\n\n**粗体**、*斜体*、~~删除~~ 和 `git status`\n\n1. 第一项\n2. 第二项\n\n- 子项\n\n> 引用\n\n```cpp\nint x = 1 < 2;\n```\n\n| 名称 | 数值 |\n| --- | --- |\n| x | 1 |\n\n[正式版](file:///C:/Projects/demo/main.c#L2428-L2432)';
  w.document.body.innerHTML = w.AGYChatEditorBundle.renderMarkdown(source);
  assert.equal(w.document.querySelector('h3').textContent, '当前工作区状态');
  assert.equal(w.document.querySelector('strong').textContent, '粗体');
  assert.equal(w.document.querySelector('em').textContent, '斜体');
  assert.equal(w.document.querySelector('s').textContent, '删除');
  assert.equal(w.document.querySelector('p code').textContent, 'git status');
  assert.equal(w.document.querySelectorAll('ol li').length, 2);
  assert.equal(w.document.querySelector('ul li').textContent, '子项');
  assert.equal(w.document.querySelector('blockquote p').textContent, '引用');
  assert.equal(w.document.querySelector('pre code.language-cpp').textContent, 'int x = 1 < 2;\n');
  assert.equal(w.document.querySelectorAll('table th').length, 2);
  assert.equal(w.document.querySelector('a').getAttribute('href'), 'file:///C:/Projects/demo/main.c#L2428-L2432');
  w.close();
});

test('does not turn raw HTML or unsafe Markdown URLs into executable content', () => {
  const dom = new JSDOM('', { runScripts: 'outside-only' }); const w = dom.window; w.eval(script);
  const source = '<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n[bad](javascript:alert(1))\n\n[network file](file://example.com/share/a.txt)\n\n![image](https://example.com/a.png "safe title")';
  w.document.body.innerHTML = w.AGYChatEditorBundle.renderMarkdown(source);
  assert.equal(w.document.querySelector('script'), null);
  assert.equal(w.document.querySelector('[onerror]'), null);
  assert.equal(w.document.querySelector('a[href^="javascript:"]'), null);
  assert.equal(w.document.querySelector('a[href^="file://example"]'), null);
  assert.equal(w.document.querySelector('img').getAttribute('src'), 'https://example.com/a.png');
  w.close();
});

test('saving renders Markdown while reopening and sending preserve the exact source', async () => {
  const dom = new JSDOM('<div data-step-index="7">original</div>', { pretendToBeVisual: true, runScripts: 'outside-only' });
  const w = dom.window, calls = []; let rules = [];
  const bubble = w.document.querySelector('[data-step-index]');
  bubble.__reactFiber$test = { memoizedProps: { step: { status: 3,
    metadata: { sourceTrajectoryStepInfo: { stepIndex: 7 } },
    step: { case: 'plannerResponse', value: { response: 'original', messageId: 'bot-a' } } } } };
  w.agyChatEditor = { invoke: async data => {
    calls.push(data);
    if (data.action === 'save') rules = [{ key: 'undefined/bot-a', text: data.request.text }];
    return { ok: true, rules };
  } };
  w.eval(script); bubble.querySelector('.agy-edit-button').click();
  const source = '### 标题\n\n**重点** `x + 1`\n\n```text\n<keep exact>\n```';
  bubble.querySelector('textarea').value = source;
  await bubble.querySelector('.agy-edit-dialog button').onclick();
  const preview = bubble.querySelector('.agy-markdown');
  assert.equal(preview.querySelector('h3').textContent, '标题');
  assert.equal(preview.querySelector('strong').textContent, '重点');
  assert.equal(preview.querySelector('pre code').textContent, '<keep exact>\n');
  assert.equal(calls.find(call => call.action === 'save').request.text, source);
  assert.equal(w.document.querySelector('.agy-edit-notice'), null);
  bubble.querySelector('.agy-edit-button').click();
  assert.equal(bubble.querySelector('textarea').value, source);
  w.dispatchEvent(new w.Event('pagehide')); w.close();
});
