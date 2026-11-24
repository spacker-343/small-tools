(() => {
  'use strict';
  function findResponse(element, index) {
    const key = Object.keys(element).find(key => key.startsWith('__reactFiber$'));
    let fiber = key && element[key]; const seen = new Set();
    function inspect(value, depth = 0) {
      if (!value || typeof value !== 'object' || depth > 5 || seen.has(value)) return null;
      seen.add(value);
      if (value.step?.case === 'plannerResponse' && value.step.value?.response && value.step.value.messageId &&
          value.status === 3 && value.metadata?.sourceTrajectoryStepInfo?.stepIndex === index) {
        const source = value.metadata.sourceTrajectoryStepInfo;
        const view = element.closest('[data-testid="conversation-view"], [data-testid="subagent-tab-conversation"], [data-testid="side-question-tab-conversation"]');
        const route = /^\/c\/([0-9a-f-]{36})/.exec(element.ownerDocument.defaultView.location.pathname)?.[1];
        return { index, messageId: value.step.value.messageId, original: value.step.value.response,
          ...(source.cascadeId && { cascadeId: source.cascadeId }), ...(source.trajectoryId && { trajectoryId: source.trajectoryId }),
          ...((view?.dataset.cascadeId || route) && { viewCascadeId: view?.dataset.cascadeId || route }) };
      }
      const children = Array.isArray(value) ? value : ['step', 'steps', 'node', 'content', 'lastTextResponse', 'children', 'props'].map(key => value[key]);
      for (const child of children) { const found = inspect(child, depth + 1); if (found) return found; }
      return null;
    }
    for (let count = 0; fiber && count < 14; count++, fiber = fiber.return) {
      const found = inspect(fiber.memoizedProps); if (found) return found;
      if (fiber.memoizedProps?.['data-step-index'] !== undefined && Number(fiber.memoizedProps['data-step-index']) !== index) break;
    }
    return null;
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { findResponse };
  if (typeof window === 'undefined') return;
  let bridge; try { bridge = window.agyChatEditor || window.parent.agyChatEditor; } catch { return; }
  if (!bridge || window.__agyLiveEditorLoaded) return;
  window.__agyLiveEditorLoaded = true;
  let active = true, rules = [], scheduled = false, frameId;
  // Hot migration from v1: preserve the draft and remove only its own editor nodes.
  const drafts = [];
  for (const old of document.querySelectorAll('.agy-edit-dialog')) {
    const bubble = old.closest('[data-step-index]'), text = old.querySelector('textarea')?.value;
    if (bubble && text !== undefined) drafts.push({ bubble, text });
    old.querySelectorAll('button')[1]?.click();
    if (old.isConnected) { for (const child of bubble?.children || []) child.style.display = ''; old.remove(); }
  }
  const style = document.createElement('style'); style.id = 'agy-live-editor-style';
  style.textContent = `.agy-edit-button{font:12px sans-serif;color:inherit;background:transparent;border:1px solid #7776;border-radius:6px;padding:3px 8px;cursor:pointer;align-self:flex-start;flex-shrink:0;margin:4px}.agy-edit-dialog,.agy-edited-preview{box-sizing:border-box;background:#1c1e22;color:#eee;border:1px solid #666;border-radius:10px;width:100%;min-width:0;flex:1;padding:14px}.agy-edit-dialog textarea{box-sizing:border-box;width:100%;height:300px;max-height:60vh;background:#121417;color:#eee;border:1px solid #666;border-radius:6px;padding:12px;font:14px/1.6 monospace;resize:vertical}.agy-edit-dialog button{padding:7px 12px;margin:10px 8px 0 0;cursor:pointer}.agy-edit-dialog p,.agy-edited-preview small{font:12px/1.6 sans-serif;white-space:pre-wrap}.agy-edited-preview div{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.agy-native-hidden{display:none!important}.agy-edit-notice{position:fixed;bottom:30px;right:24px;z-index:2147483647;max-width:420px;background:#20262f;color:#eee;border:1px solid #8792a3;border-radius:8px;padding:14px;font:13px/1.6 sans-serif}`;
  document.head.append(style);
  const keyOf = identity => (identity.viewCascadeId || identity.cascadeId) + '/' + identity.messageId;
  for (const notice of document.querySelectorAll('.agy-edit-notice')) notice.remove();
  function preview(bubble, identity) {
    if (bubble.querySelector(':scope > .agy-edit-dialog')) return;
    const rule = rules.find(rule => rule.key === keyOf(identity));
    let node = bubble.querySelector(':scope > .agy-edited-preview');
    node?.querySelector(':scope > small')?.remove();
    if (!rule) {
      node?.remove(); for (const child of bubble.children) child.classList.remove('agy-native-hidden'); return;
    }
    for (const child of bubble.children) if (!child.matches('.agy-edit-button,.agy-edited-preview')) child.classList.add('agy-native-hidden');
    if (!node) {
      node = document.createElement('div'); node.className = 'agy-edited-preview';
      node.append(document.createElement('div')); bubble.prepend(node);
    }
    if (node.lastChild.textContent !== rule.text) node.lastChild.textContent = rule.text;
  }
  function open(bubble, draft) {
    if (bubble.querySelector(':scope > .agy-edit-dialog')) return;
    const index = Number(bubble.dataset.stepIndex), identity = findResponse(bubble, index);
    if (!identity) return;
    const rule = rules.find(rule => rule.key === keyOf(identity));
    const editor = document.createElement('section'); editor.className = 'agy-edit-dialog'; editor.setAttribute('aria-label', '编辑 AI 回复');
    const title = document.createElement('h3'); title.textContent = '编辑 AI 回复';
    const info = document.createElement('p'); info.hidden = true;
    const textarea = document.createElement('textarea'); textarea.value = draft ?? rule?.text ?? identity.original;
    textarea.setAttribute('aria-label', 'AI 回复正文');
    const save = document.createElement('button'); save.textContent = '保存';
    const cancel = document.createElement('button'); cancel.textContent = '取消';
    const hidden = [...bubble.children].map(node => [node, node.style.display]);
    for (const [node] of hidden) node.style.display = 'none';
    const close = () => { for (const [node, display] of hidden) node.style.display = display; editor.remove(); };
    cancel.onclick = () => { close(); scan(); };
    save.onclick = async () => {
      save.disabled = cancel.disabled = textarea.disabled = true;
      try {
        const current = bubble.isConnected && findResponse(bubble, index);
        if (current?.messageId !== identity.messageId || current.original !== identity.original || keyOf(current) !== keyOf(identity)) throw new Error('会话或消息已变化，请重新编辑');
        const result = await bridge.invoke({ action: 'save', request: { ...identity, text: textarea.value } });
        rules = result.rules; close(); scan();
      } catch (error) {
        const message = String(error.message || error).replace(/^Error invoking remote method '[^']+':\s*(?:Error:\s*)?/, '');
        info.textContent = '保存失败：' + (message.length > 100 ? message.slice(0, 100) + '…' : message);
        info.hidden = false; save.disabled = cancel.disabled = textarea.disabled = false;
      }
    };
    editor.append(title, info, textarea, save, cancel); bubble.append(editor); textarea.focus();
  }
  function scan() {
    for (const bubble of document.querySelectorAll('[data-step-index]')) {
      const identity = findResponse(bubble, Number(bubble.dataset.stepIndex)); if (!identity) continue;
      const parent = bubble.parentElement?.closest('[data-step-index]');
      if (parent?.dataset.stepIndex === bubble.dataset.stepIndex && findResponse(parent, Number(parent.dataset.stepIndex))) continue;
      let button = bubble.querySelector(':scope > .agy-edit-button');
      if (button?.dataset.agyLive !== '2') { button?.remove(); button = null; }
      if (!button) {
        button = document.createElement('button'); button.className = 'agy-edit-button'; button.dataset.agyLive = '2';
        button.textContent = '编辑'; button.setAttribute('aria-label', '编辑这条 AI 回复');
        button.onclick = event => { event.stopPropagation(); open(bubble); }; bubble.append(button);
      }
      preview(bubble, identity);
    }
  }
  const observer = new MutationObserver(() => {
    if (scheduled || !active) return; scheduled = true;
    frameId = requestAnimationFrame(() => { scheduled = false; if (active) scan(); });
  });
  observer.observe(document.body, { childList: true, subtree: true });
  async function refresh() {
    try {
      const result = await bridge.invoke({ action: 'list' }); if (!active) return;
      rules = result.rules || []; scan();
    } catch { /* Saving reports connection errors inline; background refresh stays silent. */ }
  }
  scan(); void refresh(); for (const draft of drafts) open(draft.bubble, draft.text);
  const timer = setInterval(() => { if (!document.querySelector('.agy-edit-dialog')) void refresh(); }, 6000);
  const dispose = () => { active = false; observer.disconnect(); cancelAnimationFrame(frameId); clearInterval(timer); };
  window.addEventListener('pagehide', dispose, { once: true });
  window.__agyLiveEditorDispose = dispose;
})();
