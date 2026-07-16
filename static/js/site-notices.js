// Site-wide notice bars: announcements, fire sales, giveaways.
// Reads from the public /api/announcements, /api/fire-sales, /api/giveaways
// endpoints and renders a stack of dismissible bars pinned to the top of the page.
(function () {
  var DISMISS_PREFIX = 'notice_dismissed_';

  function isDismissed(id) {
    return localStorage.getItem(DISMISS_PREFIX + id) === '1';
  }
  function dismiss(id) {
    try { localStorage.setItem(DISMISS_PREFIX + id, '1'); } catch (e) {}
  }

  function fmtTimeLeft(ms) {
    if (ms <= 0) return 'ending soon';
    var h = Math.floor(ms / 3600000);
    var m = Math.floor((ms % 3600000) / 60000);
    if (h > 0) return h + 'h ' + m + 'm left';
    return m + 'm left';
  }

  function money(n) {
    return '$' + Number(n || 0).toLocaleString();
  }

  var container = document.createElement('div');
  container.id = 'site-notices';
  document.body.insertBefore(container, document.body.firstChild);

  function layout() {
    var bars = Array.prototype.slice.call(container.children);
    var offset = 0;
    bars.forEach(function (b) {
      b.style.top = offset + 'px';
      offset += b.offsetHeight;
    });
    document.documentElement.style.setProperty('--site-notices-height', offset + 'px');
  }

  function makeBar(opts) {
    var bar = document.createElement('div');
    bar.className = 'site-notice-bar site-notice-' + opts.kind;
    var html = '<span class="site-notice-text">' + opts.text + '</span>';
    if (opts.cta) html += '<button class="site-notice-cta">' + opts.cta + '</button>';
    html += '<button class="site-notice-close" aria-label="Dismiss">✕</button>';
    bar.innerHTML = html;
    bar.querySelector('.site-notice-close').onclick = function () {
      dismiss(opts.id);
      bar.remove();
      layout();
    };
    return bar;
  }

  function loadAnnouncements() {
    fetch('/api/announcements').then(function (r) { return r.json(); }).then(function (data) {
      var a = (data.announcements || [])[0];
      if (!a) return;
      var id = 'ann_' + a.id;
      if (isDismissed(id)) return;
      var bar = makeBar({
        id: id, kind: a.type === 'warning' ? 'warning' : (a.type === 'event' ? 'event' : 'info'),
        text: '📢 <strong>' + escapeHtml(a.title) + '</strong> — ' + escapeHtml(a.message),
      });
      container.appendChild(bar);
      layout();
    }).catch(function () {});
  }

  function loadFireSales() {
    fetch('/api/fire-sales').then(function (r) { return r.json(); }).then(function (data) {
      var s = (data.fire_sales || [])[0];
      if (!s) return;
      var id = 'sale_' + s.id;
      if (isDismissed(id)) return;
      var scope = s.case_type ? ('on ' + s.case_type.replace(/_/g, ' ')) : 'on all cases';
      var bar = makeBar({
        id: id, kind: 'sale', cta: 'Shop now',
        text: '🔥 <strong>FIRE SALE</strong> — ' + s.discount_percent + '% off ' + escapeHtml(scope) + '!',
      });
      bar.querySelector('.site-notice-cta').onclick = function () { location.href = '/'; };
      container.appendChild(bar);
      layout();
    }).catch(function () {});
  }

  function loadGiveaways() {
    fetch('/api/giveaways').then(function (r) { return r.json(); }).then(function (data) {
      var g = (data.giveaways || [])[0];
      if (!g) return;
      var id = 'give_' + g.id;
      if (isDismissed(id)) return;
      var left = fmtTimeLeft(new Date(g.end_time + 'Z') - new Date());
      var text = g.entered
        ? '🎁 You\'re entered! ' + money(g.prize_amount) + ' prize — ' + g.entries_count + ' entries, ' + left
        : '🎁 Giveaway active: ' + money(g.prize_amount) + ' prize — ' + g.entries_count + ' entries, ' + left;
      var bar = makeBar({
        id: id, kind: 'giveaway', text: text,
        cta: g.entered ? null : 'Enter',
      });
      if (!g.entered) {
        var ctaBtn = bar.querySelector('.site-notice-cta');
        ctaBtn.onclick = function () {
          ctaBtn.disabled = true;
          ctaBtn.textContent = 'Entering…';
          fetch('/api/giveaways/' + g.id + '/enter', { method: 'POST', credentials: 'include' })
            .then(function (r) {
              if (r.status === 401) {
                alert('Log in to enter this giveaway.');
                ctaBtn.disabled = false;
                ctaBtn.textContent = 'Enter';
                return;
              }
              return r.json().then(function (body) {
                if (!r.ok) {
                  alert(body.detail || body.error || 'Could not enter giveaway');
                  ctaBtn.disabled = false;
                  ctaBtn.textContent = 'Enter';
                  return;
                }
                bar.querySelector('.site-notice-text').textContent =
                  '🎁 You\'re entered! ' + money(g.prize_amount) + ' prize — drawing ' + left;
                ctaBtn.remove();
                layout();
              });
            })
            .catch(function () {
              ctaBtn.disabled = false;
              ctaBtn.textContent = 'Enter';
            });
        };
      }
      container.appendChild(bar);
      layout();
    }).catch(function () {});
  }

  function escapeHtml(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  loadAnnouncements();
  loadFireSales();
  loadGiveaways();
})();
