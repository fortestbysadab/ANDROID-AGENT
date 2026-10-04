/* The non-chat screens: Files, Schedule and Tools.

   All three are read-only by design. Nothing here can delete a file, cancel
   a task or run a tool: the agent has no delete capability anywhere, and an
   action taken from a list — without the approval prompt and the argument
   preview that the chat path provides — would quietly bypass the safety
   model. Ask the agent in chat and the usual gates apply. */

import React, { useEffect, useState } from 'react';
import { api, downloadUrl } from './api.js';
import { fileSize, when } from './text.jsx';

function useFetch(loader, deps = []) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  useEffect(() => {
    let live = true;
    setState((prev) => ({ ...prev, loading: true }));
    loader()
      .then((data) => live && setState({ loading: false, error: null, data }))
      .catch((error) => live && setState({ loading: false, error: error.message, data: null }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

function Loading({ what }) {
  return <p className="empty">Loading {what}…</p>;
}

function Failed({ error }) {
  return <p className="empty">{error}</p>;
}

/* ------------------------------------------------------------- files */

const PREVIEWABLE = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp']);
const KIND_LABEL = {
  document: 'document', photo: 'photo',
  screenshot: 'screenshot', recording: 'recording',
};
const KINDS = ['all', 'document', 'photo', 'screenshot', 'recording'];

export function FilesScreen() {
  const { loading, error, data } = useFetch(api.files);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('all');
  if (loading) return <Loading what="files" />;
  if (error) return <Failed error={error} />;

  const files = (data.files || []).filter(
    (file) =>
      file.name.toLowerCase().includes(query.trim().toLowerCase()) &&
      (kind === 'all' || file.kind === kind)
  );
  const counts = (data.files || []).reduce((totals, file) => {
    totals[file.kind] = (totals[file.kind] || 0) + 1;
    return totals;
  }, {});
  if (!data.files?.length) {
    return (
      <p className="empty">
        Nothing here yet. Ask the agent to make something — “a PDF report on
        October expenses” — or take a screenshot or photo.
      </p>
    );
  }
  return (
    <>
      <input
        className="filter"
        placeholder="Filter files"
        value={query}
        aria-label="Filter files"
        onChange={(event) => setQuery(event.target.value)}
      />
      <div
        className="row"
        role="group"
        aria-label="Filter by kind"
        style={{ maxInlineSize: '76ch', margin: '0 auto 12px' }}
      >
        {KINDS.filter((name) => name === 'all' || counts[name]).map((name) => (
          <button
            key={name}
            type="button"
            className="btn"
            aria-pressed={kind === name}
            style={
              kind === name
                ? { background: 'var(--accent)', color: 'var(--accent-text)',
                    borderColor: 'transparent' }
                : undefined
            }
            onClick={() => setKind(name)}
          >
            {name === 'all' ? `All (${data.files.length})` : `${name} (${counts[name]})`}
          </button>
        ))}
      </div>
      <div className="cards">
        {files.map((file) => (
          <article className="card" key={file.name}>
            <div className="row">
              <h3>{file.title || file.name}</h3>
              <span className="tag">{KIND_LABEL[file.kind] || 'file'}</span>
              <span className="tag">{file.format || '—'}</span>
              {file.version > 1 && <span className="tag">v{file.version}</span>}
            </div>
            <div className="meta">
              {file.name} · {fileSize(file.size)} · {when(file.modified)}
            </div>
            {file.kind === 'recording' && (
              <audio
                controls
                preload="none"
                src={downloadUrl(file.name)}
                style={{ inlineSize: '100%', marginBlockStart: 10 }}
              />
            )}
            {PREVIEWABLE.has(file.format) && (
              <img
                src={downloadUrl(file.name)}
                alt={file.title || file.name}
                loading="lazy"
                style={{
                  marginBlockStart: 10, maxInlineSize: '100%',
                  borderRadius: 8, display: 'block',
                }}
              />
            )}
            <div className="row" style={{ marginBlockStart: 10 }}>
              <a
                className="btn"
                href={downloadUrl(file.name)}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open
              </a>
              <a className="btn" href={downloadUrl(file.name)} download={file.name}>
                Download
              </a>
            </div>
          </article>
        ))}
        {!files.length && <p className="empty">Nothing matches “{query}”.</p>}
      </div>
    </>
  );
}

/* ---------------------------------------------------------- schedule */

export function ScheduleScreen() {
  const { loading, error, data } = useFetch(api.schedule);
  if (loading) return <Loading what="schedule" />;
  if (error) return <Failed error={error} />;

  const tasks = data.tasks || [];
  if (!tasks.length) {
    return (
      <p className="empty">
        Nothing scheduled. Ask the agent — “check my battery every morning at
        7”, “remind me in 20 minutes”.
      </p>
    );
  }
  return (
    <div className="cards">
      {tasks.map((task) => (
        <article className="card" key={task.id}>
          <div className="row">
            <h3>{task.description}</h3>
            {task.completed && <span className="tag">done</span>}
            {!task.enabled && !task.completed && <span className="tag">paused</span>}
          </div>
          <div className="meta">
            {task.schedule}
            {task.enabled && ` · next ${task.next_run_local}`}
          </div>
          <div className="meta">
            {task.kind === 'tool' ? task.tool : 'instruction'} · run{' '}
            {task.run_count} time{task.run_count === 1 ? '' : 's'}
            {task.last_status && ` · last ${task.last_status}`}
          </div>
        </article>
      ))}
      <p className="meta" style={{ textAlign: 'center' }}>
        To change or cancel one, ask in chat — that way the usual confirmation
        applies.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------- tools */

const RISK_ORDER = [
  'read_only', 'reversible', 'sensitive_read', 'device_mutation',
  'external_side_effect', 'raw_control', 'critical',
];
const RISK_LABEL = {
  read_only: 'Read only',
  reversible: 'Reversible',
  sensitive_read: 'Private data',
  device_mutation: 'Changes the device',
  external_side_effect: 'Leaves the device — needs approval',
  raw_control: 'Direct screen control',
  critical: 'Denied',
};

export function ToolsScreen() {
  const { loading, error, data } = useFetch(api.tools);
  const [query, setQuery] = useState('');
  if (loading) return <Loading what="tools" />;
  if (error) return <Failed error={error} />;

  const needle = query.trim().toLowerCase();
  const tools = (data.tools || []).filter(
    (tool) =>
      tool.name.includes(needle) || tool.description.toLowerCase().includes(needle)
  );
  const groups = RISK_ORDER.map((risk) => [
    risk, tools.filter((tool) => tool.risk === risk),
  ]).filter(([, items]) => items.length);

  return (
    <>
      <input
        className="filter"
        placeholder={`Search ${data.tools?.length || 0} tools`}
        value={query}
        aria-label="Search tools"
        onChange={(event) => setQuery(event.target.value)}
      />
      <div className="cards">
        {groups.map(([risk, items]) => (
          <section key={risk}>
            <h2 style={{ fontSize: 14, margin: '14px 0 8px' }}>
              {RISK_LABEL[risk]} <span className="meta">({items.length})</span>
            </h2>
            <div className="cards">
              {items.map((tool) => (
                <article className="card" key={tool.name}>
                  <div className="row">
                    <h3>{tool.name.replace(/_/g, ' ')}</h3>
                    <span className={`tag ${tool.risk}`}>{RISK_LABEL[tool.risk]}</span>
                    {tool.untrusted && (
                      <span className="tag" title="Output is written by other people">
                        untrusted content
                      </span>
                    )}
                  </div>
                  <p className="desc">{tool.description}</p>
                </article>
              ))}
            </div>
          </section>
        ))}
        {!tools.length && <p className="empty">No tool matches “{query}”.</p>}
      </div>
    </>
  );
}
