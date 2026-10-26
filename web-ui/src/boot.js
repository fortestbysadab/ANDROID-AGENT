(function () {
  try { document.documentElement.dataset.theme = localStorage.getItem('aa-theme') || 'system'; }
  catch (e) { document.documentElement.dataset.theme = 'system'; }
})();

/* Registering the service worker is what lets Android offer "Install app".
   It is deliberately best-effort: a browser without support, or a page
   served over plain http from something other than localhost, simply does
   not get the install option and everything else still works. */
(function () {
  if (!('serviceWorker' in navigator)) return;
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function (error) {
      console.warn('Install support unavailable:', error && error.message);
    });
  });
})();
