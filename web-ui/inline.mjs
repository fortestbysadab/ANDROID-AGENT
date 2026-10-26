/* Fold the built JS and CSS into one HTML file, written where the Python
   server expects it. Self-containment is a hard rule: the console must load
   with no network at all. */
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const dist = 'dist';
const js = readFileSync(join(dist, 'app.js'), 'utf8');
const cssPath = join(dist, 'app.css');
const css = existsSync(cssPath) ? readFileSync(cssPath, 'utf8') : '';
const boot = readFileSync('src/boot.js', 'utf8');

/* What makes the console installable to the home screen. Every path here
   is same-origin and served by the agent itself. */
const installTags = [
  '<meta name="theme-color" content="#171a26">',
  '<meta name="mobile-web-app-capable" content="yes">',
  '<meta name="apple-mobile-web-app-capable" content="yes">',
  '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">',
  '<meta name="apple-mobile-web-app-title" content="Agent">',
  '<link rel="manifest" href="/manifest.webmanifest">',
  '<link rel="icon" href="/static/icon-192.png" sizes="192x192" type="image/png">',
  '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">',
].join('\n');

/* Self-containment is still a hard rule. Same-origin paths are fine; a
   reference that leaves the device is not. */
if (/\bsrc\s*=\s*["']https?:/i.test(js) || /@import\s+url\(/i.test(css)) {
  throw new Error('Build references an external resource; the console must be self-contained.');
}
if (/https?:\/\//i.test(installTags)) {
  throw new Error('The document head references an external resource.');
}

const html = `<!doctype html>
<html lang="en" data-theme="system">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
${installTags}
<title>Android Agent</title>
<style>${css}</style>
<script>${boot}</script>
</head>
<body>
<div id="root"></div>
<script type="module">${js}</script>
</body>
</html>
`;
writeFileSync('../android_agent/web/ui.html', html);
console.log(`ui.html written: ${(html.length / 1024).toFixed(0)} KB`);
