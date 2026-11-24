import MarkdownIt from 'markdown-it';
import DOMPurify from 'dompurify';

const parser = new MarkdownIt({ html: false, linkify: true, breaks: false, typographer: false });
const validateLink = parser.validateLink;
parser.validateLink = value => {
  if (!/^file:/i.test(value)) return validateLink(value);
  try { const url = new URL(value); return url.protocol === 'file:' && !url.hostname && !url.username; }
  catch { return false; }
};
const allowedURI = /^(?:(?:(?:f|ht)tps?|mailto|tel|file):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i;

export function renderMarkdown(source) {
  return DOMPurify.sanitize(parser.render(source), {
    ALLOWED_TAGS: ['p', 'br', 'hr', 'em', 'strong', 's', 'a', 'blockquote', 'ul', 'ol', 'li', 'code', 'pre',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'img'],
    ALLOWED_ATTR: ['href', 'title', 'src', 'alt', 'start', 'class', 'align'],
    ALLOWED_URI_REGEXP: allowedURI,
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    RETURN_TRUSTED_TYPE: false,
  });
}
