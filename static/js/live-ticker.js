// Live activity ticker — scrolling feed of recent drops/wins across the site.
// Self-contained: include with a single <script src="/static/js/live-ticker.js">
// plus a <div class="live-ticker" id="liveTicker"><div class="ticker-track"
// id="tickerTrack"></div></div> container, no other setup. Polls
// /api/lobby-ticker every 20s; that endpoint requires no auth.
(function () {
    function injectStyles() {
        const style = document.createElement('style');
        style.textContent = `
            @keyframes tickerMove { 0% { transform: translateX(0); } 100% { transform: translateX(-50%); } }
            @keyframes tickerGlow { 0%, 100% { border-color: rgba(255,215,0,0.08); } 50% { border-color: rgba(255,215,0,0.25); } }
            @keyframes tickerBlink { 0%,100% { opacity:1; } 50% { opacity:0.2; } }
            .live-ticker {
                overflow:hidden;
                background:linear-gradient(180deg, rgba(5,5,15,0.98), rgba(10,10,20,0.95));
                border-bottom:1px solid rgba(255,215,0,0.12);
                padding:8px 0;
                white-space:nowrap;
                position:relative;
                animation:tickerGlow 4s ease-in-out infinite;
                box-shadow:0 2px 20px rgba(255,215,0,0.03);
            }
            .live-ticker::before {
                content:'🔴 LIVE';
                position:absolute;
                left:12px;
                top:50%;
                transform:translateY(-50%);
                font-size:9px;
                font-weight:900;
                letter-spacing:1.5px;
                color:#ff4444;
                text-shadow:0 0 10px rgba(255,68,68,0.5);
                z-index:5;
                animation:tickerBlink 1.5s ease-in-out infinite;
                background:rgba(5,5,12,0.9);
                padding:2px 10px 2px 8px;
                border-radius:0 4px 4px 0;
            }
            .ticker-track {
                display:inline-flex;
                gap:0;
                animation:tickerMove 50s linear infinite;
                padding-left:90px;
            }
            .ticker-track:hover { animation-play-state:paused; }
            .ticker-item {
                display:inline-flex;
                align-items:center;
                gap:6px;
                padding:0 30px;
                font-size:12px;
                border-right:1px solid rgba(255,255,255,0.06);
                white-space:nowrap;
                letter-spacing:0.3px;
                color:#e0e0e0;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            }
        `;
        document.head.appendChild(style);
    }

    function esc(s) {
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    function formatTickerItem(event) {
        const name = esc(event.username || 'Player');
        switch (event.type) {
            case 'crash': {
                const mult = parseFloat(event.multiplier || 0).toFixed(1);
                const win  = parseFloat(event.win_amount || 0).toFixed(2);
                const color = mult >= 10 ? '#ff4444' : mult >= 5 ? '#ff69b4' : '#ffd700';
                return `<span class="ticker-item">🔥 <strong>${name}</strong> cashed out <strong style="color:${color};">${mult}×</strong> on Crash &nbsp;<span style="color:#555;">(+$${win})</span></span>`;
            }
            case 'slots': {
                const win = parseFloat(event.win_amount || 0).toFixed(2);
                const emoji = win >= 10000 ? '💎' : win >= 2000 ? '💰' : '🎰';
                return `<span class="ticker-item">${emoji} <strong>${name}</strong> hit <strong style="color:#4caf50;">$${win}</strong> jackpot on Slots</span>`;
            }
            case 'mines': {
                const tiles = event.tiles_cleared || 0;
                const mult  = parseFloat(event.multiplier || 0).toFixed(1);
                return `<span class="ticker-item">🏆 <strong>${name}</strong> cleared <strong style="color:#4caf50;">${tiles}</strong> safe tiles on Mines &nbsp;<span style="color:#555;">${mult}×</span></span>`;
            }
            case 'case_open': {
                const emoji = event.rarity_emoji || '📦';
                const color = event.rarity === 'Gold' ? '#ffd700' : event.rarity === 'Red' ? '#ff4444' : '#ff69b4';
                return `<span class="ticker-item">${emoji} <strong>${name}</strong> unboxed <strong style="color:${color};">${esc(event.item_name || 'item')}</strong></span>`;
            }
            case 'coinflip_win':
            case 'dice_win': {
                const win  = parseFloat(event.win_amount || 0).toFixed(2);
                const game = event.type === 'coinflip_win' ? 'Coinflip' : 'Dice';
                return `<span class="ticker-item">🎲 <strong>${name}</strong> won <strong style="color:#4caf50;">$${win}</strong> on ${game}</span>`;
            }
            default:
                return `<span class="ticker-item">⚡ <strong>${name}</strong> won big</span>`;
        }
    }

    async function loadLiveTicker() {
        try {
            const data = await fetch('/api/lobby-ticker?limit=20').then(r => r.json());
            const track = document.getElementById('tickerTrack');
            if (!track) return;
            if (!Array.isArray(data) || !data.length) {
                track.innerHTML = '<span class="ticker-item" style="color:#555;">Waiting for players…</span>';
                return;
            }
            const items = data.map(formatTickerItem);
            const sep = '<span class="ticker-item" style="color:#333;border:none;">✦</span>';
            const html = [...items, sep, ...items, sep, ...items].join('');
            track.innerHTML = html;
            track.style.animationDuration = (items.length * 5) + 's';
        } catch(e) { /* silently ignore ticker errors */ }
    }

    function init() {
        if (!document.getElementById('liveTicker')) return;
        injectStyles();
        loadLiveTicker();
        setInterval(loadLiveTicker, 20000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
