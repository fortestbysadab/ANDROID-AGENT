(function () {
  try { document.documentElement.dataset.theme = localStorage.getItem('aa-theme') || 'system'; }
  catch (e) { document.documentElement.dataset.theme = 'system'; }
})();
