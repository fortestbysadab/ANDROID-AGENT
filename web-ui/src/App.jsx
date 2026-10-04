import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api.js';
import { FilesScreen, ScheduleScreen, ToolsScreen } from './screens.jsx';
import { RichText, when } from './text.jsx';
import {
  ArrowUp, Chat as ChatIcon, Clock, Doc, Globe, Lock, Menu, Plus, Shield,
  Tools as ToolsIcon,
} from './icons.jsx';

const SCREENS = [
  { id: 'chat', label: 'Chat', Icon: ChatIcon },
  { id: 'files', label: 'Files', Icon: Doc },
  { id: 'schedule', label: 'Schedule', Icon: Clock },
  { id: 'tools', label: 'Tools', Icon: ToolsIcon },
];
const POLL_MS = 5000;

/* ------------------------------------------------------------- login */

function Login({ onDone }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api.login(token);
      onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form onSubmit={submit}>
        <div className="mark">A</div>
        <h1>Android Agent</h1>
        <p>Enter the access token from your .env file.</p>
        {error && <div className="error" role="alert">{error}</div>}
        <input
          type="password"
          value={token}
          autoFocus
          autoComplete="current-password"
          aria-label="Access token"
          placeholder="Access token"
          onChange={(event) => setToken(event.target.value)}
        />
        <button type="submit" disabled={busy || !token}>
          {busy ? 'Checking…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}

/* -------------------------------------------------------------- chat */

function MapCard({ artifact }) {
  const [shown, setShown] = useState(false);
  const { latitude, longitude, accuracy, approximate } = artifact;
  const label = `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`;
  const precision =
    typeof accuracy === 'number'
      ? accuracy >= 1000
        ? ` · ±${(accuracy / 1000).toFixed(1)} km`
        : ` · ±${Math.round(accuracy)} m`
      : '';

  return (
    <div className="mapcard">
      <div className="maphead">
        <span>📍 {label}{precision}</span>
        <a
          href={`https://www.google.com/maps/search/?api=1&query=${latitude},${longitude}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open in Maps
        </a>
      </div>
      {approximate && (
        <div className="warnline" style={{ padding: '0 12px 8px' }}>
          Approximate — this may be a long way out.
        </div>
      )}
      {shown ? (
        /* Built on click, never in the initial markup: until the owner asks,
           the console has requested nothing from anyone but this server. */
        <iframe
          className="mapframe"
          title={`Map of ${label}`}
          loading="lazy"
          referrerPolicy="no-referrer"
          src={`https://maps.google.com/maps?q=${latitude},${longitude}&z=16&output=embed`}
        />
      ) : (
        <button type="button" onClick={() => setShown(true)}>Show map</button>
      )}
    </div>
  );
}

function Message({ message, onApprove }) {
  if (message.role === 'approval') {
    const args = Object.entries(message.arguments || {});
    return (
      <div className="turn">
        <div className="approval">
          <h3><Shield /> Approval required — {message.text}</h3>
          {message.after_untrusted_content && (
            <div className="warnline">
              This followed message content written by someone else. Check it is
              what you asked for.
            </div>
          )}
          <div className="args">
            {args.length
              ? args.map(([key, value]) => (
                  <div key={key}><b>{key}</b>: {String(value)}</div>
                ))
              : 'No arguments.'}
          </div>
          {message.resolved ? (
            <div className="meta">{message.resolved}</div>
          ) : (
            <div className="acts">
              <button
                type="button"
                className="approve"
                onClick={() => onApprove(message.approval_id, true)}
              >
                Approve
              </button>
              <button
                type="button"
                className="deny"
                onClick={() => onApprove(message.approval_id, false)}
              >
                Deny
              </button>
            </div>
          )}
        </div>
      </div>
    );
  }

  const mine = message.role === 'user';
  const maps = (message.artifacts || []).filter(
    (artifact) => artifact.kind === 'location' && typeof artifact.latitude === 'number'
  );
  const others = (message.artifacts || []).filter(
    (artifact) => artifact.kind !== 'location'
  );

  if (mine) {
    return (
      <div className="turn user">
        <div className="body">
          <div className="userbubble">{message.text}</div>
          <div className="byline" style={{ marginBlock: '6px 0' }}>
            <time>{when(message.at)}</time>
          </div>
        </div>
        <div className="avatar me" aria-hidden="true">You</div>
      </div>
    );
  }

  return (
    <div className="turn">
      <div className="avatar bot" aria-hidden="true">A</div>
      <div className="body">
        <div className="byline">
          <b>Agent</b><time>{when(message.at)}</time>
          {message.used_web_search && (
            <span style={{ color: 'var(--accent)', display: 'inline-flex', gap: 4, alignItems: 'center' }}>
              <Globe width="12" height="12" /> Web search
            </span>
          )}
        </div>
        <div className="prose">
          <RichText value={message.text} />
          {maps.map((artifact, index) => (
            <MapCard key={index} artifact={artifact} />
          ))}
          {others.length > 0 && (
            <div className="row" style={{ marginBlockStart: 10 }}>
              {others.map((artifact, index) => (
                <a
                  key={index}
                  className="tag"
                  href={`/api/media/file?name=${encodeURIComponent(artifact.name)}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {artifact.name}
                </a>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Chat({ state, setState, notify, search, setSearch }) {
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const threadRef = useRef(null);

  useEffect(() => {
    const node = threadRef.current;
    if (!node) return;
    const parent = node.parentElement;
    // Only follow the conversation when the owner is already at the bottom;
    // yanking the view while they are reading history is maddening.
    const atBottom = parent.scrollHeight - parent.scrollTop - parent.clientHeight < 140;
    if (atBottom) parent.scrollTop = parent.scrollHeight;
  }, [state.messages]);

  async function send(event) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    setDraft('');
    setBusy(true);
    setState((prev) => ({
      ...prev,
      messages: [...(prev.messages || []), { role: 'user', text, at: Date.now() / 1000 }],
    }));
    try {
      setState((await api.send(text, search)).state);
    } catch (error) {
      notify(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function approve(id, yes) {
    try {
      setState((await api.approve(id, yes)).state);
    } catch (error) {
      notify(error.message);
    }
  }

  const messages = state.messages || [];
  return (
    <>
      <div className="screen chat scroll">
        <div className="thread" ref={threadRef}>
          {messages.length === 0 && (
            <p className="empty">
              Ask for anything — “how much battery?”, “where am I?”,
              “summarise my inbox”, “make a PDF report on October expenses”.
            </p>
          )}
          {messages.map((message, index) => (
            <Message key={index} message={message} onApprove={approve} />
          ))}
          {busy && (
            <div className="turn">
              <div className="avatar bot" aria-hidden="true">A</div>
              <div className="body working">
                <span className="dots"><span /><span /><span /></span> Working…
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="composer">
        <form className="shell" onSubmit={send}>
          <label htmlFor="composer" className="sr-only" style={{ position: 'absolute', left: -9999 }}>
            Message the agent
          </label>
          <textarea
            id="composer"
            rows={1}
            value={draft}
            placeholder="Message the agent…"
            onChange={(event) => {
              setDraft(event.target.value);
              const box = event.target;
              box.style.height = 'auto';
              box.style.height = `${Math.min(box.scrollHeight, 180)}px`;
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) send(event);
            }}
          />
          <div className="shellfoot">
            <button
              type="button"
              className="searchtoggle"
              aria-pressed={search}
              aria-label={search ? 'Disable web search' : 'Enable web search'}
              onClick={() => setSearch(!search)}
            >
              <Globe />
              <span>Web search</span>
              {search && <span className="live" />}
            </button>
            <div className="row" style={{ gap: 12 }}>
              <span className="hint">
                {busy ? 'Working…' : 'Enter to send · Shift + Enter for a new line'}
              </span>
              <button className="send" type="submit" disabled={busy || !draft.trim()}>
                <ArrowUp />
              </button>
            </div>
          </div>
        </form>
        <p className="privacy">
          <Lock /> Everything stays on your phone. The agent can be wrong —
          check anything that matters.
        </p>
      </div>
    </>
  );
}

/* --------------------------------------------------------------- app */

export default function App() {
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);
  const [screen, setScreen] = useState('chat');
  const [state, setState] = useState({ messages: [] });
  const [theme, setTheme] = useState(
    () => localStorage.getItem('aa-theme') || 'system'
  );
  const [toast, setToast] = useState('');
  const [drawer, setDrawer] = useState(false);
  const [search, setSearch] = useState(false);

  const notify = useCallback((message) => {
    setToast(message);
    setTimeout(() => setToast(''), 4000);
  }, []);

  const refresh = useCallback(async () => {
    try {
      setState(await api.state());
      setAuthed(true);
    } catch (error) {
      if (error.status === 401) setAuthed(false);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('aa-theme', theme);
  }, [theme]);

  useEffect(() => {
    if (!authed || screen !== 'chat') return undefined;
    const id = setInterval(() => {
      // Polling while the tab is hidden wastes the phone's battery for
      // nothing: nobody is looking at the result.
      if (document.visibilityState === 'visible') refresh();
    }, POLL_MS);
    return () => clearInterval(id);
  }, [authed, screen, refresh]);

  useEffect(() => {
    const onKey = (event) => { if (event.key === 'Escape') setDrawer(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (!ready) return <div className="empty">Loading…</div>;
  if (!authed) return <Login onDone={refresh} />;

  const current = SCREENS.find((item) => item.id === screen);
  return (
    <div className="app">
      {drawer && (
        <button className="scrim" aria-label="Close menu" onClick={() => setDrawer(false)} />
      )}
      <aside className={`side${drawer ? ' open' : ''}`}>
        <div className="sidehead">
          <span className="mark" aria-hidden="true">A</span>
          <b>Android Agent</b>
        </div>
        <button
          type="button"
          className="newchat"
          onClick={async () => {
            try {
              setState((await api.newChat()).state);
              setScreen('chat');
              setDrawer(false);
            } catch (error) { notify(error.message); }
          }}
        >
          <Plus /> New chat
        </button>
        <nav className="nav" aria-label="Sections">
          {SCREENS.map(({ id, label, Icon }) => (
            <button
              key={id}
              type="button"
              aria-current={screen === id ? 'page' : undefined}
              onClick={() => { setScreen(id); setDrawer(false); }}
            >
              <Icon />
              {label}
              {id === 'tools' && state.tools ? (
                <span className="count">{state.tools}</span>
              ) : null}
            </button>
          ))}
        </nav>
        <div className="sidefoot">
          <div className="themes" role="group" aria-label="Theme">
            {['light', 'system', 'dark'].map((name) => (
              <button
                key={name}
                type="button"
                aria-pressed={theme === name}
                onClick={() => setTheme(name)}
              >
                {name}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="ghost"
            onClick={async () => {
              try { await api.logout(); } finally { setAuthed(false); }
            }}
          >
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <button
            className="burger"
            type="button"
            aria-label="Menu"
            onClick={() => setDrawer(true)}
          >
            <Menu />
          </button>
          <h1>{current.label}</h1>
          <span className="status">
            <span className={`dot${state.session?.active ? '' : ' off'}`} />
            {state.session?.active ? 'Remembering this chat' : 'Fresh chat'}
          </span>
        </header>

        {screen === 'chat' ? (
          <Chat
            state={state}
            setState={setState}
            notify={notify}
            search={search}
            setSearch={setSearch}
          />
        ) : (
          <div className="screen scroll">
            {screen === 'files' && <FilesScreen />}
            {screen === 'schedule' && <ScheduleScreen />}
            {screen === 'tools' && <ToolsScreen />}
          </div>
        )}
      </main>

      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
