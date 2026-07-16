// Shared sticker-compositing renderer -- used by both dashboard.js (inventory
// grid badges + inspect modal) and market.html (listing cards) so both show
// the exact same "weapon with stickers layered on top" look via one
// implementation instead of two hand-rolled copies that could drift apart.
//
// Returns ONLY the sticker overlay <img> markup (not a wrapping element) --
// the caller inserts it as a sibling of their own base weapon <img>, inside
// whatever position:relative container that page already has (.inv-img-wrap,
// .card-img-wrap, .item-inspect-weapon-wrap, ...). x/y are 0-1 normalized
// (same convention the sticker sandbox already uses), sticker size is a
// percentage of that same container so it scales with it at any size.
//
// Self-contained (inline styles only, no dependency on either page's own CSS
// classes or helper functions), since dashboard.js's esc()/CSS classes are
// not exposed globally and market.html has its own separate stylesheet.
(function () {
  function escAttr(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/"/g, '&quot;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function renderStickerOverlays(appliedStickers, opts) {
    opts = opts || {};
    var stickerPct = opts.stickerPct != null ? opts.stickerPct : 15; // matches the sandbox's 15%-of-parent convention
    var clickable = !!opts.clickable;
    var stickers = Array.isArray(appliedStickers) ? appliedStickers : [];
    return stickers.map(function (s) {
      if (!s || !s.sticker_image) return '';
      var x = (s.x != null ? s.x : 0.5) * 100;
      var y = (s.y != null ? s.y : 0.5) * 100;
      var rot = s.rotation != null ? s.rotation : 0;
      var scale = s.scale != null ? s.scale : 1.0;
      return '<img src="' + escAttr(s.sticker_image) + '" alt="' + escAttr(s.sticker_name || '') + '" ' +
        'style="position:absolute;left:' + x + '%;top:' + y + '%;width:' + stickerPct + '%;height:' + stickerPct + '%;' +
        'object-fit:contain;transform:translate(-50%,-50%) rotate(' + rot + 'deg) scale(' + scale + ');' +
        'filter:drop-shadow(0 1px 3px rgba(0,0,0,0.6));pointer-events:' + (clickable ? 'auto' : 'none') + ';" ' +
        'onerror="this.style.display=\'none\'">';
    }).join('');
  }

  window.renderStickerOverlays = renderStickerOverlays;
})();
