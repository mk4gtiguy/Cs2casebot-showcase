(function () {
  var d = 200;
  var overlay = document.createElement('div');
  overlay.id = 'page-overlay';
  // This script loads from <head>, before <body> exists -- appending
  // immediately threw (document.body is null) and silently aborted the
  // rest of this file's top-level code every time. Defer until ready.
  if (document.body) {
    document.body.appendChild(overlay);
  } else {
    document.addEventListener('DOMContentLoaded', function () {
      document.body.appendChild(overlay);
    });
  }

  function fadeOut() {
    overlay.classList.add('active');
  }

  function fadeIn() {
    overlay.style.transition = 'none';
    overlay.classList.remove('active');
    void overlay.offsetHeight;
    overlay.style.transition = '';
  }

  function navigate(url) {
    if (overlay.classList.contains('active')) return;
    fadeOut();
    setTimeout(function () { window.location.href = url; }, d);
  }

  // Intercept same-origin link clicks
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a');
    if (!a || e.ctrlKey || e.metaKey || e.button !== 0) return;
    var h = a.getAttribute('href');
    if (!h || h === '#' || h.startsWith('javascript:') || h.startsWith('http:') ||
        h.startsWith('https:') || h.startsWith('//') || a.hasAttribute('download') ||
        a.getAttribute('target') === '_blank') return;
    e.preventDefault();
    navigate(h);
  });

  // On page show / load, fade in
  function onShow() {
    // Ensure overlay exists (it might have been removed if body was replaced)
    if (!document.getElementById('page-overlay')) {
      document.body.appendChild(overlay);
    }
    fadeIn();
  }
  window.addEventListener('pageshow', onShow);
  if (document.readyState === 'complete') onShow();
  else window.addEventListener('load', onShow);
})();

// Desktop scaling (games-shared.css scales .page/.hud-frame via transform
// on wide viewports). transform does not add to normal-flow layout height,
// so without this the enlarged render can run past the bottom of the
// viewport with no way to scroll to it. offsetHeight measures the element's
// own untransformed box, so this stays exact regardless of page content --
// no fixed-px guessing needed. A plain load/resize listener isn't enough:
// several game pages (tower, skin-bingo, ...) build their board/rows
// asynchronously after a fetch, growing .page AFTER 'load' already fired
// and measured a smaller height -- so this uses ResizeObserver to keep
// recomputing whenever .page's own (untransformed) box actually changes
// size, on every game page (this file is loaded everywhere).
(function () {
  function applyCompensation(el) {
    var t = getComputedStyle(el).transform;
    var m = t && t.match(/matrix\(([^,]+),/);
    var scale = m ? parseFloat(m[1]) : 1;
    if (!scale || scale <= 1) { el.style.marginBottom = ''; return; }
    el.style.marginBottom = (el.offsetHeight * (scale - 1)) + 'px';
  }
  function allTargets() {
    return document.querySelectorAll('.page, .hud-frame');
  }
  function compensateScale() {
    allTargets().forEach(applyCompensation);
  }
  function start() {
    compensateScale();
    if ('ResizeObserver' in window) {
      // Fires whenever a target's own layout box changes size (e.g. async
      // content growing it) -- transform alone does not trigger this, so
      // the resize listener below still covers breakpoint crossings.
      var ro = new ResizeObserver(function (entries) {
        entries.forEach(function (entry) { applyCompensation(entry.target); });
      });
      allTargets().forEach(function (el) { ro.observe(el); });
    }
  }
  window.addEventListener('resize', compensateScale);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
  // Belt-and-suspenders: several game pages build their board after an
  // awaited fetch on load (e.g. tower's buildFloor(), skin-bingo's card
  // draw), which can grow/shrink .page shortly after the checks above
  // already ran. Re-measure a few times over the typical fetch-settle
  // window rather than relying on any single lifecycle moment.
  [300, 1000, 2000].forEach(function (ms) {
    setTimeout(compensateScale, ms);
  });
})();
