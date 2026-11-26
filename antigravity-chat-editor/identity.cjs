'use strict';

// Some completed native replies omit messageId but retain the content identity
// shared by the rendered step and the language server's trajectory record.
function responseId(response, metadata) {
  if (typeof response?.messageId === 'string' && response.messageId) return response.messageId;
  if (typeof metadata?.modelApiContentId === 'string' && metadata.modelApiContentId) return 'content:' + metadata.modelApiContentId;
  return null;
}

module.exports = { responseId };
