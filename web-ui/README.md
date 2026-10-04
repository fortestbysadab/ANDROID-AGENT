# Web console source

React + Vite. The build produces **one self-contained HTML file** at
`android_agent/web/ui.html` — no CDN, no webfont, no external request on
load — because the console has to work on a phone with no connection.

```sh
cd web-ui
npm install
npm run build     # writes ../android_agent/web/ui.html
npm run dev       # local dev server, proxies nothing; run the Python server too
```

`inline.mjs` folds the JS and CSS into the HTML and **fails the build** if
anything references an external URL. The one deliberate exception is the
location map, which is created in a click handler and therefore never
fetched on load.

The built `ui.html` is committed, so running the agent needs no Node.
