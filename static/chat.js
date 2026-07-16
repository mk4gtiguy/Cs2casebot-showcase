// Global site chat: a small toggle bubble docked bottom-left (out of the way
// of page content and the agent-egg widget, which lives bottom-right). Click
// to open a chat panel; closed by default so it never covers anything.
(function () {
  var ws = null;
  var reconnectTimer = null;
  var connected = false;
  var chatOpen = false;
  var unread = 0;

  var container = document.createElement('div');
  container.id = 'chat-widget';
  container.innerHTML = [
    '<style>',
    '  #chat-widget { position:fixed; left:16px; bottom:16px; z-index:9999; font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif; }',
    '  #chat-bubble { position:relative; width:48px; height:48px; border-radius:50%; background:#1a1a2e; border:2px solid rgba(255,215,0,.5); box-shadow:0 2px 12px rgba(0,0,0,.6); display:flex; align-items:center; justify-content:center; font-size:22px; cursor:pointer; user-select:none; }',
    '  #chat-bubble:hover { border-color:rgba(255,215,0,.9); }',
    '  #chat-badge { position:absolute; top:-4px; right:-4px; min-width:18px; height:18px; padding:0 4px; border-radius:9px; background:#ff5c5c; color:#fff; font-size:11px; font-weight:700; display:none; align-items:center; justify-content:center; box-shadow:0 0 0 2px #1a1a2e; }',
    '  #chat-badge.show { display:flex; }',
    '  #chat-panel { position:absolute; left:0; bottom:58px; width:320px; max-width:calc(100vw - 32px); background:#12121e; border:1px solid rgba(255,215,0,.35); border-radius:10px; box-shadow:0 8px 30px rgba(0,0,0,.6); display:none; flex-direction:column; overflow:hidden; }',
    '  #chat-panel.open { display:flex; }',
    '  #chat-panel-head { padding:8px 12px; font-size:12px; font-weight:700; color:#ffd700; border-bottom:1px solid rgba(255,255,255,.08); display:flex; justify-content:space-between; align-items:center; }',
    '  #chat-panel-close { cursor:pointer; color:rgba(255,255,255,.5); font-size:14px; }',
    '  #chat-panel-close:hover { color:#fff; }',
    '  #chat-log { flex:1; overflow-y:auto; padding:8px 10px; max-height:280px; display:flex; flex-direction:column; gap:4px; }',
    '  .chat-line { font-size:12px; line-height:1.4; color:#e8e8e8; word-break:break-word; }',
    '  .chat-line .cname { font-weight:700; }',
    '  .chat-line .cname.normal { color:#7fc7ff; }',
    '  .chat-line .cname.vip { color:#ffd700; }',
    '  .chat-line .cname.admin { color:#ff5c5c; }',
    '  .chat-line .ctag { font-size:10px; font-weight:700; opacity:.85; margin-right:2px; }',
    '  .chat-line.system { color:#888; font-style:italic; font-size:11px; }',
    '  #chat-input-row { border-top:1px solid rgba(255,255,255,.08); padding:8px; }',
    '  #chat-input { width:100%; box-sizing:border-box; background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.14); border-radius:5px; padding:8px 10px; color:#fff; font-size:16px; outline:none; }',
    '  #chat-input::placeholder { color:rgba(255,255,255,.4); }',
    '  #chat-input:focus { border-color:rgba(255,215,0,.6); }',
    '  @media (max-width:600px) { #chat-panel { width:calc(100vw - 32px); } }',
    '</style>',
    '<div id="chat-panel">',
    '  <div id="chat-panel-head"><span>💬 Chat</span><span id="chat-panel-close">✕</span></div>',
    '  <div id="chat-log" role="log" aria-live="polite" aria-label="Chat messages"></div>',
    '  <div id="chat-input-row">',
    '    <input id="chat-input" type="text" placeholder="Say something..." maxlength="500" aria-label="Chat message">',
    '  </div>',
    '</div>',
    '<div id="chat-bubble" title="Chat">💬<span id="chat-badge"></span></div>'
  ].join('\n');
  document.body.appendChild(container);

  var bubble = document.getElementById('chat-bubble');
  var badge = document.getElementById('chat-badge');
  var panel = document.getElementById('chat-panel');
  var closeBtn = document.getElementById('chat-panel-close');
  var logEl = document.getElementById('chat-log');
  var input = document.getElementById('chat-input');
  var MAX_LINES = 100;

  function setUnread(n) {
    unread = n;
    if (unread > 0) {
      badge.textContent = unread > 9 ? '9+' : String(unread);
      badge.classList.add('show');
    } else {
      badge.classList.remove('show');
    }
  }

  function openChat() {
    if (chatOpen) return;
    chatOpen = true;
    panel.classList.add('open');
    setUnread(0);
    logEl.scrollTop = logEl.scrollHeight;
    input.focus();
  }

  function closeChat() {
    if (!chatOpen) return;
    chatOpen = false;
    panel.classList.remove('open');
    input.blur();
  }

  function toggleChat() {
    if (chatOpen) closeChat(); else openChat();
  }

  function setStatus(text) {
    var sys = document.createElement('div');
    sys.className = 'chat-line system';
    sys.textContent = '— ' + text + ' —';
    logEl.appendChild(sys);
    trimLines();
  }

  function trimLines() {
    while (logEl.children.length > MAX_LINES) {
      logEl.removeChild(logEl.firstChild);
    }
    logEl.scrollTop = logEl.scrollHeight;
  }

  function connect() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/api/chat/ws';
    try { ws = new WebSocket(url); } catch (e) { return; }

    ws.onopen = function () {
      connected = true;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    };

    ws.onmessage = function (e) {
      try {
        var data = JSON.parse(e.data);
        if (data.type === 'history') {
          logEl.innerHTML = '';
          if (data.messages) data.messages.forEach(addMessage);
          return;
        }
        addMessage(data);
        if (!chatOpen && data.type === 'message') {
          setUnread(unread + 1);
        }
      } catch (_) {}
    };

    ws.onclose = function () {
      connected = false;
      ws = null;
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(connect, 3000);
      }
    };

    ws.onerror = function () { ws && ws.close(); };
  }

  function addMessage(msg) {
    if (msg.type === 'system') {
      setStatus(msg.text);
      return;
    }
    if (msg.type !== 'message') return;

    var d = document.createElement('div');
    d.className = 'chat-line';

    var tag = msg.is_admin ? '<span class="ctag" style="color:#ff5c5c;">[ADMIN]</span>' : msg.is_vip ? '<span class="ctag" style="color:#ffd700;">[VIP]</span>' : '';
    var nameClass = msg.is_admin ? 'admin' : msg.is_vip ? 'vip' : 'normal';
    d.innerHTML = tag + '<span class="cname ' + nameClass + '">' + esc(msg.username || 'Guest') + '</span>: <span class="ctext">' + esc(msg.text) + '</span>';
    logEl.appendChild(d);
    trimLines();
  }

  function sendMessage() {
    var text = input.value.trim();
    if (!text) return;
    if (!connected || !ws) { setStatus('not connected'); return; }
    ws.send(JSON.stringify({ type: 'message', text: text }));
    input.value = '';
  }

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); sendMessage(); }
    else if (e.key === 'Escape') { e.preventDefault(); closeChat(); }
  });

  bubble.addEventListener('click', toggleChat);
  closeBtn.addEventListener('click', closeChat);

  var esc = function (s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); };

  // Auto-connect on next tick so the DOM is fully ready
  setTimeout(connect, 100);
})();
