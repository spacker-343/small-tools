'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const ID = '11111111-1111-4111-8111-111111111111';

test('accepts a live /c/id chat, persists an edit, and never quits the app', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'agy-main-test-'));
  fs.writeFileSync(path.join(directory, 'renderer.js'), 'void 0;');
  const calls = [], handlers = new Map(), application = new EventEmitter();
  let quit = false;
  application.quit = () => { quit = true; };
  const steps = [{ type: 'CORTEX_STEP_TYPE_PLANNER_RESPONSE', status: 'CORTEX_STEP_STATUS_DONE',
    plannerResponse: { messageId: 'bot-a', response: 'original' } }];
  const fakeHTTPS = { request(url, options, callback) {
    const request = new EventEmitter(); request.setTimeout = () => request;
    request.end = body => {
      const method = url.split('/').at(-1), data = JSON.parse(body); calls.push({ method, data });
      const response = new EventEmitter(); response.statusCode = 200;
      queueMicrotask(() => {
        callback(response);
        response.emit('data', Buffer.from(JSON.stringify(method === 'GetCascadeTrajectorySteps' ? { steps: data.stepOffset ? [] : steps } : {})));
        response.emit('end');
      });
    };
    return request;
  } };
  const electron = { app: application, ipcMain: { removeHandler: key => handlers.delete(key), handle: (key, handler) => handlers.set(key, handler) },
    webContents: { getAllWebContents: () => [] }, webFrameMain: {} };
  const lockFile = path.join(directory, 'update_lock.json');
  fs.writeFileSync(lockFile, JSON.stringify({ locked: true }));
  const prevLockEnv = process.env.AGY_UPDATE_GUARD_LOCK_FILE;
  process.env.AGY_UPDATE_GUARD_LOCK_FILE = lockFile;
  const module = { exports: {} };
  const context = vm.createContext({ module, exports: module.exports, __dirname: directory, process, console, Buffer, URL,
    require: name => name === 'electron' ? electron : name === 'node:https' ? fakeHTTPS : name === './proxy.cjs' ? {
      createProxy: () => ({ once() {}, listen(_port, _host, callback) { callback(); }, address: () => ({ port: 23456 }), close() {} }),
    } : name.startsWith('./') ? require(path.join(__dirname, '..', name)) : require(name) });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../main.cjs'), 'utf8'), context);
  let runtime;
  try {
    runtime = module.exports.start({ getLsBaseUrl: () => 'https://127.0.0.1:12345', getLsPort: () => 12345,
      getLsCsrfToken: () => 'test-token', getLsProcess: () => ({ pid: 42, spawnargs: ['server', '--cloud_code_endpoint', 'https://example.com'] }) });
    const handler = handlers.get('agy-chat-editor:invoke');
    const frame = { url: 'https://127.0.0.1:12345/c/' + ID };
    const event = { senderFrame: frame, sender: { mainFrame: frame } };
    const result = await handler(event, { action: 'save', request: { index: 0, cascadeId: ID, viewCascadeId: ID,
      messageId: 'bot-a', original: 'original', text: 'edited' } });
    assert.equal(result.rules[0].text, 'edited'); assert.equal(quit, false);
    const listed = await handler(event, { action: 'list' });
    assert.equal(listed.updateLocked, true);
    fs.writeFileSync(lockFile, JSON.stringify({ locked: false }));
    const listedUnlocked = await handler(event, { action: 'list' });
    assert.equal(listedUnlocked.updateLocked, false);
    assert.equal(JSON.parse(fs.readFileSync(path.join(directory, '.local/edits.json')))[0].cascadeId, ID);
    assert.ok(calls.some(call => call.method === 'SetCloudCodeURL'));
    const bad = { senderFrame: { url: 'https://evil.example/c/' + ID }, sender: { mainFrame: frame } };
    await assert.rejects(handler(bad, { action: 'list' }), /禁止/);
    const reverted = await handler(event, { action: 'save', request: { index: 0, cascadeId: ID,
      messageId: 'bot-a', original: 'original', text: 'original' } });
    assert.equal(reverted.rules.length, 0);
    steps.push({ status: 'CORTEX_STEP_STATUS_DONE', metadata: { modelApiContentId: 'native-content-a' },
      plannerResponse: { response: 'native reply without messageId' } });
    const native = await handler(event, { action: 'save', request: { index: 1, cascadeId: ID, viewCascadeId: ID,
      messageId: 'content:native-content-a', original: 'native reply without messageId', text: '**edited native reply**' } });
    assert.equal(native.rules[0].messageId, 'content:native-content-a');
    assert.equal(native.rules[0].text, '**edited native reply**');
    assert.equal(quit, false);
    const duplicate = id => ({ status: 'CORTEX_STEP_STATUS_DONE', plannerResponse: { messageId: id, response: 'original' } });
    const refreshed = module.exports.refreshRule([duplicate('bot-a'), duplicate('bot-b'), duplicate('bot-c')],
      { cascadeId: ID, messageId: 'bot-a', original: 'original', text: 'edited', occurrence: 0, matchCount: 2 });
    assert.equal(refreshed.matchCount, 3);
    const { rewriteRequest } = require('../proxy.cjs');
    assert.throws(() => rewriteRequest({ requestId: 'agent/' + ID + '/turn', request: { labels: { cascade_id: ID },
      contents: [{ role: 'model', parts: [{ text: 'original' }] }, { role: 'model', parts: [{ text: 'original' }] }] } }, [refreshed]), /重复回复/);
  } finally {
    if (prevLockEnv === undefined) delete process.env.AGY_UPDATE_GUARD_LOCK_FILE;
    else process.env.AGY_UPDATE_GUARD_LOCK_FILE = prevLockEnv;
    if (runtime) await runtime.dispose();
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
