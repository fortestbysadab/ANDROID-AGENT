/* Inline SVGs rather than an icon package: one more dependency would be a
   dependency tree, and the console ships as a single file. Stroke icons in
   the same family as the reference design. */
import React from 'react';

const base = {
  width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none',
  stroke: 'currentColor', strokeWidth: 1.9, strokeLinecap: 'round',
  strokeLinejoin: 'round', 'aria-hidden': 'true',
};

export const Globe = (p) => (
  <svg {...base} {...p}><circle cx="12" cy="12" r="9" />
    <path d="M3 12h18M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18" /></svg>
);
export const ArrowUp = (p) => (
  <svg {...base} strokeWidth="2.3" {...p}><path d="M12 19V5M5 12l7-7 7 7" /></svg>
);
export const Lock = (p) => (
  <svg {...base} width="12" height="12" {...p}>
    <rect x="5" y="11" width="14" height="10" rx="2" />
    <path d="M8 11V7a4 4 0 0 1 8 0v4" /></svg>
);
export const Plus = (p) => (
  <svg {...base} strokeWidth="2.3" {...p}><path d="M12 5v14M5 12h14" /></svg>
);
export const Menu = (p) => (
  <svg {...base} width="20" height="20" {...p}><path d="M4 7h16M4 12h16M4 17h16" /></svg>
);
export const Shield = (p) => (
  <svg {...base} width="14" height="14" {...p}>
    <path d="M12 3l7 3v6c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z" />
    <path d="M12 9v3.5M12 16h.01" /></svg>
);
export const Chat = (p) => (
  <svg {...base} {...p}><path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-6a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z" /></svg>
);
export const Doc = (p) => (
  <svg {...base} {...p}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <path d="M14 3v5h5M9 13h6M9 17h4" /></svg>
);
export const Clock = (p) => (
  <svg {...base} {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>
);
export const Tools = (p) => (
  <svg {...base} {...p}>
    <path d="M14.7 6.3a4 4 0 0 1 5 5L10 21H5v-5z" /><path d="M13 8l3 3" /></svg>
);
