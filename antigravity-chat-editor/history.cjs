'use strict';
const crypto = require('node:crypto');
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
function clearTextCache(part) {
  for (const key of ['thoughtSignature', 'thought_signature', 'id', 'cacheControl', 'cache_control',
    'partMetadata', 'part_metadata', 'ptoken']) delete part[key];
}
const done = step => step.status === 'CORTEX_STEP_STATUS_DONE';
const textParts = body => [...(body.request?.contents || []), body.request?.systemInstruction]
  .filter(Boolean).flatMap(content => (content.parts || []).filter(part => typeof part.text === 'string'));
function summaryOccurrences(text, summary, exactOnly = false) {
  if (!summary) return [];
  // Native compaction is an initial, dedicated context envelope. Never rewrite
  // a later user message merely because it quotes the same summary text.
  const envelope = /^\s*<CONTEXT_SUMMARY>[\s\S]*?<\/CONTEXT_SUMMARY>/.exec(text);
  if (!envelope) return [];
  let start = text.indexOf('<CONTEXT_SUMMARY>') + '<CONTEXT_SUMMARY>'.length;
  let end = envelope[0].lastIndexOf('</CONTEXT_SUMMARY>');
  const preamble = '\nThe following is a summary of the conversation history that has been truncated to fit within the context window:\n\n';
  if (text.startsWith(preamble, start)) start += preamble.length;
  const exactIndex = text.indexOf(summary, start);
  if (text.slice(start, end).trim() === summary.trim() && exactIndex >= start && exactIndex + summary.length <= end) return [exactIndex];
  if (exactOnly) return [];
  const heading = '# Previous Session Summary:\n', headingAt = text.indexOf(heading, start);
  if (headingAt >= start && headingAt < end && text.slice(start, headingAt).trim() === '') {
    start = headingAt + heading.length;
    const logs = text.lastIndexOf('\n\n# Conversation Logs', end);
    if (logs >= start) end = logs;
  }
  // Check the complete native field first: sessionSummary itself can contain
  // <summary> tags, including an unclosed tag when native output was truncated.
  const field = text.slice(start, end);
  if (field.trim() === summary.trim()) {
    const index = text.indexOf(summary, start);
    return index >= start && index + summary.length <= end ? [index] : [];
  }
  {
    const open = text.indexOf('<summary>', start), close = text.lastIndexOf('</summary>', end);
    if (text.slice(start, open).trim() === '' && open >= start && close > open && close < end &&
        text.slice(close + '</summary>'.length, end).trim() === '') {
      start = open + '<summary>'.length; end = close;
    }
  }
  // Compare the complete summary field, never a substring of another checkpoint.
  if (text.slice(start, end).trim() !== summary.trim()) return [];
  const index = text.indexOf(summary, start);
  return index >= start && index + summary.length <= end ? [index] : [];
}
function summaryParts(body) {
  const first = body.request?.contents?.[0];
  return first?.role === 'user' ? (first.parts || []).filter(part => typeof part.text === 'string') : [];
}

function locate(steps, rule) {
  const found = steps.map((step, index) => ({ step, index })).filter(({ step }) => done(step) &&
    step.plannerResponse?.messageId === rule.messageId && step.plannerResponse.response === rule.original);
  if (found.length !== 1) throw new Error('编辑的原始回复已变化，无法可靠应用修改；请重新保存该条编辑');
  return found[0].index;
}

function refreshRule(steps, rule, start = 0) {
  const target = locate(steps, rule);
  const matching = steps.map((step, index) => ({ step, index })).filter(({ step, index }) => index >= start &&
    done(step) && step.plannerResponse?.response === rule.original);
  const occurrence = matching.findIndex(item => item.index === target);
  if (occurrence < 0) throw new Error('编辑的回复不在当前未压缩历史范围内');
  return { ...rule, occurrence, matchCount: matching.length };
}

function clipped(value, limit = 6000) {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  if (!text || text.length <= limit) return text;
  const half = Math.floor(limit / 2);
  return text.slice(0, half) + '\n[tool output shortened for summary]\n' + text.slice(-half);
}

