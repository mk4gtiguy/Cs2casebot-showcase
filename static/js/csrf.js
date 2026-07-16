(function(){
  function getCookie(name) {
    var match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return match ? match[2] : '';
  }
  function ensureToken() {
    var tok = getCookie('csrf_token');
    if (!tok) {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', '/api/csrf-token', false);
      xhr.withCredentials = true;
      try { xhr.send(); } catch(e) {}
    }
  }
  ensureToken();
  var origFetch = window.fetch;
  window.fetch = function(url, opts) {
    opts = opts || {};
    if (opts.method && opts.method !== 'GET' && opts.method !== 'HEAD') {
      opts.headers = opts.headers || {};
      var t = getCookie('csrf_token');
      if (t) { opts.headers['X-CSRF-Token'] = t; }
    }
    return origFetch.call(this, url, opts);
  };
})();
