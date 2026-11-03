// Lets each page work inside the app's tabs (app.html). Loaded in <head> of every page by webapp.dress.
(function () {
  // Page names that are tabs in the app. '' is the call log.
  var TABS = { '': 'calls', 'index.html': 'calls', 'map': 'map', 'listen': 'listen', 'aviation': 'aviation',
               'history': 'history', 'settings': 'settings' };
  var inApp = false;
  try { inApp = window.parent !== window && /\/app$/.test(window.parent.location.pathname); } catch (e) { /* not ours */ }
  if (!inApp) return;
  document.documentElement.classList.add('in-app');
  var dir = location.pathname.replace(/[^/]*$/, '');
  function tabOf(a) {
    if (!a.href || a.target === '_blank' || a.hasAttribute('download')) return null;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin || url.pathname.indexOf(dir) !== 0) return null;
    var name = url.pathname.slice(dir.length);
    return Object.prototype.hasOwnProperty.call(TABS, name) ? TABS[name] : null;
  }
  function tell(msg) { msg.app = true; window.parent.postMessage(msg, location.origin); }
  // Links to another tab switch tabs instead of opening that page inside this one.
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var tab = tabOf(a);
    if (!tab) return;
    e.preventDefault();
    tell({ go: tab, url: a.href });
  }, true);
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('header a[href]').forEach(function (a) { if (tabOf(a)) a.classList.add('app-tab-link'); });
    var opener = document.getElementById('recaps-open');
    var panel = document.getElementById('recaps');
    if (opener) opener.classList.add('app-tab-link');
    if (panel) {
      // The map's 12-hour summaries panel is the app's Summaries tab.
      new MutationObserver(function () { tell({ recaps: !panel.hidden }); })
        .observe(panel, { attributes: true, attributeFilter: ['hidden'] });
    }
    var own = location.pathname.slice(dir.length);
    tell({ ready: true, page: Object.prototype.hasOwnProperty.call(TABS, own) ? TABS[own] : '' });
  });
  window.addEventListener('message', function (e) {
    if (e.origin !== location.origin || !e.data || !e.data.app) return;
    var panel = document.getElementById('recaps');
    if (e.data.recaps === true && panel && panel.hidden && typeof window.openRecaps === 'function') window.openRecaps();
    if (e.data.recaps === false && panel) panel.hidden = true;
  });
})();