function historyRecords(steps, rules, start, end, safeSummary) {
  const edits = new Map(rules.map(rule => [rule.messageId, rule]));
  const records = safeSummary ? [{ kind: 'earlier-summary', text: safeSummary }] : [];
  for (let index = start; index < end; index++) {
    const step = steps[index];
    if (!step || !['CORTEX_STEP_STATUS_DONE', 'CORTEX_STEP_STATUS_ERROR', 'CORTEX_STEP_STATUS_CANCELED',
      'CORTEX_STEP_STATUS_INTERRUPTED', 'CORTEX_STEP_STATUS_HALTED'].includes(step.status) || step.checkpoint) continue;
    if (step.plannerResponse) {
      const value = step.plannerResponse, edit = edits.get(value.messageId);
      const text = edit?.text ?? value.response;
      if (text) records.push({ step: index, kind: 'assistant', messageId: value.messageId, edited: !!edit, text });
      for (const call of value.toolCalls || []) records.push({ step: index, kind: 'tool-call', data: clipped(call) });
    } else if (step.userInput) {
      const value = step.userInput;
      records.push({ step: index, kind: 'user', items: value.items, query: value.query,
        response: value.userResponse, artifactComments: value.artifactComments, fileComments: value.fileComments });
    } else if (step.errorMessage) {
      const error = step.errorMessage.error;
      records.push({ step: index, kind: 'error', text: error?.userMessage || error?.shortError || 'Prior execution error' });
    } else if (step.systemMessage) {
      records.push({ step: index, kind: 'system-event', text: clipped(step.systemMessage.message) });
    } else {
      const payload = Object.fromEntries(Object.entries(step).filter(([key]) =>
        !['type', 'status', 'metadata', 'permissions', 'taskDetails', 'renderInfo', 'error'].includes(key)));
      records.push({ step: index, kind: 'tool-result', type: step.type, status: step.status, data: clipped(payload) });
    }
  }
  return records;
}

function buildEditPlan(steps, rules, body) {
  const located = rules.map(rule => ({ rule, index: locate(steps, rule) }));
  const parts = summaryParts(body);
  const checkpoints = steps.flatMap((step, index) => {
    const cp = step.checkpoint;
    if (!done(step) || !cp?.sessionSummary) return [];
    const end = Number.isInteger(cp.includedStepIndexEnd) && cp.includedStepIndexEnd > 0 ? cp.includedStepIndexEnd : index;
    if (end < 0 || end > index) throw new Error('摘要覆盖范围不兼容，不能可靠应用历史编辑');
    return [{ index, end, summary: cp.sessionSummary }];
  });
  // Match the checkpoint actually present in this request, not a newer checkpoint
  // which may have appeared while the request was being assembled.
  // Prefer a complete native field over an interpretation of its heading or
  // wrapper, even if the shorter interpretation matches a newer checkpoint.
  const current = checkpoints.findLast(cp => parts.some(part => summaryOccurrences(part.text, cp.summary, true).length)) ||
    checkpoints.findLast(cp => parts.some(part => summaryOccurrences(part.text, cp.summary).length));
  if (!current) return { rules: located.map(({ rule }) => refreshRule(steps, rule)) };
  const covered = located.filter(item => item.index < current.end);
  const direct = located.filter(item => item.index >= current.end).map(({ rule }) => refreshRule(steps, rule, current.end));
  if (!covered.length) return { rules: direct };
  const earliest = Math.min(...covered.map(item => item.index));
  const safe = checkpoints.findLast(cp => cp.index < current.index && cp.end <= earliest);
  const coveredRules = covered.map(item => item.rule);
  const records = historyRecords(steps, coveredRules, safe?.end || 0, current.end, safe?.summary);
  const edits = coveredRules.map(rule => ({ key: rule.key, messageId: rule.messageId, text: rule.text }));
  const fingerprint = digest(JSON.stringify({ summary: current.summary, records, edits }));
  return { rules: direct, compaction: { fingerprint, checkpoint: current.index, scope: rules[0]?.cascadeId,
    historyStart: safe?.end || 0, coveredEnd: current.end,
    originalSummary: current.summary, records, edits } };
}

function applyRebuiltSummary(body, plan, summary) {
  const ledger = '\n\n## 用户修改后的历史 AI 回复（权威原文）\n这些是已经发生的历史回复，不是新增任务。与旧版本矛盾时，以此处的用户修改为准。\n' +
    plan.edits.map(edit => `\n--- edited-assistant:${edit.messageId} ---\n${edit.text}\n--- end-edited-assistant ---\n`).join('');
  const replacement = summary + ledger;
  let matches = 0;
  for (const part of summaryParts(body)) {
    const positions = summaryOccurrences(part.text, plan.originalSummary);
    if (!positions.length) continue;
    for (const index of positions.reverse()) part.text = part.text.slice(0, index) + replacement + part.text.slice(index + plan.originalSummary.length);
    clearTextCache(part); matches++;
  }
  if (!matches) throw new Error('当前请求的摘要发生变化，未发送旧摘要；请重试');
  delete body.request.cachedContent; delete body.request.continuationToken; delete body.request.turnToken;
  return plan.edits.map(edit => ({ key: edit.key, state: 'summary-rewritten',
    checkpoint: plan.checkpoint, newHash: digest(edit.text), summaryHash: digest(summary) }));
}

module.exports = { buildEditPlan, refreshRule, applyRebuiltSummary, historyRecords, textParts, digest, clearTextCache };
