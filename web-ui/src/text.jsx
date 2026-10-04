/* A deliberately small Markdown subset, rendered as React elements rather
   than injected HTML. There is no dangerouslySetInnerHTML anywhere in this
   app: model output and message content are data, and React escaping is
   what guarantees it. */

import React from 'react';

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*|https?:\/\/[^\s<>"')]+)/g;

function inline(text, keyPrefix) {
  return text.split(INLINE).filter(Boolean).map((part, index) => {
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
      return <em key={key}>{part.slice(1, -1)}</em>;
    }
    if (/^https?:\/\//.test(part)) {
      return (
        <a key={key} href={part} target="_blank" rel="noopener noreferrer">{part}</a>
      );
    }
    return <React.Fragment key={key}>{part}</React.Fragment>;
  });
}

export function RichText({ value }) {
  const blocks = [];
  const lines = String(value || '').replace(/\r\n/g, '\n').split('\n');
  let paragraph = [];
  let list = null;
  let listKind = null;
  let fence = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push(
        <p key={`p${blocks.length}`}>{inline(paragraph.join(' '), `p${blocks.length}`)}</p>
      );
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      const Tag = listKind === 'ol' ? 'ol' : 'ul';
      blocks.push(
        <Tag key={`l${blocks.length}`}>
          {list.map((item, index) => (
            <li key={index}>{inline(item, `l${blocks.length}-${index}`)}</li>
          ))}
        </Tag>
      );
      list = null;
      listKind = null;
    }
  };

  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      if (fence === null) {
        flushParagraph();
        flushList();
        fence = [];
      } else {
        blocks.push(<pre key={`c${blocks.length}`}><code>{fence.join('\n')}</code></pre>);
        fence = null;
      }
      continue;
    }
    if (fence !== null) { fence.push(line); continue; }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const kind = bullet ? 'ul' : 'ol';
      if (listKind !== kind) { flushList(); list = []; listKind = kind; }
      list.push((bullet || numbered)[1]);
      continue;
    }
    flushList();
    if (!line.trim()) { flushParagraph(); continue; }
    paragraph.push(line.trim());
  }
  if (fence !== null) {
    blocks.push(<pre key="cend"><code>{fence.join('\n')}</code></pre>);
  }
  flushParagraph();
  flushList();
  return <>{blocks}</>;
}

export function when(seconds) {
  if (!seconds) return '';
  const date = new Date(seconds * 1000);
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  return sameDay
    ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleString([], {
        day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
      });
}

export function fileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
