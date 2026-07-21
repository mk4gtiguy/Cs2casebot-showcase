
    const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    // ============================================
    // SECTION 1: CONFIGURATION & CONSTANTS
    // ============================================
    const SPIN_SPEEDS = {
        chill: { duration: 4500, label: '🐢 Chill' },
        normal: { duration: 3500, label: '⚡ Normal' },
        fast: { duration: 2500, label: '🚀 Fast' }
    };
    const RARITY_EMOJIS = { 'Gold': '⭐', 'Red': '🔴', 'Pink': '💗', 'Purple': '🟪', 'Blue': '🟦' };
    const RARITY_COLORS = { 'Gold': '#ffd700', 'Red': '#ff4444', 'Pink': '#ff69b4', 'Purple': '#aa00ff', 'Blue': '#4488ff' };
    const RARITY_GLOWS = {
        'Gold': '0 0 60px rgba(255,215,0,0.6)',
        'Red': '0 0 60px rgba(255,68,68,0.6)',
        'Pink': '0 0 60px rgba(255,105,180,0.6)',
        'Purple': '0 0 60px rgba(170,0,255,0.6)',
        'Blue': '0 0 60px rgba(68,136,255,0.6)'
    };
    // Real skin pools by rarity — used for reel filler items so images actually load
    const SKIN_POOLS_BY_RARITY = {"Blue": ["MP9 | Dry Season", "XM1014 | Copperflage", "G3SG1 | Arctic Camo", "FAMAS | Survivor Z", "M4A4 | Radiation Hazard", "P2000 | Royal Baroque", "Nova | Tempest", "AUG | Radiation Hazard", "P250 | Small Game", "MP9 | Buff Blue", "M4A4 | Converter", "P2000 | Coral Halftone", "P250 | Plum Netting", "P250 | Copper Oxide", "FAMAS | Yeti Camo", "XM1014 | Charter", "AWP | Sun in Leo", "MP7 | Asterion", "P90 | Desert Warfare", "FAMAS | Meow 36", "P250 | Dark Filigree", "P90 | Reef Grief", "Negev | Palm", "P250 | Forest Night", "P90 | Module", "MP7 | Orange Peel", "Negev | Infrastructure", "Negev | Boroque Sand", "M249 | Warbird", "M249 | Sage Camo", "XM1014 | Urban Perforated", "XM1014 | Oxide Blaze", "P90 | Desert DDPAT", "Nova | Plume", "XM1014 | Blue Tire", "P90 | Grim", "FAMAS | Night Borre", "XM1014 | Fallout Warning", "XM1014 | Irezumi", "MP7 | Anodized Navy", "Nova | Wood Fired", "Nova | Candy Apple", "MP7 | Short Ochre", "FAMAS | Teardown", "P90 | Cocoa Rampage", "G3SG1 | Violet Murano", "P90 | Sunset Lily", "P2000 | Pathfinder", "FAMAS | Decommissioned", "G3SG1 | Red Jasper", "MP7 | Gunsmoke", "Nova | Exo", "MP7 | Sunbaked", "M4A4 | Steel Work", "AWP | Snake Camo", "MP9 | Slide", "P2000 | Gnarled", "G3SG1 | Contractor", "M4A4 | Magnesium", "MP7 | Olive Plaid"], "Purple": ["MP7 | Powercore", "Nova | Ocular", "M4A4 | Griffin", "Nova | Toy Soldier", "MP7 | Amberline", "P90 | ScaraB Rush", "MP9 | Mount Fuji", "M4A4 | Red DDPAT", "P90 | Neoqueen", "P90 | Vent Rush", "MP7 | Impire", "XM1014 | Solitude", "P2000 | Space Race", "AWP | Exoskeleton", "Nova | Rising Sun", "XM1014 | Seasons", "MP9 | Goo", "AUG | Random Access", "AUG | Aristocrat", "Nova | Rising Skull", "P250 | Digital Architect", "Nova | Koi", "P90 | Death Grip", "M249 | Emerald Poison Dart", "Negev | Lionfish", "MP7 | Special Delivery", "XM1014 | Black Tie", "AUG | Arctic Wolf", "M4A4 | Etch Lord", "FAMAS | Valence", "Nova | Sobek's Bite", "P2000 | Amber Fade", "M4A4 | Sheet Lightning", "P2000 | Woodsman", "G3SG1 | High Seas", "MP9 | Bulldozer", "P250 | Cyber Shell", "MP9 | Ruby Poison Dart", "Nova | Baroque Orange", "AUG | Torque", "P90 | Blind Spot", "AWP | Atheris", "MP7 | Fade", "FAMAS | ZX Spectron", "AWP | Worm God", "XM1014 | Monster Melt", "P250 | Inferno", "P90 | Astral Jörmungandr", "Nova | Graphite", "P250 | Vino Primo", "FAMAS | Neural Net", "MP9 | Arctic Tri-Tone", "P2000 | Acid Etched", "M4A4 | Modern Hunter", "Negev | Power Loader", "P90 | Chopper", "MP9 | Hypnotic", "P250 | Red Rock", "AWP | Exothermic", "AUG | Flame Jörmungandr"], "Pink": ["AWP | Green Energy", "P90 | Run and Hide", "AWP | Corticera", "AUG | Momentum", "MP7 | Nemesis", "P250 | Undertow", "MP9 | Latte Rush", "P2000 | Imperial Dragon", "FAMAS | Meltdown", "P250 | Epicenter", "MP9 | Hydra", "MP9 | Airlock", "P90 | Shallow Grave", "M4A4 | Poseidon", "XM1014 | XOXO", "M4A4 | Desolate Space", "P250 | Cartel", "FAMAS | Djinn", "FAMAS | Waters of Nephthys", "AWP | Redline", "AUG | Syd Mead", "Negev | Mjölnir", "FAMAS | Afterimage", "FAMAS | Rapid Eye Movement", "G3SG1 | Flux", "P90 | Deathgaze", "AWP | Duality", "P90 | Cold Blooded", "P2000 | Ocean Foam", "XM1014 | Tranquility", "AUG | Stymphalian", "M4A4 | 龍王 (Dragon King)", "AWP | Crakow!", "P250 | Apep's Curse", "XM1014 | Entombed", "M4A4 | Hellfire", "P250 | Franklin", "MP9 | Food Chain", "P250 | Kintsugi", "M4A4 | Hellish", "M4A4 | Tooth Fairy", "AWP | Mortis", "AWP | BOOM", "MP7 | Abyssal Apparition", "P2000 | Corticera", "XM1014 | Incinegator", "P250 | Mehndi", "M4A4 | Cyber Security", "FAMAS | Mecha Industries", "AUG | Lil' Pig", "P250 | Visions", "P2000 | Wicked Sick", "AWP | Graphite", "AWP | The End", "AUG | Fleet Flock", "P250 | Muertos", "P90 | Trigon", "FAMAS | Eye of Athena", "AWP | Elite Build", "MP7 | Smoking Kills"], "Red": ["AWP | Chrome Cannon", "AWP | The Prince", "P90 | Death by Kitty", "AWP | Chromatic Aberration", "AWP | Medusa", "AWP | Man-o'-war", "P250 | See Ya Later", "AUG | Chameleon", "FAMAS | Commemoration", "AWP | Neo-Noir", "M4A4 | In Living Color", "P90 | Asiimov", "MP7 | Bloodsport", "AUG | Akihabara Accept", "M4A4 | Bullet Rain", "AWP | LongDog", "AWP | Hyper Beast", "AWP | Queen's Gambit", "AWP | CMYK", "M4A4 | Neo-Noir", "M4A4 | The Battlestar", "AWP | Oni Taiji", "M4A4 | The Coalition", "M4A4 | Temukau", "M4A4 | X-Ray", "FAMAS | Bad Trip", "M4A4 | Full Throttle", "MP9 | Starlight Protector", "P2000 | Fire Elemental", "AWP | Desert Hydra", "AWP | Dragon Lore", "AWP | Asiimov", "M4A4 | Royal Paladin", "M4A4 | Asiimov", "M4A4 | Buzz Kill", "AWP | Lightning Strike", "M4A4 | Eye of Horus", "AWP | Gungnir", "FAMAS | Roll Cage", "AWP | Printstream", "M4A4 | The Emperor", "M4A4 | Desert-Strike", "AWP | Fade", "AWP | Wildfire", "AWP | Containment Breach"], "Gold": ["M4A4 | Howl"]};
    // Flat array kept for backwards compat with anything that still refs ALL_WEAPONS
    const ALL_WEAPONS = SKIN_POOLS_BY_RARITY['Blue'].concat(SKIN_POOLS_BY_RARITY['Purple']);
    const STICKER_CAPSULES = [{"id": "cs20_sticker_capsule", "name": "CS20 Sticker Capsule", "emoji": "🎂", "price": 1.0, "image": "assets/containers/2103.webp", "sticker_count": 20}, {"id": "recoil_sticker_collection", "name": "Recoil Sticker Collection", "emoji": "⭐", "price": 0.5, "image": "assets/containers/2221.webp", "sticker_count": 32}, {"id": "austin_2025_champions_autograp", "name": "Austin 2025 Champions Autograph", "emoji": "🏆", "price": 5.0, "image": "assets/containers/1989.webp", "sticker_count": 20}, {"id": "budapest_2025_champions_autogr", "name": "Budapest 2025 Champions Autograph", "emoji": "🏆", "price": 5.0, "image": "assets/containers/2098.webp", "sticker_count": 20}, {"id": "copenhagen_2024_champions_auto", "name": "Copenhagen 2024 Champions Autograph", "emoji": "🏆", "price": 4.0, "image": "assets/containers/2112.webp", "sticker_count": 20}, {"id": "shanghai_2024_champions_autogr", "name": "Shanghai 2024 Champions Autograph", "emoji": "🏆", "price": 4.0, "image": "assets/containers/2156.webp", "sticker_count": 20}, {"id": "paris_2023_champions_autograph", "name": "Paris 2023 Champions Autograph", "emoji": "🏆", "price": 3.0, "image": "assets/containers/2138.webp", "sticker_count": 20}, {"id": "rio_2022_champions_autograph", "name": "Rio 2022 Champions Autograph", "emoji": "🏆", "price": 2.5, "image": "assets/containers/2149.webp", "sticker_count": 20}, {"id": "antwerp_2022_champions_autogra", "name": "Antwerp 2022 Champions Autograph", "emoji": "🏆", "price": 2.5, "image": "assets/containers/1982.webp", "sticker_count": 20}, {"id": "stockholm_2021_champions_autog", "name": "Stockholm 2021 Champions Autograph", "emoji": "🏆", "price": 2.0, "image": "assets/containers/2173.webp", "sticker_count": 15}, {"id": "boston_2018_legends_autograph", "name": "Boston 2018 Legends Autograph", "emoji": "🔥", "price": 1.5, "image": "assets/containers/2092.webp", "sticker_count": 120}, {"id": "london_2018_legends_autograph", "name": "London 2018 Legends Autograph", "emoji": "🔥", "price": 1.5, "image": "assets/containers/2130.webp", "sticker_count": 120}, {"id": "katowice_2019_legends_autograp", "name": "Katowice 2019 Legends Autograph", "emoji": "🔥", "price": 2.0, "image": "assets/containers/2125.webp", "sticker_count": 120}, {"id": "berlin_2019_legends_autograph", "name": "Berlin 2019 Legends Autograph", "emoji": "🔥", "price": 2.0, "image": "assets/containers/2087.webp", "sticker_count": 120}, {"id": "krakow_2017_legends_autograph", "name": "Krakow 2017 Legends Autograph", "emoji": "💫", "price": 1.5, "image": "assets/containers/2129.webp", "sticker_count": 120}, {"id": "krakow_2017_challengers_autogr", "name": "Krakow 2017 Challengers Autograph", "emoji": "💫", "price": 1.0, "image": "assets/containers/2128.webp", "sticker_count": 120}, {"id": "cologne_2016_legends_holo_foil", "name": "Cologne 2016 Legends (Holo/Foil)", "emoji": "✨", "price": 2.0, "image": "assets/containers/2199.webp", "sticker_count": 18}, {"id": "mlg_columbus_2016_legends_holo", "name": "MLG Columbus 2016 Legends (Holo/Foil)", "emoji": "✨", "price": 2.0, "image": "assets/containers/2197.webp", "sticker_count": 18}, {"id": "budapest_2025_challengers_stic", "name": "Budapest 2025 Challengers Sticker", "emoji": "🌟", "price": 1.5, "image": "assets/containers/2097.webp", "sticker_count": 36}, {"id": "copenhagen_2024_challengers_st", "name": "Copenhagen 2024 Challengers Sticker", "emoji": "🌟", "price": 1.5, "image": "assets/containers/2111.webp", "sticker_count": 36}];
    const STICKER_RARITIES = ['⭐', '✨', '💫', '🔥', '👑 Common', '👑 Rare', '👑 Epic', '👑 Legendary'];
    const STICKER_COLORS = {
        '⭐': '#4488ff', '✨': '#aa00ff', '💫': '#ff69b4', '🔥': '#ff4444',
        '👑 Common': '#00aa00', '👑 Rare': '#0066cc', '👑 Epic': '#aa00ff', '👑 Legendary': '#ffd700'
    };
    const SLOT_SYMBOLS = ['🍒', '🍋', '🍊', '🍇', '💎', '7️⃣', '🎰'];

    // ============================================
    // SECTION 2: STATE
    // ============================================
    const state = {
        userId: null,
        isGoogleUser: false,
        balance: 0,
        tickets: 0,
        inventory: [],
        currentPage: 0,
        totalPages: 0,
        PAGE_SIZE: 20,
        bulkQuantity: 1,
        capsuleBulkQuantity: 1,
        popupItems: [],
        popupIndex: 0,
        popupMode: null,
        popupCaseId: null,
        popupCasePrice: 0,
        currentMinesGame: null,
        selectedTradeIds: [],
        tradeRarity: null,
        gameHistory: [],
        userSettings: { theme: 'casino', spin_speed: 'normal', sound_enabled: true, confetti_mode: 'always' },
        streakData: { current_streak: 0, best_streak: 0, golds_in_streak: 0, total_opens: 0 }
    };
    let matchmakingWS = null;
    let favoriteIds = [];
    let casesMap = {};
    let autoAdvanceEnabled = localStorage.getItem('autoAdvance') === '1';
    let _autoAdvanceTimer = null;
    let powerupGuarantee = false;
    let powerupInsurance = false;
    let isOpening = false;
    let lastCapsuleId = null;
    let tradeItems = [];

    // ============================================
    // SECTION 2.5: SOUND ENGINE
    // ============================================
    let audioCtx = null;
    function getAudioCtx() {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (audioCtx.state === 'suspended') audioCtx.resume();
        return audioCtx;
    }
    function playTone(freq, duration, type = 'sine', volume = 0.15) {
        if (!state.userSettings.sound_enabled) return;
        try {
            const ctx = getAudioCtx();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = type;
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(volume, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(ctx.currentTime);
            osc.stop(ctx.currentTime + duration);
        } catch (e) { /* ignore */ }
    }
    function playReelTick() { playTone(600 + Math.random() * 200, 0.04, 'sine', 0.06); }
    function playReelSlowTick() { playTone(400 + Math.random() * 100, 0.08, 'sine', 0.08); }
    function playRevealBlue() { playTone(800, 0.3, 'sine', 0.12); }
    function playRevealPurple() { playTone(1000, 0.4, 'sine', 0.14); setTimeout(() => playTone(1200, 0.3, 'sine', 0.10), 150); }
    function playRevealPink() { playTone(1200, 0.4, 'sawtooth', 0.10); setTimeout(() => playTone(1500, 0.3, 'sawtooth', 0.08), 150); }
    function playRevealRed() { playTone(200, 0.6, 'square', 0.12); setTimeout(() => playTone(150, 0.4, 'square', 0.10), 200); }
    function playRevealGold() { playTone(100, 0.5, 'square', 0.20); setTimeout(() => playTone(200, 0.4, 'square', 0.15), 100); setTimeout(() => playTone(400, 0.3, 'sawtooth', 0.15), 200); setTimeout(() => playTone(800, 0.4, 'sawtooth', 0.12), 300); setTimeout(() => playTone(1200, 0.5, 'sine', 0.15), 400); }
    function playCaseOpen() { playTone(300, 0.15, 'square', 0.10); setTimeout(() => playTone(450, 0.10, 'square', 0.08), 100); }
    function playReelStop() { playTone(150, 0.3, 'square', 0.12); setTimeout(() => playTone(200, 0.2, 'square', 0.08), 150); }

    // ============================================
    // SECTION 3: API, TOAST, CONFETTI (unchanged)
    // ============================================
    async function apiCall(endpoint, options = {}) {
        const config = { credentials: 'include', headers: { 'Content-Type': 'application/json' }, ...options };
        try {
            const res = await fetch(endpoint, config);
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || data.detail || 'API request failed');
            return data;
        } catch (e) {
            console.error('API Error:', e);
            throw e;
        }
    }
    function showToast(message, type = 'info') {
        const existing = document.querySelector('.toast-notification');
        if (existing) existing.remove();
        const colors = { success: '#4caf50', error: '#ff4444', info: '#ffd700' };
        const toast = document.createElement('div');
        toast.className = 'toast-notification';
        toast.style.cssText = `background: ${colors[type] || colors.info}; color: ${type === 'info' ? '#0a0a0f' : 'white'};`;
        toast.textContent = message;
        document.body.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transition = 'opacity 0.5s ease';
            setTimeout(() => { if (toast.parentNode) toast.remove(); }, 500);
        }, 3000);
    }
    function spawnConfetti(rarity, count = 80, customColors = null) {
        if (state.userSettings.confetti_mode === 'never') return;
        if (state.userSettings.confetti_mode === 'gold' && rarity !== 'Gold' && !rarity.includes('Legendary') && !rarity.includes('Epic')) return;
        const container = document.getElementById('confettiContainer');
        let colors = customColors;
        if (!colors) {
            if (rarity === 'Gold' || rarity.includes('Legendary')) colors = ['#ffd700', '#ff6b00', '#ffcc00', '#ff9900', '#ffffff'];
            else if (rarity === 'Red' || rarity === 'Epic') colors = ['#ff4444', '#ff6b6b', '#ff0000', '#ff3333', '#ff8888'];
            else if (rarity === 'Pink' || rarity === 'Rare') colors = ['#ff69b4', '#ff1493', '#ff85c0', '#ffb6d9', '#ffc0cb'];
            else if (rarity === 'Purple' || rarity === 'Common') colors = ['#aa00ff', '#7b2fbe', '#9b59b6', '#d4a0ff', '#c084fc'];
            else colors = ['#4488ff', '#6db3f2', '#1a73e8', '#4fc3f7', '#81d4fa'];
        }
        for (let i = 0; i < count; i++) {
            const c = document.createElement('div');
            c.className = 'confetti';
            c.style.left = Math.random() * 100 + '%';
            c.style.top = '-10px';
            c.style.background = colors[Math.floor(Math.random() * colors.length)];
            c.style.width = (Math.random() * 10 + 4) + 'px';
            c.style.height = (Math.random() * 10 + 4) + 'px';
            c.style.borderRadius = Math.random() > 0.5 ? '50%' : '2px';
            c.style.transform = `rotate(${Math.random() * 360}deg)`;
            c.style.animationDuration = (Math.random() * 2 + 2) + 's';
            c.style.animationDelay = (Math.random() * 1.5) + 's';
            c.style.opacity = Math.random() * 0.8 + 0.2;
            container.appendChild(c);
        }
        setTimeout(() => { container.innerHTML = ''; }, 4000);
    }
    function spawnConfettiExplosion() { spawnConfetti('Gold', 200); setTimeout(() => spawnConfetti('Gold', 150), 500); setTimeout(() => spawnConfetti('Gold', 100), 1000); }
    function spawnRainbowConfetti() { const colors = ['#ff0000', '#ff8800', '#ffff00', '#00ff00', '#0088ff', '#8800ff', '#ff00ff']; spawnConfetti('Gold', 150, colors); }
    function spawnCoinShower(count = 20) {
        const container = document.getElementById('coinShower');
        const coins = ['🪙', '💰', '💎', '⭐', '🎉'];
        for (let i = 0; i < count; i++) {
            const coin = document.createElement('div');
            coin.className = 'coin';
            coin.textContent = coins[Math.floor(Math.random() * coins.length)];
            coin.style.left = Math.random() * 100 + '%';
            coin.style.top = '-20px';
            coin.style.fontSize = (Math.random() * 20 + 16) + 'px';
            coin.style.animationDuration = (Math.random() * 2 + 2) + 's';
            coin.style.animationDelay = (Math.random() * 1.5) + 's';
            container.appendChild(coin);
        }
        setTimeout(() => { container.innerHTML = ''; }, 4000);
    }
    function spawnParticles(rarity = 'Gold', count = 30) {
        const container = document.getElementById('particleContainer');
        const colors = {
            'Gold': ['#ffd700', '#ff6b00', '#ffcc00'],
            'Red': ['#ff4444', '#ff6b6b', '#ff0000'],
            'Pink': ['#ff69b4', '#ff1493', '#ff85c0'],
            'Purple': ['#aa00ff', '#7b2fbe', '#9b59b6'],
            'Blue': ['#4488ff', '#6db3f2', '#1a73e8']
        };
        const palette = colors[rarity] || colors['Gold'];
        for (let i = 0; i < count; i++) {
            const p = document.createElement('div');
            p.className = 'particle';
            p.style.left = (20 + Math.random() * 60) + '%';
            p.style.top = (20 + Math.random() * 60) + '%';
            p.style.background = palette[Math.floor(Math.random() * palette.length)];
            p.style.width = (Math.random() * 6 + 2) + 'px';
            p.style.height = (Math.random() * 6 + 2) + 'px';
            p.style.borderRadius = Math.random() > 0.5 ? '50%' : '2px';
            const angle = Math.random() * Math.PI * 2;
            const distance = 100 + Math.random() * 200;
            p.style.setProperty('--tx', Math.cos(angle) * distance + 'px');
            p.style.setProperty('--ty', Math.sin(angle) * distance - 100 + 'px');
            p.style.animationDuration = (Math.random() * 0.8 + 0.6) + 's';
            p.style.animationDelay = (Math.random() * 0.3) + 's';
            container.appendChild(p);
        }
        setTimeout(() => { container.innerHTML = ''; }, 2000);
    }

    // ============================================
    // SECTION 4: AUTH, SETTINGS, TABS (unchanged)
    // ============================================
    function loginDiscord() { window.location.href = '/auth/discord'; }
    function loginGoogle() { window.location.href = '/auth/google'; }
    function logout() { window.location.href = '/auth/logout'; }

    async function checkAuth() {
        try {
            const data = await apiCall('/api/user/me');
            state.userId = String(data.user_id);
            state.isGoogleUser = data.is_google || false;
            state.primaryProvider = data.primary_provider || 'discord';
            state.googleLinked = data.google_linked || false;
            state.balance = data.balance ?? 0;
            document.getElementById('balance').textContent = '$' + Number(state.balance).toFixed(2);
            document.getElementById('userInfo').classList.remove('hidden');
            try { applySiteMode(data.preferred_mode || 'cs2'); } catch(e) {}
            document.getElementById('userName').textContent = data.username || 'User';
            document.getElementById('authBadge').textContent = state.isGoogleUser ? '🔗 Google' : '🔗 Discord';
            // Use server-resolved avatar_url (handles both Discord and Google avatars)
            const avatarUrl = data.avatar_url || 'https://cdn.discordapp.com/embed/avatars/0.png';
            document.getElementById('userAvatar').src = avatarUrl;
            // Check for linked/error feedback in URL
            const urlParams = new URLSearchParams(window.location.search);
            if (urlParams.get('linked') === 'google') showToast('✅ Google account linked!', 'success');
            if (urlParams.get('linked') === 'discord') showToast('✅ Discord account linked!', 'success');
            if (urlParams.get('error') === 'google_already_linked') showToast('❌ That Google account is already linked to another user.', 'error');
            if (urlParams.get('error') === 'discord_already_linked') showToast('❌ That Discord account is already linked to another user.', 'error');
            if (urlParams.get('linked') || urlParams.get('error')) window.history.replaceState({}, '', '/');
            // Deep-link support: /?tab=inventory jumps straight to a tab (used
            // by the Discord bot's "manage this on the dashboard" links).
            const deepLinkTab = urlParams.get('tab');
            const validTabs = ['cases', 'inventory', 'trade', 'premium', 'quests', 'capsules', 'armory', 'achievements', 'games', 'profile', 'battles', 'friends'];
            if (deepLinkTab && validTabs.includes(deepLinkTab)) window.history.replaceState({}, '', '/');
            document.getElementById('loginSection').classList.add('hidden');
            document.getElementById('dashboard').classList.remove('hidden');
            try { maybeShowWelcomeModal(data.login_count); } catch(e) {}
            try { await loadSettings(); } catch(e) { console.warn('Settings:', e); }
            try { await checkMaintenanceUI(); } catch(e) { console.warn('Maintenance:', e); }
            try { await loadStreak(); } catch(e) { console.warn('Streak:', e); }
            try { await loadBalance(); } catch(e) { console.warn('Balance:', e); }
            try { await loadStats(); } catch(e) { console.warn('Stats:', e); }
            try { await loadCases(); } catch(e) { console.warn('Cases:', e); }
            try { await loadFeaturedCases(); } catch(e) { console.warn('Featured:', e); }
            try { await loadFavorites(); } catch(e) { console.warn('Favorites:', e); }
            try { await loadInventory(state.currentPage); } catch(e) { console.warn('Inventory:', e); }
            try { await loadLoadout(); } catch(e) { console.warn('Loadout:', e); }
            try { await loadLoadoutsList(); } catch(e) { console.warn('LoadoutsList:', e); }
            try { await loadTicketBalance(); } catch(e) { console.warn('Tickets:', e); }
            try { await loadQuests(); } catch(e) { console.warn('Quests:', e); }
            try { await loadCapsules(); } catch(e) { console.warn('Capsules:', e); }
            try { await loadVIPStatus(); } catch(e) { console.warn('Premium:', e); }
            try { await loadAchievements(); } catch(e) { console.warn('Achievements:', e); }
            try { await loadProfile(); } catch(e) { console.warn('Profile:', e); }
            try { await loadGameStats(); } catch(e) { console.warn('GameStats:', e); }
            try { await loadGoalData(); } catch(e) { console.warn('Goals:', e); }
            try { await maybeOpenDailySpin(); } catch(e) { console.warn('DailySpin:', e); }
            if (deepLinkTab && validTabs.includes(deepLinkTab)) { try { switchTab(deepLinkTab); } catch(e) { console.warn('DeepLinkTab:', e); } }
            return true;
        } catch (e) {
            console.error('Auth check error:', e);
            document.getElementById('loginSection').classList.remove('hidden');
            document.getElementById('dashboard').classList.add('hidden');
            return false;
        }
    }

    async function loadSettings() {
        try {
            const data = await apiCall('/api/user/settings');
            state.userSettings = data;
            document.getElementById('themeSelect').value = data.theme || 'casino';
            document.getElementById('speedSelect').value = data.spin_speed || 'normal';
            document.getElementById('confettiSelect').value = data.confetti_mode || 'always';
            document.getElementById('soundToggle').checked = data.sound_enabled !== false;
            document.getElementById('installPromptToggle').checked = localStorage.getItem('pwa_dismissed') !== '1';
            applyTheme(data.theme || 'casino');
        } catch (e) { console.error('Load settings error:', e); }
    }
    async function checkMaintenanceUI() {
        try {
            const res = await fetch('/api/admin/settings', { credentials: 'include' });
            if (!res.ok) return;
            const data = await res.json();
            const isMaintenance = data.settings.maintenance_mode === 'true';
            const banner = document.getElementById('maintenanceBanner');
            const meRes = await fetch('/api/user/me', { credentials: 'include' });
            const me = meRes.ok ? await meRes.json() : {};
            const isAdmin = me.is_admin || false;
            if (isMaintenance) {
                if (!banner) {
                    const newBanner = document.createElement('div');
                    newBanner.id = 'maintenanceBanner';
                    newBanner.style.cssText = 'position:fixed;top:0;left:0;width:100%;z-index:999999;background:linear-gradient(135deg,#ff4444,#cc0000);color:white;text-align:center;padding:15px 20px;font-family:Orbitron,sans-serif;font-weight:bold;font-size:16px;letter-spacing:1px;box-shadow:0 4px 30px rgba(255,0,0,0.3);border-bottom:3px solid #ffd700;';
                    newBanner.innerHTML = `🔧 <strong>MAINTENANCE MODE</strong> — ${esc(data.settings.maintenance_message || 'Please check back soon!')}`;
                    document.body.prepend(newBanner);
                    const container = document.querySelector('.container');
                    if (container) container.style.marginTop = '70px';
                }
                if (!isAdmin) {
                    document.querySelectorAll('.btn-primary, .btn-gold, .btn-success, .btn-danger, .case-btn, .premium-card').forEach(el => el.style.pointerEvents = 'none');
                    document.querySelectorAll('.btn, input, select, button').forEach(el => el.disabled = true);
                }
            } else {
                if (banner) banner.remove();
                const container = document.querySelector('.container');
                if (container) container.style.marginTop = '';
                document.querySelectorAll('.btn-primary, .btn-gold, .btn-success, .btn-danger, .case-btn, .premium-card').forEach(el => el.style.pointerEvents = '');
                document.querySelectorAll('.btn, input, select, button').forEach(el => el.disabled = false);
            }
        } catch (e) { console.error('Maintenance UI check error:', e); }
    }
    async function saveSettings() {
        const settings = {
            theme: document.getElementById('themeSelect').value,
            spin_speed: document.getElementById('speedSelect').value,
            confetti_mode: document.getElementById('confettiSelect').value,
            sound_enabled: document.getElementById('soundToggle').checked
        };
        try {
            await apiCall('/api/user/settings', { method: 'POST', body: JSON.stringify(settings) });
            state.userSettings = settings;
            applyTheme(settings.theme);
            showToast('✅ Settings saved!');
        } catch (e) { console.error('Save settings error:', e); }
    }
    function applyTheme(theme) {
        const themes = {
            'casino': { bg: '#0a0a0f', card: 'rgba(26,26,46,0.8)', accent: '#ffd700' },
            'neon': { bg: '#0a0a1a', card: 'rgba(20,20,60,0.8)', accent: '#00ff88' },
            'dark': { bg: '#05050a', card: 'rgba(15,15,25,0.8)', accent: '#8888ff' },
            'crystal': { bg: '#0a0f1a', card: 'rgba(20,30,60,0.8)', accent: '#44ddff' },
            'inferno': { bg: '#1a0a0a', card: 'rgba(60,20,20,0.8)', accent: '#ff4400' }
        };
        const t = themes[theme] || themes.casino;
        document.body.style.background = `radial-gradient(ellipse at top, ${t.bg} 0%, #0a0a0f 70%)`;
        document.querySelectorAll('.card').forEach(c => { c.style.background = t.card; });
    }
    function openSettings() {
      const modal = document.getElementById('settingsModal');
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      const firstFocusable = modal.querySelector('button, input, select, textarea, [tabindex]:not([tabindex="-1"])');
      if (firstFocusable) setTimeout(() => firstFocusable.focus(), 100);
      // Focus trap
      modal._focusTrap = function (e) {
        const focusable = modal.querySelectorAll('button, input, select, textarea, [tabindex]:not([tabindex="-1"])');
        if (!focusable.length) return;
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (e.key === 'Tab') {
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
        if (e.key === 'Escape') closeSettings();
      };
      document.addEventListener('keydown', modal._focusTrap);
    }
    function closeSettings() {
      const modal = document.getElementById('settingsModal');
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      if (modal._focusTrap) { document.removeEventListener('keydown', modal._focusTrap); }
      document.querySelector('.btn[onclick*="openSettings"]')?.focus();
    }

    function toggleDashTabBar(force) {
        const bar = document.getElementById('dashTabBar');
        const backdrop = document.getElementById('dashTabBarBackdrop');
        if (!bar) return;
        const open = force !== undefined ? force : !bar.classList.contains('open');
        bar.classList.toggle('open', open);
        if (backdrop) backdrop.classList.toggle('open', open);
    }
    window.toggleDashTabBar = toggleDashTabBar;

    function switchTab(tab) {
        toggleDashTabBar(false);
        document.querySelectorAll('.tab-bar .btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        const buttons = document.querySelectorAll('.tab-bar .btn');
        for (let i = 0; i < buttons.length; i++) {
            if (buttons[i].textContent.trim().toLowerCase().includes(tab.toLowerCase())) {
                buttons[i].classList.add('active');
                break;
            }
        }
        const tabElement = document.getElementById('tab-' + tab);
        if (tabElement) tabElement.classList.add('active');
        if (tab === 'cases') { loadFavorites(); loadCases(); }
        if (tab === 'inventory') { loadInventory(state.currentPage); loadLoadout(); }
        if (tab === 'trade') loadTradeTab();
        if (tab === 'premium') { loadVIPStatus(); loadTicketBalance(); loadReferralInfo(); }
        if (tab === 'quests') loadQuests();
        if (tab === 'capsules') loadCapsules();
        if (tab === 'achievements') loadAchievements();
        if (tab === 'games') loadGameStats();
        if (tab === 'profile') { loadProfile(); }
        if (tab === 'battles') { loadBattleHistory(); loadBattleFeeOptions(); }
        if (tab === 'friends') loadFriendsTab();
    }

    // Turbo League toggle (header pill). The mode itself isn't built yet --
    // 'turbo' just shows a coming-soon overlay over the existing dashboard.
    function applySiteMode(mode) {
        const overlay = document.getElementById('turboComingSoonOverlay');
        const btnCs2 = document.getElementById('modeBtnCs2');
        const btnTurbo = document.getElementById('modeBtnTurbo');
        if (!overlay || !btnCs2 || !btnTurbo) return;
        const isTurbo = mode === 'turbo';
        overlay.classList.toggle('hidden', !isTurbo);
        [[btnTurbo, isTurbo], [btnCs2, !isTurbo]].forEach(([btn, active]) => {
            btn.classList.toggle('active', active);
            btn.style.background = active ? 'linear-gradient(135deg,#ffd700,#f0a500)' : 'transparent';
            btn.style.color = active ? '#0a0a0f' : '#888';
            btn.style.fontWeight = active ? 'bold' : 'normal';
        });
        state.siteMode = mode;
    }
    window.applySiteMode = applySiteMode;

    async function setSiteMode(mode) {
        applySiteMode(mode);
        try { await apiCall('/api/user/mode', { method: 'POST', body: JSON.stringify({ mode }) }); }
        catch (e) { console.warn('setSiteMode error:', e); }
    }
    window.setSiteMode = setSiteMode;

    // ============================================
    // SECTION 5: BALANCE, TICKETS, STREAK
    // ============================================
    async function loadBalance() {
        try {
            const data = await apiCall('/api/user/me/balance');
            const el = document.getElementById('balance');
            const txt = '$' + data.balance.toFixed(2);
            if (state.balance !== void 0 && state.balance !== data.balance) {
                const prev = '$' + state.balance.toFixed(2);
                animateBalance(el, prev, txt, data.balance > state.balance);
            } else {
                el.textContent = txt;
            }
            state.balance = data.balance;
        } catch (e) { console.error('Load balance error:', e); document.getElementById('balance').textContent = '⚠️'; }
    }
    async function loadTicketBalance() {
        try {
            const data = await apiCall('/api/tickets/balance');
            state.tickets = data.tickets || 0;
            const el = document.getElementById('ticketBalance');
            if (el) el.textContent = state.tickets;
        } catch (e) { console.error('Load ticket balance error:', e); }
    }
    async function loadStreak() {
        try {
            const data = await apiCall('/api/user/streak');
            state.streakData = data;
        } catch (e) { console.error('Load streak error:', e); }
    }

    // ============================================
    // SECTION 6: CASES, FAVORITES (unchanged)
    // ============================================
    async function loadFeaturedCases() {
        try {
            const data = await apiCall('/api/cases/featured');
            const container = document.getElementById('featuredCarousel');
            container.innerHTML = data.featured.map(c => `
                <div class="featured-item" data-case-id="${c.id}" onclick="openCasePopup('${c.id}', ${state.bulkQuantity})">
                    <img src="/api/case-image/${c.id}" alt="${esc(c.name)}" onerror="this.style.display='none'">
                    <div class="name">${esc(c.name)}</div>
                    <div class="price">$${c.price.toFixed(2)}</div>
                </div>
            `).join('');
        } catch (e) { console.error('Load featured cases error:', e); }
    }
    async function loadFavorites() {
        try {
            const data = await apiCall('/api/user/favorites');
            favoriteIds = data.favorite_ids || [];
            document.getElementById('favCount').textContent = `(${data.count}/5)`;
            const section = document.getElementById('favoriteSection');
            const grid = document.getElementById('favoriteGrid');
            if (data.count > 0) {
                section.classList.remove('hidden');
                grid.innerHTML = data.favorites.map(c => `
                    <div class="case-btn" data-case-id="${c.id}" onclick="openCasePopup('${c.id}', ${state.bulkQuantity})">
                        <button class="remove-fav" onclick="event.stopPropagation(); removeFavorite('${c.id}')" title="Remove from favorites">✕</button>
                        <img src="/api/case-image/${c.id}" alt="${esc(c.name)}" onerror="this.style.display='none'">
                        <div class="name">${esc(c.name)}</div>
                        <div class="price">$${c.price.toFixed(2)}</div>
                        <button class="btn btn-primary btn-sm" style="margin-top:8px;font-size:9px;padding:4px 10px;">Open ${(!TERMINAL_THEMED_CASES.has(c.id) && state.bulkQuantity > 1) ? state.bulkQuantity : ''}</button>
                    </div>
                `).join('');
            } else {
                section.classList.add('hidden');
            }
            updateFavoriteStars();
        } catch (e) { console.error('Load favorites error:', e); }
    }
    async function toggleFavorite(caseId) {
        try {
            if (favoriteIds.includes(caseId)) await removeFavorite(caseId);
            else await addFavorite(caseId);
        } catch (e) { console.error('Toggle favorite error:', e); }
    }
    async function addFavorite(caseId) {
        try {
            const data = await apiCall('/api/user/favorites/add', { method: 'POST', body: JSON.stringify({ case_id: caseId }) });
            if (data.success) {
                favoriteIds = data.favorites;
                showToast(`⭐ Added to favorites! (${data.favorites.length}/5)`);
                await loadFavorites();
                await loadCases();
            } else {
                showToast(data.error || 'Failed to add favorite', 'error');
            }
        } catch (e) { console.error('Add favorite error:', e); showToast('❌ Error adding favorite', 'error'); }
    }
    async function removeFavorite(caseId) {
        try {
            const data = await apiCall('/api/user/favorites/remove', { method: 'POST', body: JSON.stringify({ case_id: caseId }) });
            if (data.success) {
                favoriteIds = data.favorites;
                showToast('🗑️ Removed from favorites');
                await loadFavorites();
                await loadCases();
            }
        } catch (e) { console.error('Remove favorite error:', e); showToast('❌ Error removing favorite', 'error'); }
    }
    function updateFavoriteStars() {
        document.querySelectorAll('.fav-star').forEach(el => {
            const caseId = el.dataset.caseId;
            if (favoriteIds.includes(caseId)) {
                el.classList.remove('inactive');
                el.classList.add('active');
                el.textContent = '⭐';
            } else {
                el.classList.remove('active');
                el.classList.add('inactive');
                el.textContent = '☆';
            }
        });
    }
    let _allCasesData = [];
    let _caseCategory = 'all';
    let _caseSearchTerm = '';
    const CASE_CATEGORY_LABELS = { all: 'All', case: '📦 Cases', souvenir: '🏆 Souvenir Packages' };

    async function loadCases() {
        try {
            const data = await apiCall('/api/cases');
            data.cases.forEach(c => { casesMap[c.id] = { name: c.name, price: c.price }; });
            _allCasesData = data.cases;

            Object.keys(CASE_CATEGORY_LABELS).forEach(cat => {
                const el = document.getElementById('casecat-count-' + cat);
                if (!el) return;
                const n = cat === 'all' ? _allCasesData.length : _allCasesData.filter(c => (c.category || 'case') === cat).length;
                el.textContent = `(${n})`;
            });

            renderCaseGrid();
            updateAutoAdvanceButton();
        } catch (e) { console.error('Load cases error:', e); }
    }

    function setCaseCategory(cat) {
        _caseCategory = cat;
        document.querySelectorAll('#caseCategoryTabs .btn').forEach(b => b.classList.remove('btn-primary', 'active'));
        const btn = document.getElementById('casecat-' + cat);
        if (btn) { btn.classList.add('btn-primary', 'active'); btn.classList.remove('btn-outline'); }
        document.querySelectorAll('#caseCategoryTabs .btn').forEach(b => { if (b !== btn) b.classList.add('btn-outline'); });
        renderCaseGrid();
    }

    function filterCases() {
        _caseSearchTerm = (document.getElementById('caseSearch').value || '').trim().toLowerCase();
        renderCaseGrid();
    }

    function renderCaseGrid() {
        const grid = document.getElementById('caseGrid');
        const emptyEl = document.getElementById('caseEmpty');
        const filtered = _allCasesData.filter(c => {
            const matchesCategory = _caseCategory === 'all' || (c.category || 'case') === _caseCategory;
            const matchesSearch = !_caseSearchTerm || c.name.toLowerCase().includes(_caseSearchTerm);
            return matchesCategory && matchesSearch;
        });

        if (!filtered.length) {
            grid.innerHTML = '';
            emptyEl.style.display = 'block';
            return;
        }
        emptyEl.style.display = 'none';

        grid.innerHTML = filtered.map(c => {
            const isFav = favoriteIds.includes(c.id);
            return `
                <div class="case-btn" data-case-id="${c.id}" onclick="openCasePopup('${c.id}', ${state.bulkQuantity})">
                    <span class="fav-star ${isFav ? 'active' : 'inactive'}" data-case-id="${c.id}" onclick="event.stopPropagation(); toggleFavorite('${c.id}')">${isFav ? '⭐' : '☆'}</span>
                    <img src="/api/case-image/${c.id}" alt="${esc(c.name)}" onerror="this.style.display='none'">
                    <div class="name">${esc(c.name)}</div>
                    <div class="price">$${c.price.toFixed(2)}</div>
                    <button class="btn btn-primary btn-sm" style="margin-top:8px;font-size:9px;padding:4px 10px;">Open ${(!TERMINAL_THEMED_CASES.has(c.id) && state.bulkQuantity > 1) ? state.bulkQuantity : ''}</button>
                </div>
            `;
        }).join('');
    }
    function setBulkQuantity(qty) {
        state.bulkQuantity = qty;
        document.querySelectorAll('.case-btn, .featured-item').forEach(btn => {
            const caseId = btn.dataset.caseId;
            if (caseId) btn.onclick = function() { openCasePopup(caseId, qty); };
            const openBtn = btn.querySelector('.btn-sm');
            if (openBtn) openBtn.textContent = qty > 1 ? 'Open ' + qty : 'Open';
        });
        const discount = {1:0,5:5,10:10,15:15,20:20,25:25}[qty] || 0;
        document.getElementById('bulkDiscount').textContent = discount > 0 ? `(${discount}% discount)` : '';
        document.querySelectorAll('#tab-cases .btn-sm').forEach(b => b.classList.remove('btn-primary', 'active'));
        const qtyBtn = document.getElementById('qty' + qty);
        if (qtyBtn) qtyBtn.classList.add('btn-primary', 'active');
    }

    // ============================================
    // SECTION 7: NEW CS2-STYLE REEL FUNCTIONS (FIXED IMAGE MAPPING)
    // ============================================
    // Cases that use the offer-based terminal flow (buy/skip a sequence of
    // specific priced items) instead of the standard random-reel case-opening
    // flow -- matches how real CS2 terminals like the Dead Hand Terminal work.
    const TERMINAL_THEMED_CASES = new Set(['dead_hand_terminal']);

    function openCasePopup(caseId, quantity = 1) {
        if (isOpening) return;
        if (TERMINAL_THEMED_CASES.has(caseId)) {
            openTerminalPopup(caseId);
            return;
        }
        isOpening = true;
        state.popupMode = 'case';
        state.popupCaseId = caseId;
        state.popupCasePrice = casesMap[caseId]?.price || 0;
        state.popupIndex = 0;
        state.popupItems = [];
        const overlay = document.getElementById('popupOverlay');
        overlay.classList.add('show');
        document.getElementById('popupContent').classList.remove('terminal-theme');
        document.getElementById('popupBody').innerHTML = `<div class="loading" style="font-size:18px;padding:40px;">Opening case${quantity > 1 ? 'es' : ''}...</div>`;
        openCaseBatch(caseId, quantity).finally(() => { isOpening = false; });
    }

    // ============================================
    // TERMINAL OFFER FLOW (Dead Hand Terminal etc.)
    // Free to "insert"; the terminal price is paid once on /terminal/open.
    // It then shows 5 offers one at a time -- BUY (pay that offer's price,
    // item goes to inventory) or SKIP (free, move to the next offer). After
    // the 5th offer is resolved the terminal is spent.
    // ============================================
    const TERMINAL_BOOT_LINES = [
        '> ACCESSING TERMINAL...',
        '> AUTHENTICATING UPLINK...',
        '> DECRYPTING PAYLOAD...',
        '> READY.'
    ];

    async function openTerminalPopup(caseId) {
        if (isOpening) return;
        isOpening = true;
        state.popupMode = 'terminal';
        state.popupCaseId = caseId;
        const overlay = document.getElementById('popupOverlay');
        overlay.classList.add('show');
        document.getElementById('popupContent').classList.add('terminal-theme');
        document.getElementById('popupBody').innerHTML = `<div class="terminal-boot" id="terminalBoot">
            ${TERMINAL_BOOT_LINES.map((line, i) => `<div class="terminal-boot-line" style="animation-delay:${i * 320}ms">${esc(line)}</div>`).join('')}
        </div>`;
        try {
            // Resume an already-active terminal (e.g. user closed the popup mid-run)
            // instead of erroring, so a paid-for terminal is never stranded.
            const existing = await apiCall('/api/terminal/session', { method: 'GET' });
            const data = (existing.session && existing.session.case_id === caseId)
                ? existing.session
                : await apiCall('/api/terminal/open', { method: 'POST', body: JSON.stringify({ case_id: caseId }) });
            await loadBalance();
            setTimeout(() => renderTerminalOffer(data), TERMINAL_BOOT_LINES.length * 320 + 250);
        } catch (e) {
            document.getElementById('popupBody').innerHTML = `<div class="error" style="font-size:18px;padding:20px;">❌ ${e.message || 'Failed to open terminal'}<br><br><button class="btn btn-primary" onclick="closePopup()">Close</button></div>`;
        } finally {
            isOpening = false;
        }
    }

    function renderTerminalOffer(session) {
        state.currentTerminalSession = session;
        if (session.status === 'completed' || !session.current_offer) {
            document.getElementById('popupBody').innerHTML = `
                <div class="terminal-boot" style="padding:30px 20px;">
                    <div class="terminal-boot-line" style="animation-delay:0ms">> TERMINAL DEPLETED.</div>
                    <div class="terminal-boot-line" style="animation-delay:250ms">> ${session.items_bought} item(s) acquired, $${Number(session.total_spent).toFixed(2)} spent.</div>
                </div>
                <button class="btn btn-primary" onclick="closePopup()" style="margin-top:10px;">Close</button>
            `;
            return;
        }
        const offer = session.current_offer;
        const rarityClass = (offer.rarity || 'blue').toLowerCase();
        const imgSrc = offer.image_filename ? `/static/images/skins/${offer.image_filename}` : `/api/skin-image?name=${encodeURIComponent(offer.name)}`;
        document.getElementById('popupBody').innerHTML = `
            <div style="margin-bottom:6px;color:#00ff41;font-family:'Courier New',monospace;font-size:12px;">&gt; OFFER ${offer.index + 1} OF ${session.total_offers}</div>
            <div class="terminal-offer-card rarity-${rarityClass}" id="terminalOfferCard">
                <img class="terminal-offer-img" src="${imgSrc}" alt="${esc(offer.display_name)}" onerror="this.src='/static/images/Default CS2 Weapons/weapon_ak47.png'">
                <div class="terminal-offer-name">${esc(offer.display_name)}</div>
                <div class="terminal-offer-meta">${esc(offer.condition || '')}${offer.is_stattrak ? ' · StatTrak™' : ''}</div>
                <div class="terminal-offer-price">$${Number(offer.price).toFixed(2)}</div>
            </div>
            <div style="display:flex;gap:10px;margin-top:16px;justify-content:center;">
                <button class="btn btn-primary" onclick="terminalDecide('buy')">💰 BUY — $${Number(offer.price).toFixed(2)}</button>
                <button class="btn" onclick="terminalDecide('skip')">SKIP ▶</button>
            </div>
        `;
    }

    async function terminalDecide(action) {
        if (isOpening) return;
        const session = state.currentTerminalSession;
        if (!session) return;
        isOpening = true;
        try {
            const data = await apiCall('/api/terminal/decide', {
                method: 'POST',
                body: JSON.stringify({ session_id: session.session_id, action })
            });
            if (action === 'buy') {
                await loadBalance();
                await loadInventory(state.currentPage);
                await loadStats();
            }
            renderTerminalOffer(data);
        } catch (e) {
            document.getElementById('popupBody').innerHTML = `<div class="error" style="font-size:18px;padding:20px;">❌ ${e.message || 'Something went wrong'}<br><br><button class="btn btn-primary" onclick="closePopup()">Close</button></div>`;
        } finally {
            isOpening = false;
        }
    }

    async function openCaseBatch(caseId, quantity) {
        const useGuarantee = powerupGuarantee;
        const useInsurance = powerupInsurance;
        // Reset power-ups before the API call so they don't linger on error
        powerupGuarantee = false;
        powerupInsurance = false;
        updatePowerupUI();
        try {
            const data = await apiCall('/api/open-case', {
                method: 'POST',
                body: JSON.stringify({ case_id: caseId, quantity: quantity, use_guarantee: useGuarantee, use_insurance: useInsurance })
            });
            if (data.success) {
                state.popupItems = data.items;
                state.popupCaseId = caseId;
                const hasGold = state.popupItems.some(item => item.rarity === 'Gold');
                try {
                    await apiCall('/api/user/streak/update', {
                        method: 'POST',
                        body: JSON.stringify({ case_id: caseId, rarity: hasGold ? 'Gold' : 'Blue', is_gold: hasGold })
                    });
                } catch (e) {}
                await loadStreak();
                showReelForItem(data.items[0], 0, data.items.length);
                await loadBalance();
                await loadStats();
                await loadInventory(state.currentPage);
                await loadQuests();
            } else {
                document.getElementById('popupBody').innerHTML = `<div class="error" style="font-size:18px;padding:20px;">❌ ${data.error || 'Failed to open case'}<br><br><button class="btn btn-primary" onclick="closePopup()">Close</button></div>`;
            }
        } catch (e) {
            document.getElementById('popupBody').innerHTML = `<div class="error" style="font-size:18px;padding:20px;">❌ Error opening case: ${e.message || 'Unknown error'}<br><br><button class="btn btn-primary" onclick="closePopup()">Close</button></div>`;
        }
    }

    function buildReelItems(winner) {
        const totalItems = 28;
        const winnerPos = 13;
        const items = [];
        const rarities = ['Blue', 'Purple', 'Pink', 'Red', 'Gold'];
        const rarityWeights = [50, 25, 15, 8, 2];
        function randomRarity() {
            const total = rarityWeights.reduce((a, b) => a + b, 0);
            let r = Math.random() * total;
            for (let i = 0; i < rarityWeights.length; i++) {
                r -= rarityWeights[i];
                if (r <= 0) return rarities[i];
            }
            return 'Blue';
        }
        function randomWeapon(rarity) {
            // Use real skin names by rarity so images actually load in the reel
            const pool = SKIN_POOLS_BY_RARITY[rarity] || SKIN_POOLS_BY_RARITY['Blue'];
            const name = pool[Math.floor(Math.random() * pool.length)];
            return { name, display_name: name, rarity };
        }
        for (let i = 0; i < totalItems; i++) {
            if (i === winnerPos) {
                items.push({ ...winner, role: 'winner' });
            } else {
                const rarity = randomRarity();
                const w = randomWeapon(rarity);
                items.push({ name: w.name, rarity: w.rarity, role: 'filler' });
            }
        }
        // near‑miss 1
        const nearMissRarity = ['Purple', 'Pink', 'Red', 'Gold'][Math.floor(Math.random() * 4)];
        const nearMissPos = winnerPos - 2 - Math.floor(Math.random() * 2);
        if (nearMissPos >= 0 && nearMissPos < totalItems && nearMissPos !== winnerPos) {
            const nm = randomWeapon(nearMissRarity);
            items[nearMissPos] = { name: nm.name, rarity: nm.rarity, role: 'near-miss' };
        }
        // near‑miss 2
        if (Math.random() < 0.3) {
            const nmRarity2 = ['Purple', 'Pink', 'Red'][Math.floor(Math.random() * 3)];
            const nmPos2 = winnerPos + 2 + Math.floor(Math.random() * 2);
            if (nmPos2 < totalItems && nmPos2 !== winnerPos) {
                const nm2 = randomWeapon(nmRarity2);
                items[nmPos2] = { name: nm2.name, rarity: nm2.rarity, role: 'near-miss' };
            }
        }
        // 5% chance a Gold passes by
        if (Math.random() < 0.05 && winner.rarity !== 'Gold') {
            const goldPos = winnerPos - 4 - Math.floor(Math.random() * 3);
            if (goldPos >= 0 && goldPos < totalItems && goldPos !== winnerPos) {
                const goldW = randomWeapon('Gold');
                items[goldPos] = { name: goldW.name, rarity: 'Gold', role: 'near-miss' };
            }
        }
        return items;
    }

    function showReelForItem(item, index, total) {
        state.popupIndex = index; // single source of truth for "which item is on screen" -- used by skipToEnd
        const isSingle = total === 1;
        const reelItems = buildReelItems(item);
        const winnerPos = 13;
        const html = `
            <div style="margin-bottom:6px;color:#888;font-size:12px;">${!isSingle ? `Opening ${index + 1} of ${total}` : ''}<span style="margin-left:12px;color:#ffd700;">🔥 Streak: ${state.streakData.current_streak || 0}</span></div>
            <div class="reel-wrapper" id="reelWrapper">
                <div class="center-marker" id="centerMarker"></div>
                <div class="reel-overlay-left"></div>
                <div class="reel-overlay-right"></div>
                <div class="reel-track" id="reelTrack">
                    ${reelItems.map((ri, i) => {
                        // For image: always use the clean name (no emoji, no category prefix)
                        // ri.name is the clean "Weapon | Skin" name; ri.image_url is direct path if available
                        const cleanName = (ri.name || '').replace(/^[\p{Emoji}\s🟦🟪🟥🩷⭐👑💛🔥]+/gu, '').trim();
                        const imgSrc = ri.image_url
                            ? ri.image_url
                            : `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;
                        // For display: show the clean name only (no emoji prefix)
                        const displayText = cleanName || ri.name || '';
                        // Bug 165 fix: escape item name and rarity before injecting into innerHTML
                        return `<div class="reel-item" data-index="${i}" data-role="${ri.role || ''}" data-rarity="${esc(ri.rarity || '')}">
                            <img class="reel-img" src="${imgSrc}" alt="${esc(displayText)}" onerror="this.src='/static/images/Default CS2 Weapons/weapon_ak47.png'">
                            <div class="reel-name">${esc(displayText)}</div>
                            <div class="reel-rarity-label">${esc(ri.rarity || 'Common')}</div>
                        </div>`;
                    }).join('')}
                </div>
            </div>
            <div id="reelStatus" style="margin:10px 0;font-size:14px;color:#888;min-height:30px;">🎰 Spinning...</div>
            <div id="reelButtons" style="display:none;margin-top:6px;"></div>
        `;
        document.getElementById('popupBody').innerHTML = html;
        playCaseOpen();
        startReelSpin(item, winnerPos, index, total);
    }

    function startReelSpin(winner, winnerPos, index, total) {
        const track = document.getElementById('reelTrack');
        const marker = document.getElementById('centerMarker');
        const status = document.getElementById('reelStatus');
        if (!track) return;
        const itemWidth = 126;
        const containerWidth = track.parentElement.clientWidth || 600;
        const centerOffset = (containerWidth / 2) - (itemWidth / 2);
        let targetX = -(winnerPos * itemWidth) + centerOffset;
        const extraScroll = (20 + Math.floor(Math.random() * 15)) * itemWidth;
        const startX = targetX - extraScroll;
        track.style.transition = 'none';
        track.style.transform = `translateX(${startX}px)`;
        void track.offsetHeight;
        const speedKey = state.userSettings.spin_speed || 'normal';
        const speedConfig = SPIN_SPEEDS[speedKey] || SPIN_SPEEDS.normal;
        const duration = speedConfig.duration + (Math.random() * 600 - 300);
        let tickInterval = setInterval(() => {
            if (!track) { clearInterval(tickInterval); return; }
            if (status.textContent.includes('Spinning')) playReelTick();
            else clearInterval(tickInterval);
        }, 120);
        track.style.transition = `transform ${duration}ms cubic-bezier(0.08, 0.82, 0.15, 0.95)`;
        track.style.transform = `translateX(${targetX}px)`;
        let slowTickTimeout = setTimeout(() => {
            if (track && status.textContent.includes('Spinning')) playReelSlowTick();
        }, duration * 0.7);
        setTimeout(() => {
            clearInterval(tickInterval);
            clearTimeout(slowTickTimeout);
            const bounceAmount = 8 + Math.random() * 12;
            const bounceDir = Math.random() > 0.5 ? 1 : -1;
            const bounceX = targetX + (bounceDir * bounceAmount);
            track.style.transition = `transform 140ms cubic-bezier(0.2, 1.2, 0.3, 1)`;
            track.style.transform = `translateX(${bounceX}px)`;
            setTimeout(() => {
                track.style.transition = `transform 120ms cubic-bezier(0.3, 0.1, 0.5, 1)`;
                track.style.transform = `translateX(${targetX}px)`;
                setTimeout(() => {
                    playReelStop();
                    revealWinner(winner, index, total);
                }, 130);
            }, 160);
        }, duration + 100);
    }

    function revealWinner(winner, index, total) {
        const status = document.getElementById('reelStatus');
        const marker = document.getElementById('centerMarker');
        const items = document.querySelectorAll('.reel-item');
        let winnerEl = null;
        items.forEach(el => { if (el.dataset.role === 'winner') winnerEl = el; });
        if (!winnerEl) {
            items.forEach(el => { if (el.dataset.rarity === winner.rarity) winnerEl = el; });
        }
        if (marker) marker.classList.add('active');
        status.textContent = '⏳ ...';
        setTimeout(() => {
            const rarity = winner.rarity || 'Blue';
            const revealClass = `reveal-${rarity.toLowerCase()}`;
            if (winnerEl) {
                winnerEl.classList.add(revealClass);
                winnerEl.style.transition = 'transform 0.4s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow 0.4s ease';
                winnerEl.style.transform = 'scale(1.12)';
                winnerEl.style.zIndex = '20';
                setTimeout(() => { winnerEl.style.transform = 'scale(1.04)'; }, 400);
            }
            const soundMap = { 'Blue': playRevealBlue, 'Purple': playRevealPurple, 'Pink': playRevealPink, 'Red': playRevealRed, 'Gold': playRevealGold };
            if (soundMap[rarity]) soundMap[rarity]();
            if (rarity === 'Gold') {
                spawnConfettiExplosion();
                spawnRainbowConfetti();
                spawnCoinShower(50);
                spawnParticles('Gold', 60);
                status.innerHTML = `<span style="color:#ffd700;font-size:22px;font-weight:900;">⭐ GOLD! ⭐</span>`;
            } else if (rarity === 'Red') {
                spawnConfetti('Red', 120);
                spawnCoinShower(30);
                spawnParticles('Red', 40);
                status.innerHTML = `<span style="color:#ff4444;font-size:20px;font-weight:700;">🔴 RED!</span>`;
            } else if (rarity === 'Pink') {
                spawnConfetti('Pink', 100);
                spawnParticles('Pink', 30);
                status.innerHTML = `<span style="color:#ff69b4;font-size:18px;font-weight:700;">💗 PINK!</span>`;
            } else if (rarity === 'Purple') {
                spawnConfetti('Purple', 80);
                spawnParticles('Purple', 20);
                status.innerHTML = `<span style="color:#aa00ff;font-size:18px;font-weight:700;">🟪 PURPLE!</span>`;
            } else {
                status.innerHTML = `<span style="color:#4488ff;font-size:16px;">🟦 Blue</span>`;
            }
            const multiplier = isBigWinMultiplier(winner);
            if (multiplier >= 10) {
                showBigWinOverlay(winner.price, multiplier);
                setTimeout(() => {
                    showInspectScreen(winner, index, total);
                }, 5100);
            } else {
                setTimeout(() => {
                    showInspectScreen(winner, index, total);
                }, 700);
            }
        }, 300);
    }

    // openInspectModal() only ever looks items up in state.inventory (by id),
    // using the inventory grid's own field names (item_name, float_value,
    // ...). A just-opened case/capsule item never makes it into that array
    // (it only gets refreshed by a full loadInventory() call) and uses
    // different field names (name, float, ...) from the open-case/capsule
    // API response -- so "click to inspect" on the reveal card or the
    // bulk/session summary cards silently did nothing. Normalizing and
    // merging each newly-won item into state.inventory as it's rendered
    // (skipping ones already present) lets openInspectModal find it with
    // the exact same shape it already knows how to render.
    function cacheOpenedItemForInspect(item) {
        if (!item || item.id == null) return;
        state.inventory = state.inventory || [];
        if (state.inventory.some(i => String(i.id) === String(item.id))) return;
        const isSticker = state.popupMode === 'capsule';
        state.inventory.unshift({
            id: item.id,
            item_name: item.name || item.item_name || '',
            item_type: isSticker ? 'sticker' : 'weapon',
            rarity: item.rarity,
            price: item.price,
            condition: item.condition || null,
            float_value: item.float != null ? item.float : (item.float_value != null ? item.float_value : null),
            is_stattrak: !!item.is_stattrak,
            image_url: item.image_url || (item.image_filename ? `/static/images/skins/${item.image_filename}` : null),
            applied_stickers: item.applied_stickers || [],
            protected: false,
            in_loadout: false,
            status: 'kept',
        });
    }

    function showInspectScreen(item, index, total) {
        clearTimeout(_autoAdvanceTimer);
        cacheOpenedItemForInspect(item);
        const status = document.getElementById('reelStatus');
        const buttonsDiv = document.getElementById('reelButtons');
        const rarity = item.rarity || 'Blue';
        const isCapsuleItem = state.popupMode === 'capsule';
        const color = isCapsuleItem ? (STICKER_COLORS[rarity] || '#888') : (RARITY_COLORS[rarity] || '#888');
        const emoji = isCapsuleItem ? '' : (RARITY_EMOJIS[rarity] || '🎯');
        // Use stored image_url directly when available; fall back to API with clean name
        const cleanName = (item.name || '').replace(/^[\u{1F300}-\u{1FFFF}\u{2600}-\u{27BF}\u{FE00}-\u{FEFF}🟦🟪🟥🟨🟩⬛⬜🟫🔥⭐💫👑✨\s]+/gu, '').trim();
        const imgUrl = item.image_url
            ? item.image_url
            : `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;
        const displayLabel = item.display_name || cleanName || item.name;
        const isSingle = total === 1;
        const isLast = index === total - 1;
        const imgErrorHandler = isCapsuleItem ? "this.style.display='none'" : "this.src='/static/images/Default CS2 Weapons/weapon_ak47.png'";
        status.innerHTML = '';
        if (isCapsuleItem) {
            // The capsule-crack stage (its "OPENING CAPSULE..." header and the
            // "Breaking..."/"Spinning..." status line) has no further purpose
            // once the reveal card is ready -- hide it so the card can take up
            // the full space instead of both being visible stacked together.
            const stage = document.getElementById('capsuleStage');
            if (stage) stage.style.display = 'none';
        }
        const html = `
            <div class="inspect-screen">
                <div class="inspect-card" style="border-color:${color};box-shadow:0 0 60px ${color}22;">
                    <img class="inspect-weapon" src="${imgUrl}" alt="${esc(displayLabel)}" onerror="${imgErrorHandler}" style="cursor:pointer;" title="Click to inspect" onclick="openInspectModal('${item.id}')">
                    <div style="font-size:9px;color:#666;letter-spacing:0.5px;margin-top:-4px;margin-bottom:2px;">🔍 Click to inspect</div>
                    <div class="inspect-name" style="color:${color};">${esc(displayLabel)}</div>
                    <div class="inspect-rarity" style="color:${color};">${emoji ? emoji + ' ' : ''}${rarity}</div>
                    <div class="inspect-details">
                        <div><span class="label">💰 Value</span><br><span class="value">$${item.price.toFixed(2)}</span></div>
                        ${item.condition ? `<div><span class="label">🔧 Condition</span><br><span class="value">${condBadgeHtml(item.condition) || item.condition}</span></div>` : ''}
                        ${item.float !== undefined && item.float !== null ? `<div><span class="label">🎯 Float</span><br><span class="value">${Number(item.float).toFixed(4)}</span></div>` : ''}
                        ${item.is_stattrak ? '<div><span class="label">🔥 StatTrak</span><br><span class="value">Yes</span></div>' : ''}
                        <div><span class="label">📊 Rarity</span><br><span class="value">${rarity}</span></div>
                        <div><span class="label">🏷️ ID</span><br><span class="value">${item.id || 'N/A'}</span></div>
                    </div>
                    <div class="inspect-buttons">
                        <button class="btn btn-success" onclick="handlePopupAction('keep', ${index})">💾 Keep</button>
                        <button class="btn btn-danger" onclick="handlePopupAction('sell', ${index})">💰 Sell (70%)</button>
                        ${isLast ? `<button class="btn btn-primary" onclick="handlePopupAction('done', ${index})">✅ Done</button>` : `<button class="btn btn-primary" onclick="handlePopupAction('next', ${index})">▶ Next</button>`}
                        ${isSingle ? `<button class="btn btn-gold" onclick="handlePopupAction('spin_again', ${index})">🎰 Open Again</button>` : ''}
                    </div>
                    ${!isSingle ? `
                        <div style="margin-top:12px;color:#888;font-size:11px;display:flex;align-items:center;justify-content:center;gap:10px;flex-wrap:wrap;">
                            <span>Item ${index + 1} of ${total}</span>
                            <button class="btn btn-sm btn-outline" onclick="skipToEnd()" style="font-size:9px;padding:4px 10px;">⏭️ Skip All</button>
                            <label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:10px;">
                                <input type="checkbox" onchange="toggleAutoAdvance(this)" ${autoAdvanceEnabled ? 'checked' : ''}>
                                ⏩ Auto-advance
                            </label>
                        </div>` : ''}
                </div>
            </div>
        `;
        buttonsDiv.innerHTML = html;
        buttonsDiv.style.display = 'block';
        if (!isSingle && autoAdvanceEnabled) {
            _autoAdvanceTimer = setTimeout(() => {
                handlePopupAction(isLast ? 'done' : 'next', index);
            }, 1600);
        }
    }

    function summaryItemCardHtml(item) {
        cacheOpenedItemForInspect(item);
        const cleanName = cleanItemName(item.name || '');
        const imgUrl = item.image_url || `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;
        return `
            <div style="text-align:center;padding:6px;border-radius:6px;background:rgba(255,255,255,0.02);cursor:pointer;" title="Click to inspect" onclick="openInspectModal('${item.id}')">
                <img src="${imgUrl}" alt="${esc(item.display_name || item.name)}" style="width:100%;aspect-ratio:1;object-fit:contain;border-radius:4px;background:rgba(0,0,0,0.25);margin-bottom:4px;" onerror="this.src='/static/images/Default CS2 Weapons/weapon_ak47.png'">
                <div style="font-size:10px;color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(item.display_name || item.name)}</div>
                ${item.condition ? `<div style="margin-top:2px;">${condBadgeHtml(item.condition)}</div>` : ''}
                <div style="font-size:9px;color:#aaa;margin-top:2px;">$${item.price.toFixed(2)}</div>
                <div style="font-size:8px;color:#555;margin-top:2px;">🔍 inspect</div>
            </div>`;
    }

    function showBulkSummary() {
        const items = state.popupItems;
        const total = items.length;
        const totalValue = items.reduce((sum, item) => sum + item.price, 0);
        const golds = items.filter(item => item.rarity === 'Gold').length;
        document.getElementById('popupBody').innerHTML = `
            <h3 style="color:#ffd700;margin-bottom:15px;">📦 All Items Opened!</h3>
            <div style="margin:15px 0;">
                <div style="font-size:14px;color:#888;">Total Items: ${total}</div>
                <div style="font-size:14px;color:#888;">Total Value: $${totalValue.toFixed(2)}</div>
                <div style="font-size:14px;color:#ffd700;">⭐ Golds Found: ${golds}</div>
                <div style="font-size:14px;color:#4caf50;">🔥 Streak: ${state.streakData.current_streak || 0}</div>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:8px;max-height:320px;overflow-y:auto;margin:10px 0;padding:10px;background:rgba(0,0,0,0.2);border-radius:8px;">
                ${items.map(summaryItemCardHtml).join('')}
            </div>
            <div class="popup-buttons" style="margin-top:15px;">
                <button class="btn btn-success" onclick="handlePopupAction('keep_all')">💾 Keep All</button>
                <button class="btn btn-danger" onclick="handlePopupAction('sell_all')">💰 Sell All (70%)</button>
                <button class="btn btn-primary" onclick="handlePopupAction('done')">✅ Done</button>
            </div>
        `;
    }

    // Shown whenever a multi-item open finishes normally (clicked through each
    // item), not just via Skip All -- so users can see everything they pulled
    // without needing perfect memory, matching the Skip All summary UX.
    function showSessionSummary() {
        const items = state.popupItems;
        const total = items.length;
        const totalValue = items.reduce((sum, item) => sum + item.price, 0);
        const golds = items.filter(item => item.rarity === 'Gold').length;
        document.getElementById('popupBody').innerHTML = `
            <h3 style="color:#ffd700;margin-bottom:15px;">📦 Session Summary</h3>
            <div style="margin:15px 0;">
                <div style="font-size:14px;color:#888;">Total Items: ${total}</div>
                <div style="font-size:14px;color:#888;">Total Value: $${totalValue.toFixed(2)}</div>
                <div style="font-size:14px;color:#ffd700;">⭐ Golds Found: ${golds}</div>
                <div style="font-size:14px;color:#4caf50;">🔥 Streak: ${state.streakData.current_streak || 0}</div>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:8px;max-height:320px;overflow-y:auto;margin:10px 0;padding:10px;background:rgba(0,0,0,0.2);border-radius:8px;">
                ${items.map(summaryItemCardHtml).join('')}
            </div>
            <div class="popup-buttons" style="margin-top:15px;">
                <button class="btn btn-primary" onclick="finishSession()">✅ Done</button>
            </div>
        `;
    }

    // Keep/Sell choices are already finalized per item by this point, so this
    // just closes out and refreshes -- no Keep All/Sell All here to avoid
    // re-processing items the user already decided on individually.
    async function finishSession() {
        closePopup();
        await loadBalance();
        await loadStats();
        await loadInventory(state.currentPage);
    }
    function lockPopupButtons() {
        clearTimeout(_autoAdvanceTimer);
        document.querySelectorAll('.inspect-buttons button, .popup-buttons button').forEach(b => {
            b.disabled = true;
            b.style.opacity = '0.45';
            b.style.cursor = 'not-allowed';
            b.style.pointerEvents = 'none';
        });
    }

    function setAutoAdvance(enabled) {
        autoAdvanceEnabled = enabled;
        localStorage.setItem('autoAdvance', enabled ? '1' : '0');
        // Turning it off should immediately cancel any countdown already in flight,
        // not just stop future ones -- this is the "stuck with it on" case.
        if (!enabled) clearTimeout(_autoAdvanceTimer);
        updateAutoAdvanceButton();
        const popupCheckbox = document.querySelector('.inspect-screen input[type=checkbox]');
        if (popupCheckbox) popupCheckbox.checked = enabled;
    }

    function toggleAutoAdvanceMain() {
        setAutoAdvance(!autoAdvanceEnabled);
    }

    function toggleAutoAdvance(checkbox) {
        setAutoAdvance(checkbox.checked);
    }

    function updateAutoAdvanceButton() {
        const btn = document.getElementById('autoAdvanceBtn');
        if (!btn) return;
        btn.textContent = autoAdvanceEnabled ? '⏩ Auto: ON' : '⏩ Auto: OFF';
        btn.classList.toggle('btn-primary', autoAdvanceEnabled);
        btn.classList.toggle('btn-outline', !autoAdvanceEnabled);
    }

    // Items are already auto-saved to inventory (status='kept') the instant a case
    // is opened server-side -- Keep/Sell here just confirm/change that fate. So on
    // a bulk open we advance to the next item's reveal instead of closing the whole
    // popup, and only close+refresh once there's truly nothing left to review.
    async function _advanceOrFinish(index) {
        const nextIdx = index + 1;
        if (nextIdx < state.popupItems.length) {
            if (state.popupMode === 'capsule') {
                await showCapsuleCrackForItem(state.popupItems[nextIdx], nextIdx, state.popupItems.length);
            } else {
                showReelForItem(state.popupItems[nextIdx], nextIdx, state.popupItems.length);
            }
            return;
        }
        if (state.popupItems.length > 1) {
            showSessionSummary();
            return;
        }
        closePopup();
        await loadBalance();
        await loadStats();
        await loadInventory(state.currentPage);
    }

    function skipToEnd() {
        lockPopupButtons();
        for (let i = state.popupIndex; i < state.popupItems.length; i++) {
            const itemId = state.popupItems[i].id || i;
            apiCall('/api/keep-item', { method: 'POST', body: JSON.stringify({ item_id: itemId }) }).catch(() => {});
        }
        showBulkSummary();
    }

    async function handlePopupAction(action, index) {
        lockPopupButtons();
        const item = state.popupItems[index];
        if (action === 'keep') {
            try {
                const itemId = item.id || index;
                await apiCall('/api/keep-item', { method: 'POST', body: JSON.stringify({ item_id: itemId }) });
                showToast('💾 Item kept!');
            } catch (e) {
                showToast('❌ Error keeping item');
            }
            await _advanceOrFinish(index);
        } else if (action === 'sell') {
            try {
                const itemId = item.id || index;
                const data = await apiCall('/api/sell-item', { method: 'POST', body: JSON.stringify({ item_id: itemId }) });
                if (data.success) {
                    await loadBalance();
                    showToast(`💰 Sold for $${data.sell_price.toFixed(2)}`);
                }
            } catch (e) {
                showToast('❌ Error selling item');
            }
            await _advanceOrFinish(index);
        } else if (action === 'spin_again') {
            try {
                const itemId = item.id || index;
                await apiCall('/api/keep-item', { method: 'POST', body: JSON.stringify({ item_id: itemId }) });
            } catch (e) {}
            const caseId = state.popupCaseId;
            const wasCapsule = state.popupMode === 'capsule';
            if (!caseId) { showToast('❌ No case to spin again!'); return; }
            closePopup();
            await loadBalance();
            await loadStats();
            await loadInventory(state.currentPage);
            await loadTicketBalance();
            setTimeout(() => {
                if (wasCapsule) openStickerCapsule(caseId, 1);
                else if (caseId && caseId.startsWith('ticket_')) openPremiumPopup(caseId, 1);
                else openCasePopup(caseId, 1);
            }, 300);
        } else if (action === 'next') {
            await _advanceOrFinish(index);
        } else if (action === 'done') {
            if (state.popupItems.length > 1) {
                showSessionSummary();
            } else {
                closePopup();
                await loadBalance();
                await loadStats();
                await loadInventory(state.currentPage);
            }
        } else if (action === 'keep_all') {
            try {
                const allItems = state.popupItems;
                for (let i = 0; i < allItems.length; i++) {
                    const itemId = allItems[i].id || i;
                    await apiCall('/api/keep-item', { method: 'POST', body: JSON.stringify({ item_id: itemId }) });
                }
                closePopup();
                await loadBalance();
                await loadStats();
                await loadInventory(state.currentPage);
                showToast(`💾 Kept all ${allItems.length} items!`);
            } catch (e) {
                closePopup();
                showToast('❌ Error keeping all items');
            }
        } else if (action === 'sell_all') {
            try {
                const allItems = state.popupItems;
                const itemIds = allItems.map((item) => item.id || 0);
                const data = await apiCall('/api/sell-batch', { method: 'POST', body: JSON.stringify({ item_ids: itemIds }) });
                closePopup();
                if (data.success) {
                    await loadBalance();
                    await loadStats();
                    await loadInventory(state.currentPage);
                    showToast(`💰 Sold ${data.count} items for $${data.total_sell_price.toFixed(2)}`);
                }
            } catch (e) {
                closePopup();
                showToast('❌ Error selling all items');
            }
        }
    }

    function showBigWinOverlay(value, multiplier) {
        const overlay = document.getElementById('bigWinOverlay');
        const labelEl = document.getElementById('bigWinLabel');
        const numberEl = document.getElementById('bigWinNumber');
        const coinContainer = document.getElementById('bigWinCoinContainer');

        let labelText, labelClass;
        if (multiplier >= 50) {
            labelText = '💰 JACKPOT!';
            labelClass = 'jackpot';
        } else if (multiplier >= 20) {
            labelText = '🔥 MEGA WIN!';
            labelClass = 'mega';
        } else {
            labelText = '⭐ BIG WIN!';
            labelClass = 'big';
        }

        labelEl.textContent = labelText;
        labelEl.className = 'big-win-label ' + labelClass;
        numberEl.textContent = '$0.00';

        overlay.style.display = '';
        overlay.classList.remove('fade-out');
        overlay.classList.add('active');

        // Camera shake on big wins
        const shakeClass = multiplier >= 50 ? 'win-shake-heavy' : 'win-shake';
        document.body.classList.add(shakeClass);
        setTimeout(() => document.body.classList.remove(shakeClass), 600);

        // Burst coins from center
        const coins = ['🪙', '💰', '💎', '⭐', '🎉', '👑'];
        const centerX = window.innerWidth / 2;
        const centerY = window.innerHeight / 2 - 80;
        for (let i = 0; i < 80; i++) {
            const coin = document.createElement('div');
            coin.className = 'big-win-coin';
            coin.textContent = coins[Math.floor(Math.random() * coins.length)];
            const angle = Math.random() * Math.PI * 2;
            const dist = 200 + Math.random() * 500;
            coin.style.left = centerX + 'px';
            coin.style.top = centerY + 'px';
            coin.style.setProperty('--bx', Math.cos(angle) * dist + 'px');
            coin.style.setProperty('--by', Math.sin(angle) * dist + 'px');
            coin.style.setProperty('--br', (Math.random() * 720 - 360) + 'deg');
            coin.style.fontSize = (16 + Math.random() * 24) + 'px';
            coin.style.animationDuration = (1.5 + Math.random() * 1.5) + 's';
            coin.style.animationDelay = (Math.random() * 0.8) + 's';
            coinContainer.appendChild(coin);
        }

        // Animate number counting up
        const duration = Math.min(2000, 600 + value * 0.3);
        const startTime = performance.now();
        const startVal = 0;

        function countUp(now) {
            const elapsed = now - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const eased = 1 - Math.pow(1 - progress, 3);
            const current = startVal + (value - startVal) * eased;
            numberEl.textContent = '$' + current.toFixed(2);
            if (progress < 1) {
                requestAnimationFrame(countUp);
            } else {
                numberEl.textContent = '$' + value.toFixed(2);
            }
        }
        requestAnimationFrame(countUp);

        // Auto fade out after 3.5s
        setTimeout(() => {
            overlay.classList.remove('active');
            overlay.classList.add('fade-out');
            setTimeout(() => {
                overlay.classList.remove('fade-out');
                overlay.style.display = 'none';
                coinContainer.innerHTML = '';
            }, 1500);
        }, 3500);
    }

    function isBigWinMultiplier(winner) {
        const casePrice = state.popupCasePrice;
        if (casePrice <= 0) return 0;
        return winner.price / casePrice;
    }

    function closePopup() {
        clearTimeout(_autoAdvanceTimer);
        document.getElementById('popupOverlay').classList.remove('show');
        document.getElementById('popupContent').classList.remove('terminal-theme');
        document.getElementById('popupBody').innerHTML = '';
        const bigOverlay = document.getElementById('bigWinOverlay');
        bigOverlay.classList.remove('active', 'fade-out');
        bigOverlay.style.display = 'none';
        document.getElementById('bigWinCoinContainer').innerHTML = '';
        state.popupItems = [];
        state.popupIndex = 0;
        state.popupMode = null;
        state.popupCaseId = null;
        state.popupCasePrice = 0;
        state.currentMinesGame = null;
        state.currentTerminalSession = null;
    }

    // ============================================
    // SECTION 8: ACHIEVEMENTS (unchanged)
    // ============================================
    let _prevUnlocked = new Set();

    function showAchievementToast(name, description, icon, xp) {
        const existing = document.querySelector('.achievement-toast');
        if (existing) existing.remove();
        const el = document.createElement('div');
        el.className = 'achievement-toast';
        el.setAttribute('role', 'alert');
        el.innerHTML = `
            <div class="icon">${icon || '🏆'}</div>
            <div class="info">
                <div class="title">Achievement Unlocked!</div>
                <div class="desc">${esc(name)} — ${esc(description)}</div>
                ${xp ? `<div class="xp">+${xp} XP</div>` : ''}
            </div>
            <button class="close-btn" aria-label="Dismiss" onclick="this.parentElement.remove()">✕</button>
        `;
        document.body.appendChild(el);
        setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .5s'; setTimeout(() => el.remove(), 500); }, 5000);
    }

    async function loadAchievements() {
        try {
            const data = await apiCall('/api/user/achievements');
            const grid = document.getElementById('achievementsGrid');

            // Check for newly unlocked achievements and show toast
            const nowUnlocked = new Set(data.achievements.filter(a => a.unlocked).map(a => a.id));
            for (const a of data.achievements) {
                if (a.unlocked && !_prevUnlocked.has(a.id)) {
                    showAchievementToast(a.name, a.description, a.icon, a.xp);
                }
            }
            _prevUnlocked = nowUnlocked;

            grid.innerHTML = data.achievements.map(a => `
                <div class="premium-card ${a.unlocked ? '' : 'locked'}" style="text-align:center;padding:15px;">
                    ${!a.unlocked ? `<div class="lock-overlay"><div class="lock-icon">🔒</div><div class="lock-text">Locked</div></div>` : ''}
                    <div style="font-size:36px;margin-bottom:8px;">${a.icon || '🏆'}</div>
                    <div style="font-weight:bold;font-size:14px;color:${a.unlocked ? '#ffd700' : '#888'};">${a.name}</div>
                    <div style="font-size:10px;color:#666;margin-top:4px;">${a.description}</div>
                    ${a.unlocked ? '<div style="color:#4caf50;font-size:10px;margin-top:4px;">✅ Unlocked!</div>' : ''}
                </div>
            `).join('');
        } catch (e) { console.error('Load achievements error:', e); }
    }

    // ============================================
    // SECTION 9: STICKER CAPSULES — CINEMATIC REVEAL
    // ============================================

    // ── Capsule-specific sounds ──────────────────
    function playCapsuleRattle() {
        [0, 80, 160, 220, 280].forEach((t, i) => {
            setTimeout(() => playTone(120 + Math.random() * 60, 0.06, 'sawtooth', 0.08 + i * 0.01), t);
        });
    }
    function playCapsuleCrack() {
        playTone(80, 0.12, 'square', 0.25);
        setTimeout(() => playTone(160, 0.08, 'square', 0.18), 40);
        setTimeout(() => playTone(60,  0.20, 'sawtooth', 0.15), 60);
        setTimeout(() => playTone(40,  0.30, 'square', 0.10), 100);
    }
    function playCapsuleFlash() {
        playTone(1800, 0.05, 'sine', 0.20);
        setTimeout(() => playTone(2400, 0.04, 'sine', 0.15), 30);
        setTimeout(() => playTone(3000, 0.03, 'sine', 0.10), 60);
    }
    function playStickerEject() {
        [0, 30, 60, 90, 120].forEach((t, i) => {
            setTimeout(() => playTone(300 + i * 120, 0.07, 'sine', 0.12 - i * 0.01), t);
        });
        setTimeout(() => playTone(800, 0.15, 'sine', 0.18), 180);
    }
    function playStickerRevealRarity(rarity) {
        if (rarity.includes('Legendary')) {
            playRevealGold();
            setTimeout(() => playTone(2000, 0.4, 'sine', 0.12), 600);
        } else if (rarity.includes('Epic')) {
            playRevealRed();
            setTimeout(() => playTone(1600, 0.3, 'sine', 0.10), 400);
        } else if (rarity.includes('Rare') || rarity === '🔥') {
            playRevealPink();
        } else if (rarity === '💫') {
            playRevealPurple();
        } else {
            playRevealBlue();
        }
    }

    // ── CSS injected once ────────────────────────
    (function injectCapsuleStyles() {
        if (document.getElementById('capsule-styles')) return;
        const s = document.createElement('style');
        s.id = 'capsule-styles';
        s.textContent = `
            @keyframes capsuleSpin3D {
                0%   { transform: perspective(400px) rotateY(0deg) rotateX(0deg) scale(1); }
                20%  { transform: perspective(400px) rotateY(72deg) rotateX(15deg) scale(1.05); }
                40%  { transform: perspective(400px) rotateY(144deg) rotateX(-10deg) scale(1.08); }
                60%  { transform: perspective(400px) rotateY(216deg) rotateX(20deg) scale(1.05); }
                80%  { transform: perspective(400px) rotateY(288deg) rotateX(-5deg) scale(1.02); }
                100% { transform: perspective(400px) rotateY(360deg) rotateX(0deg) scale(1); }
            }
            @keyframes capsuleShake {
                0%,100% { transform: perspective(400px) rotateY(0deg) translateX(0); }
                15%  { transform: perspective(400px) rotateY(20deg) translateX(-6px); }
                30%  { transform: perspective(400px) rotateY(-25deg) translateX(8px); }
                45%  { transform: perspective(400px) rotateY(15deg) translateX(-10px); }
                60%  { transform: perspective(400px) rotateY(-30deg) translateX(7px); }
                75%  { transform: perspective(400px) rotateY(10deg) translateX(-4px); }
            }
            @keyframes capsuleCrack {
                0%   { transform: perspective(400px) rotateY(0deg) scale(1); filter: brightness(1); }
                20%  { transform: perspective(400px) rotateY(-15deg) scale(1.1); filter: brightness(1.5); }
                40%  { transform: perspective(400px) rotateY(20deg) scale(0.95); filter: brightness(2) saturate(2); }
                60%  { transform: perspective(400px) rotateY(-10deg) scale(1.08); filter: brightness(3) saturate(3); }
                80%  { transform: perspective(400px) rotateY(5deg) scale(1.12); filter: brightness(4); }
                100% { transform: perspective(400px) rotateY(0deg) scale(0); filter: brightness(10); opacity: 0; }
            }
            @keyframes crackLineGrow {
                0%   { opacity: 0; transform: scaleX(0) scaleY(0); }
                60%  { opacity: 1; transform: scaleX(1) scaleY(1); }
                100% { opacity: 0; transform: scaleX(1.2) scaleY(1.2); }
            }
            @keyframes flashWhite { 0%,100% { opacity: 0; } 50% { opacity: 1; } }
            .capsule-3d-wrap { width:160px; height:160px; margin:0 auto; position:relative; display:flex; align-items:center; justify-content:center; }
            .capsule-img-el { width:140px; height:140px; object-fit:contain; filter:drop-shadow(0 0 20px rgba(255,200,50,0.5)); will-change:transform; }
            .capsule-crack-overlay { position:absolute; inset:0; pointer-events:none; display:flex; align-items:center; justify-content:center; }
            .crack-line { position:absolute; background:white; border-radius:2px; transform-origin:center; animation:crackLineGrow 0.35s ease forwards; }
            .flash-overlay { position:fixed; inset:0; background:white; pointer-events:none; z-index:9999; opacity:0; }
            .capsule-opening-stage { text-align:center; padding:10px 0 0; min-height:340px; position:relative; overflow:visible; }
        `;
        document.head.appendChild(s);
    })();

    let _capsuleCategory = 'all';
    let _capsuleSearchTerm = '';
    const CAPSULE_CATEGORY_LABELS = {
        all: 'All', retail_community: 'Retail & Community',
        team_autograph: 'Team Autographs', major_autograph: 'Major Tournament Autographs',
    };

    async function loadCapsules() {
        try {
            const live = await apiCall('/api/capsules');
            if (live && Array.isArray(live.capsules) && live.capsules.length) {
                STICKER_CAPSULES.length = 0;
                STICKER_CAPSULES.push(...live.capsules);
            }
        } catch (e) { console.warn('Live capsule prices unavailable, using cached list:', e); }

        Object.keys(CAPSULE_CATEGORY_LABELS).forEach(cat => {
            const el = document.getElementById('capcat-count-' + cat);
            if (!el) return;
            const n = cat === 'all' ? STICKER_CAPSULES.length : STICKER_CAPSULES.filter(c => (c.category || 'retail_community') === cat).length;
            el.textContent = `(${n})`;
        });

        renderCapsuleGrid();
    }

    function setCapsuleCategory(cat) {
        _capsuleCategory = cat;
        document.querySelectorAll('#capsuleCategoryTabs .btn').forEach(b => b.classList.remove('btn-primary', 'active'));
        const btn = document.getElementById('capcat-' + cat);
        if (btn) { btn.classList.add('btn-primary', 'active'); btn.classList.remove('btn-outline'); }
        document.querySelectorAll('#capsuleCategoryTabs .btn').forEach(b => { if (b !== btn) b.classList.add('btn-outline'); });
        renderCapsuleGrid();
    }

    function filterCapsules() {
        _capsuleSearchTerm = (document.getElementById('capsuleSearch').value || '').trim().toLowerCase();
        renderCapsuleGrid();
    }

    function renderCapsuleGrid() {
        const grid = document.getElementById('capsuleGrid');
        const emptyEl = document.getElementById('capsuleEmpty');
        const filtered = STICKER_CAPSULES.filter(c => {
            const matchesCategory = _capsuleCategory === 'all' || (c.category || 'retail_community') === _capsuleCategory;
            const matchesSearch = !_capsuleSearchTerm || c.name.toLowerCase().includes(_capsuleSearchTerm);
            return matchesCategory && matchesSearch;
        });

        if (!filtered.length) {
            grid.innerHTML = '';
            emptyEl.style.display = 'block';
            return;
        }
        emptyEl.style.display = 'none';

        grid.innerHTML = filtered.map(c => {
            const imgSrc = c.image ? `/static/images/containers/${c.image.replace('assets/containers/','')}` : null;
            const imgHtml = imgSrc
                ? `<img src="${imgSrc}" alt="${esc(c.name)}" style="width:100px;height:80px;object-fit:contain;margin:8px 0;filter:drop-shadow(0 0 10px rgba(255,200,50,0.3));transition:filter 0.3s;">`
                : `<div style="font-size:40px;margin:10px 0;">${c.emoji}</div>`;
            return `
                <div class="capsule-card" data-capsule-id="${c.id}" onclick="openStickerCapsule('${c.id}', state.capsuleBulkQuantity)"
                     style="cursor:pointer;border:1px solid rgba(255,215,0,0.25);border-radius:10px;padding:14px;text-align:center;
                            background:linear-gradient(135deg,#1a1a2e,#0f0f1a);transition:all 0.25s;position:relative;overflow:hidden;"
                     onmouseenter="this.style.borderColor='rgba(255,215,0,0.6)';this.style.transform='translateY(-3px)';this.style.boxShadow='0 8px 30px rgba(255,215,0,0.15)'"
                     onmouseleave="this.style.borderColor='rgba(255,215,0,0.25)';this.style.transform='';this.style.boxShadow=''">
                    ${imgHtml}
                    <div style="font-weight:bold;color:#fff;font-size:12px;margin-top:4px;">${esc(c.name)}</div>
                    <div style="color:#ffd700;font-size:13px;font-weight:700;margin-top:3px;">$${c.price.toFixed(2)}</div>
                    <button class="btn btn-primary btn-sm" style="margin-top:8px;font-size:9px;padding:4px 10px;">Open${state.capsuleBulkQuantity > 1 ? ' ' + state.capsuleBulkQuantity : ''}</button>
                </div>
            `;
        }).join('');
    }

    function setCapsuleBulkQuantity(qty) {
        state.capsuleBulkQuantity = qty;
        document.querySelectorAll('.capsule-card').forEach(card => {
            const capsuleId = card.dataset.capsuleId;
            if (capsuleId) card.onclick = function() { openStickerCapsule(capsuleId, qty); };
            const openBtn = card.querySelector('.btn-sm');
            if (openBtn) openBtn.textContent = qty > 1 ? 'Open ' + qty : 'Open';
        });
        const discount = {1:0,5:5,10:10,15:15,20:20,25:25}[qty] || 0;
        const discountEl = document.getElementById('capsuleBulkDiscount');
        if (discountEl) discountEl.textContent = discount > 0 ? `(${discount}% discount)` : '';
        document.querySelectorAll('#tab-capsules .btn-sm').forEach(b => {
            if (b.id && b.id.indexOf('cqty') === 0) b.classList.remove('btn-primary', 'active');
        });
        const qtyBtn = document.getElementById('cqty' + qty);
        if (qtyBtn) qtyBtn.classList.add('btn-primary', 'active');
    }

    async function openStickerCapsule(capsuleId, quantity = 1) {
        if (isOpening) return;
        isOpening = true;
        lastCapsuleId = capsuleId;
        state.popupMode = 'capsule';
        state.popupCaseId = capsuleId;
        state.popupCasePrice = (STICKER_CAPSULES.find(c => c.id === capsuleId) || {}).price || 0;
        state.popupIndex = 0;
        state.popupItems = [];
        const overlay = document.getElementById('popupOverlay');
        overlay.classList.add('show');
        document.getElementById('popupBody').innerHTML = `<div class="loading" style="font-size:18px;padding:40px;">Opening capsule${quantity > 1 ? 's' : ''}...</div>`;
        await openCapsuleBatch(capsuleId, quantity).finally(() => { isOpening = false; });
    }

    async function openCapsuleBatch(capsuleId, quantity) {
        try {
            const data = await apiCall('/api/sticker', { method: 'POST', body: JSON.stringify({ capsule: capsuleId, quantity }) });
            if (data.success) {
                state.popupItems = data.items;
                await loadBalance();
                await loadStats();
                await loadInventory(state.currentPage);
                await showCapsuleCrackForItem(data.items[0], 0, data.items.length);
            } else {
                document.getElementById('popupBody').innerHTML = `<div class="error" style="font-size:18px;padding:20px;">❌ ${data.error || 'Failed to open capsule'}<br><br><button class="btn btn-primary" onclick="closePopup()">Close</button></div>`;
            }
        } catch (e) {
            document.getElementById('popupBody').innerHTML = `<div class="error" style="font-size:18px;padding:20px;">❌ Error opening capsule: ${e.message || 'Unknown error'}<br><br><button class="btn btn-primary" onclick="closePopup()">Close</button></div>`;
        }
    }

    // Plays the capsule crack cinematic (spin/shake/crack/flash), then hands off
    // to the same full-card inspect screen weapons use -- kept as its own
    // per-item step (mirroring showReelForItem) so bulk capsule opens get the
    // same Next/Skip All/Auto-advance/Session Summary flow as bulk case opens.
    async function showCapsuleCrackForItem(item, index, total) {
        state.popupIndex = index;
        const isSingle = total === 1;
        const capsuleInfo = STICKER_CAPSULES.find(c => c.id === state.popupCaseId);
        const capsuleImgSrc = capsuleInfo && capsuleInfo.image
            ? `/static/images/containers/${capsuleInfo.image.replace('assets/containers/', '')}`
            : null;

        document.getElementById('popupBody').innerHTML = `
            <div class="capsule-opening-stage" id="capsuleStage">
                <div style="margin-bottom:6px;color:#888;font-size:12px;">${!isSingle ? `Opening ${index + 1} of ${total}` : ''}</div>
                <div style="color:#aaa;font-size:13px;margin-bottom:12px;letter-spacing:2px;text-transform:uppercase;">Opening capsule...</div>
                <div class="capsule-3d-wrap" id="capsuleWrap">
                    ${capsuleImgSrc
                        ? `<img class="capsule-img-el" id="capsuleImg" src="${capsuleImgSrc}" alt="capsule">`
                        : `<div style="font-size:80px;line-height:1;" id="capsuleImg">${capsuleInfo ? capsuleInfo.emoji : '📦'}</div>`}
                    <div class="capsule-crack-overlay" id="capsuleCracks"></div>
                </div>
                <div style="margin-top:14px;color:#ffd700;font-size:13px;font-weight:700;letter-spacing:1px;" id="capsuleStatus">Spinning...</div>
            </div>
            <div id="reelStatus" style="margin:10px 0;font-size:14px;color:#888;min-height:30px;"></div>
            <div id="reelButtons" style="display:none;margin-top:6px;"></div>
        `;

        const capsuleImg = document.getElementById('capsuleImg');
        capsuleImg.style.animation = 'capsuleSpin3D 0.7s ease-in-out infinite';
        playCapsuleRattle();

        const speedKey = state.userSettings.spin_speed || 'normal';
        const spinMs = { chill: 2200, normal: 1500, fast: 800 }[speedKey] || 1500;
        await new Promise(r => setTimeout(r, spinMs));

        // Phase 2: shake
        const statusEl = document.getElementById('capsuleStatus');
        if (statusEl) statusEl.textContent = '🔥 Breaking...';
        capsuleImg.style.animation = 'capsuleShake 0.5s ease-in-out';
        playCapsuleRattle();
        await new Promise(r => setTimeout(r, 500));

        // Phase 3: cracks
        const cracksEl = document.getElementById('capsuleCracks');
        if (cracksEl) cracksEl.innerHTML = `
            <div class="crack-line" style="width:3px;height:70px;top:20px;left:50%;transform-origin:bottom center;transform:translateX(-50%) rotate(-30deg);"></div>
            <div class="crack-line" style="width:2px;height:50px;top:35px;left:45%;transform-origin:bottom center;transform:translateX(-50%) rotate(15deg);animation-delay:0.05s;"></div>
            <div class="crack-line" style="width:2px;height:40px;top:45px;left:57%;transform-origin:bottom center;transform:translateX(-50%) rotate(-10deg);animation-delay:0.1s;"></div>
        `;
        playCapsuleCrack();
        await new Promise(r => setTimeout(r, 350));

        // Phase 4: flash + explode
        const flashDiv = document.createElement('div');
        flashDiv.className = 'flash-overlay';
        document.body.appendChild(flashDiv);
        flashDiv.style.animation = 'flashWhite 0.25s ease forwards';
        playCapsuleFlash();
        capsuleImg.style.animation = 'capsuleCrack 0.4s ease-in forwards';
        await new Promise(r => setTimeout(r, 250));
        flashDiv.remove();
        await new Promise(r => setTimeout(r, 200));

        // Phase 5: reveal -- full inspect card, same treatment as weapons
        playStickerEject();
        playStickerRevealRarity(item.rarity);
        const isHolo = item.rarity.includes('Legendary') || item.rarity.includes('Epic') || item.rarity === '🔥';
        const isRare = item.rarity.includes('Rare') || item.rarity === '💫';
        if (isHolo) { spawnConfettiExplosion(); spawnParticles('Gold', 50); }
        else if (isRare) { spawnConfetti(item.rarity, 60); spawnParticles(item.rarity, 20); }
        else { spawnConfetti(item.rarity, 30); }

        showInspectScreen(item, index, total);
    }

// Make it globally accessible for inline onclick handlers
window.openStickerCapsule = openStickerCapsule;
window.setCapsuleBulkQuantity = setCapsuleBulkQuantity;
window.setInvTypeFilter = setInvTypeFilter;


    
	// ============================================
    // SECTION 10: INVENTORY — Card Grid
    // ============================================

    let _invTypeFilter = 'all';

    function setInvTypeFilter(type, el) {
        _invTypeFilter = type;
        document.querySelectorAll('.inv-type-tab').forEach(t => t.classList.remove('active'));
        if (el) el.classList.add('active');
        renderInventory();
    }

    // ── Batch sell mode ───────────────────────────
    let sellModeActive = false;
    let selectedSellIds = new Set();

    function toggleSellMode() {
        sellModeActive = !sellModeActive;
        selectedSellIds.clear();
        const btn = document.getElementById('sellModeBtn');
        const bar = document.getElementById('sellModeBar');
        if (btn) {
            btn.textContent = sellModeActive ? '✖ Cancel Sell Mode' : '💰 Sell Mode';
            btn.classList.toggle('btn-danger', sellModeActive);
        }
        if (bar) bar.style.display = sellModeActive ? 'flex' : 'none';
        updateSellModeBar();
        renderInventory();
    }

    function toggleSellSelect(itemId) {
        const key = String(itemId);
        if (selectedSellIds.has(key)) selectedSellIds.delete(key);
        else selectedSellIds.add(key);
        updateSellModeBar();
        renderInventory();
    }

    function clearSellSelection() {
        selectedSellIds.clear();
        updateSellModeBar();
        renderInventory();
    }

    function updateSellModeBar() {
        const countEl = document.getElementById('sellModeCount');
        const confirmBtn = document.getElementById('confirmSellBtn');
        if (!countEl) return;
        const items = (state.inventory || []).filter(i => selectedSellIds.has(String(i.id)));
        const total = items.reduce((sum, i) => sum + (parseFloat(i.price) || 0), 0) * 0.70;
        countEl.textContent = `Selected: ${items.length} item${items.length === 1 ? '' : 's'} ($${total.toFixed(2)})`;
        if (confirmBtn) confirmBtn.disabled = items.length === 0;
    }

    async function confirmBatchSell() {
        const ids = Array.from(selectedSellIds).map(id => parseInt(id)).filter(id => !isNaN(id));
        if (ids.length === 0) return;
        if (!confirm(`Sell ${ids.length} item(s) for 70% of their value?`)) return;
        try {
            const data = await apiCall('/api/sell-items', {
                method: 'POST',
                body: JSON.stringify({ item_ids: ids })
            });
            if (data && data.success) {
                showToast(`✅ Sold ${data.sold_count} item(s) for $${Number(data.total_sell_price).toFixed(2)}`);
                selectedSellIds.clear();
                await loadBalance();
                await loadInventory(state.currentPage);
                updateSellModeBar();
            }
        } catch (e) {
            showToast('❌ Error selling items', 'error');
        }
    }

    async function loadInventory(page = 0) {
        state.currentPage = page;
        try {
            const data = await apiCall(`/api/user/me/inventory?limit=${state.PAGE_SIZE}&offset=${page * state.PAGE_SIZE}`);
            state.inventory = data.items;
            state.totalPages = Math.ceil(data.count / state.PAGE_SIZE);
            renderInventory();
            renderPagination();
        } catch (e) { console.error('Load inventory error:', e); }
    }

    // ── Item classification helpers ───────────────
    function classifyItem(item) {
        const type  = (item.item_type || '').toLowerCase();
        const name  = (item.item_name || '').trim();
        const rar   = (item.rarity || '').trim();

        // Sticker / capsule
        if (type === 'sticker') return 'sticker';

        // Gold tier items (knives / gloves / special)
        if (rar === 'Gold' || type === 'gold') return 'gold';

        // Items whose name starts with a known weapon-category prefix that's wrong
        // e.g. "RIFLE | Big Iron" → should show as weapon with cleaned name
        return 'weapon';
    }

    // Strip leading rarity emoji or generic weapon-category prefix from stored names.
    // e.g. "🟪 SHOTGUN | Red Quartz"  → "SHOTGUN | Red Quartz"  (category still shows)
    // We map generic weapon categories to nicer display labels if the real name isn't known.
    const GENERIC_WEAPON_CATEGORY_MAP = {
        'RIFLE':   'Rifle',
        'PISTOL':  'Pistol',
        'SMG':     'SMG',
        'SHOTGUN': 'Shotgun',
        'SNIPER':  'Sniper Rifle',
        'MACHINE GUN': 'Machine Gun',
        'KNIFE':   'Knife',
    };
    function cleanItemName(raw) {
        // Strip leading emoji chars (covers unicode emoji + text emoji sequences)
        let name = (raw || '').replace(/^[\u{1F300}-\u{1FFFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{FE00}-\u{FEFF}🟦🟪🟥🟨🟩⬛⬜🟫🔥⭐💫👑✨\s]+/gu, '').trim();
        // Also strip leading "Mystery " prefix
        name = name.replace(/^Mystery\s+/i, '').trim();
        // Strip a leading "StatTrak™ " marker too -- every caller that cares
        // about StatTrak status renders its own separate badge/prefix (stPrefix),
        // so leaving it in here produced a visible "StatTrak™ StatTrak™ ..."
        // duplicate on gold knives/gloves and StatTrak weapon skins alike.
        name = name.replace(/^StatTrak™\s*/i, '').trim();
        return name || raw;
    }

    const STICKER_RARITY_LABELS = {
        '⭐': { label: 'Common',    color: '#4488ff' },
        '✨': { label: 'Uncommon',  color: '#aa00ff' },
        '💫': { label: 'Rare',      color: '#ff69b4' },
        '🔥': { label: 'Foil',      color: '#ff4444' },
        '👑 Common':    { label: 'Holo Common',   color: '#4caf50' },
        '👑 Rare':      { label: 'Holo Rare',     color: '#00aaff' },
        '👑 Epic':      { label: 'Holo Epic',     color: '#aa00ff' },
        '👑 Legendary': { label: 'Holo Legendary',color: '#ffd700' },
    };

    const SKIN_RARITY_COLORS = {
        Gold: '#ffd700', Red: '#ff4444', Pink: '#ff69b4',
        Purple: '#aa00ff', Blue: '#4488ff'
    };

    const CONDITION_SHORT = {
        'Factory New': 'FN', 'Minimal Wear': 'MW', 'Field-Tested': 'FT',
        'Well-Worn': 'WW', 'Battle-Scarred': 'BS'
    };

    // Colored condition pill + float line — same visual language as market.html
    function condBadgeHtml(condition) {
        const short = CONDITION_SHORT[condition];
        if (!short) return '';
        return `<span class="cond-badge cond-${short.toLowerCase()}">${short}</span>`;
    }
    function floatLineHtml(floatValue) {
        if (floatValue === null || floatValue === undefined || floatValue === '') return '';
        const n = Number(floatValue);
        if (Number.isNaN(n)) return '';
        return `<div class="inv-float">Float: ${n.toFixed(4)}</div>`;
    }

    // Gold / knife emoji lookup
    function goldKnifeEmoji(name) {
        const n = (name || '').toLowerCase();
        if (n.includes('bayonet'))      return '🗡️';
        if (n.includes('karambit'))     return '🌀';
        if (n.includes('butterfly'))    return '🦋';
        if (n.includes('m9'))           return '🔪';
        if (n.includes('flip'))         return '🔄';
        if (n.includes('gut'))          return '🍖';
        if (n.includes('falchion'))     return '⚔️';
        if (n.includes('shadow'))       return '👥';
        if (n.includes('bowie'))        return '🏕️';
        if (n.includes('huntsman'))     return '🏹';
        if (n.includes('glove') || n.includes('wraps') || n.includes('hand')) return '🧤';
        if (n.includes('navaja'))       return '🔱';
        if (n.includes('stiletto'))     return '💎';
        if (n.includes('talon'))        return '🦅';
        if (n.includes('ursus'))        return '🐻';
        if (n.includes('skeleton'))     return '💀';
        if (n.includes('nomad'))        return '🌍';
        if (n.includes('survival'))     return '🏕️';
        if (n.includes('paracord'))     return '🪢';
        if (n.includes('classic'))      return '🗡️';
        return '⭐';
    }

    function renderInventory() {
        const invDiv = document.getElementById('inventory');
        if (!invDiv) return;
        const search = (document.getElementById('searchInput')?.value || '').toLowerCase();
        const rarF   = document.getElementById('rarityFilter')?.value || 'all';

        let filtered = state.inventory || [];
        if (search) filtered = filtered.filter(i => (i.item_name || '').toLowerCase().includes(search));
        if (rarF !== 'all') filtered = filtered.filter(i => i.rarity === rarF);

        // Type filter
        if (_invTypeFilter !== 'all') {
            filtered = filtered.filter(i => {
                const cat = classifyItem(i);
                if (_invTypeFilter === 'sticker') return cat === 'sticker';
                if (_invTypeFilter === 'gold')    return cat === 'gold';
                if (_invTypeFilter === 'weapon')  return cat === 'weapon';
                return true;
            });
        }

        if (filtered.length === 0) {
            invDiv.innerHTML = '<div class="inv-empty">No items found.<br><span style="font-size:11px;">Open some cases to fill your inventory!</span></div>';
            return;
        }

        invDiv.innerHTML = filtered.map(item => buildInvCard(item)).join('');
    }

    function buildInvCard(item) {
        const cat     = classifyItem(item);
        const rawName = (item.item_name || '').trim();
        // Always show a clean name (no leading emoji, no "Mystery" prefix)
        const cleanName = cleanItemName(rawName);
        const rar     = (item.rarity || 'Blue').trim();
        const price   = typeof item.price === 'number' ? item.price : parseFloat(item.price || 0);
        const isST    = item.is_stattrak;
        const cond    = item.condition || '';
        const itemId  = item.id || item.item_id || null;

        // ── Rarity stripe color ──
        const rarColor = SKIN_RARITY_COLORS[rar] || '#4488ff';

        // ── Per-type display logic ──
        let displayName, subLine, imgHtml, typeBadge, rarLabel, rarLabelColor;
        const canUpgrade = (rar !== 'Gold') && (cat !== 'sticker');

        if (cat === 'sticker') {
            // Sticker: name is already clean (e.g. "CS20 Classic (Holo)")
            displayName = cleanName;
            // Parse rarity — could be emoji-based
            const stickerRar = STICKER_RARITY_LABELS[rar];
            rarLabel      = stickerRar ? stickerRar.label : rar;
            rarLabelColor = stickerRar ? stickerRar.color : '#4488ff';
            // Use image_url from server (persisted on insert); fallback to emoji
            const stickerImgUrl = item.image_url || null;
            if (stickerImgUrl) {
                imgHtml = `<img src="${stickerImgUrl}" alt="${displayName}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
                           <div class="inv-img-emoji" style="display:none;">🏷️</div>`;
            } else {
                imgHtml = `<div class="inv-img-emoji">🏷️</div>`;
            }
            typeBadge = `<div class="inv-type-badge">Sticker</div>`;
            subLine   = `<span style="color:${rarLabelColor};">${rar} ${rarLabel}</span>`;

        } else if (cat === 'gold') {
            // Gold / knife: strip emoji and "★ " prefix for display
            displayName = cleanName.replace(/^★\s*/, '').trim();
            rarLabel      = 'Gold';
            rarLabelColor = '#ffd700';
            const emoji   = goldKnifeEmoji(rawName);
            // Use image_url from server when available, fall back to /api/skin-image with clean name
            const goldImgUrl = item.image_url || `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;
            imgHtml = `<img src="${goldImgUrl}" alt="${displayName}"
                           onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
                       <div class="inv-img-emoji" style="display:none;">${emoji}</div>
                       ${window.renderStickerOverlays ? renderStickerOverlays(item.applied_stickers) : ''}`;
            typeBadge = `<div class="inv-type-badge" style="background:rgba(255,215,0,0.25);color:#ffd700;">⭐ Gold</div>`;
            subLine   = `<span style="color:#ffd700;">Gold</span>${condBadgeHtml(cond)}`;

        } else {
            // Weapon skin — use cleanName for display and image lookup (strips leading emoji/category prefix)
            displayName = cleanName;
            rarLabel      = rar;
            rarLabelColor = rarColor;
            const skinImgUrl = item.image_url || `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;
            imgHtml = `<img src="${skinImgUrl}" alt="${displayName}"
                           onerror="this.src='/static/images/Default CS2 Weapons/weapon_ak47.png'">
                       ${window.renderStickerOverlays ? renderStickerOverlays(item.applied_stickers) : ''}`;
            typeBadge = `<div class="inv-type-badge">Skin</div>`;
            subLine   = `<span style="color:${rarColor};">${rar}</span>${condBadgeHtml(cond)}`;
        }

        const stBadge   = isST ? `<div class="inv-st-badge">ST™</div>` : '';
        const stPrefix  = isST ? `<span style="color:#ff6b00;font-size:9px;">🔥 StatTrak™ </span><br>` : '';
        const upgradeBtn = (canUpgrade && !item.protected)
            ? `<button class="inv-btn inv-btn-upgrade" onclick="upgradeItem('${itemId}')">⬆ UP</button>`
            : '';

        // Sticker slots (weapons and gold only)
        const appliedStickers = Array.isArray(item.applied_stickers) ? item.applied_stickers : [];
        const inLoadout = !!item.in_loadout;
        const isProtected = !!item.protected;
        const lockBadge = isProtected ? `<div class="inv-lock-badge" title="Protected — won't be sold, traded up, or staked">🔒</div>` : '';
        let stickerHtml = '';
        let stickerBtn = '';
        let loadoutBtn = '';
        let protectBtn = '';
        if (cat !== 'sticker' && itemId) {
            // Show 4 sticker slots
            const slots = [0,1,2,3].map(slot => {
                const s = appliedStickers.find(x => x.slot === slot);
                if (s && s.sticker_image) {
                    return `<div class="inv-sticker-dot" title="${esc(s.sticker_name)}" onclick="removeStickerSlot('${itemId}',${slot})">
                                <img src="${esc(s.sticker_image)}" onerror="this.parentElement.innerHTML='🏷️'">
                            </div>`;
                }
                return `<div class="inv-sticker-dot empty" title="Slot ${slot+1} — click to apply" onclick="openStickerPicker('${itemId}',${slot})">+</div>`;
            }).join('');
            stickerHtml = `<div class="inv-stickers">${slots}</div>`;
            stickerBtn  = appliedStickers.length < 4
                ? `<button class="inv-btn inv-btn-sticker" onclick="openStickerPicker('${itemId}',${appliedStickers.length})">🏷</button>`
                : '';
            loadoutBtn  = `<button class="inv-btn inv-btn-loadout${inLoadout ? ' active' : ''}" onclick="toggleLoadout('${itemId}',this)" title="${inLoadout ? 'Remove from loadout' : 'Add to loadout'}">⭐</button>`;
            protectBtn  = `<button class="inv-btn inv-btn-protect${isProtected ? ' active' : ''}" onclick="toggleProtect('${itemId}')" title="${isProtected ? 'Unprotect' : 'Protect from selling/trade-ups/staking'}">${isProtected ? '🔒' : '🔓'}</button>`;
        }

        const isSelected = itemId && selectedSellIds.has(String(itemId));
        const sellModeCard = sellModeActive && itemId;
        const cardClick  = sellModeCard ? ` onclick="toggleSellSelect('${itemId}')" style="cursor:pointer;"` : '';
        const selectMark = sellModeCard
            ? `<div class="inv-sell-check${isSelected ? ' checked' : ''}">${isSelected ? '✓' : ''}</div>`
            : '';
        const actionsHtml = sellModeCard
            ? ''
            : `<div class="inv-actions">
                    ${loadoutBtn}
                    ${protectBtn}
                    ${stickerBtn}
                    ${upgradeBtn}
                    ${itemId ? `<button class="inv-btn inv-btn-sell" onclick="sellItem('${itemId}')" ${isProtected ? 'disabled title="Unprotect this item first"' : ''}>Sell</button>` : ''}
                </div>`;

        return `
            <div class="inv-card rarity-${rar}${inLoadout ? ' in-loadout' : ''}${isSelected ? ' sell-selected' : ''}${isProtected ? ' protected' : ''}"${cardClick}>
                ${selectMark}
                <div class="inv-rarity-stripe" style="background:${rarLabelColor};"></div>
                <div class="inv-img-wrap"${itemId ? ` onclick="if(!sellModeActive) openInspectModal('${itemId}')" style="cursor:pointer;"` : ''}>
                    ${imgHtml}
                    ${typeBadge}
                    ${stBadge}
                    ${lockBadge}
                </div>
                <div class="inv-info">
                    <div class="inv-name" style="color:${rarLabelColor};">${stPrefix}${esc(displayName)}</div>
                    <div class="inv-sub">${subLine}</div>
                    ${cat !== 'sticker' ? floatLineHtml(item.float_value) : ''}
                    <div class="inv-price">$${price.toFixed(2)}</div>
                    ${stickerHtml}
                </div>
                ${actionsHtml}
            </div>
        `;
    }
    function renderPagination() {
        const div = document.getElementById('inventoryPagination');
        if (state.totalPages <= 1) { div.innerHTML = ''; return; }
        let html = '';
        if (state.currentPage > 0) html += `<button class="btn btn-sm btn-primary" onclick="loadInventory(${state.currentPage - 1})">◀ Prev</button>`;
        html += `<span style="color:#888;padding:0 10px;">Page ${state.currentPage + 1} of ${state.totalPages}</span>`;
        if (state.currentPage < state.totalPages - 1) html += `<button class="btn btn-sm btn-primary" onclick="loadInventory(${state.currentPage + 1})">Next ▶</button>`;
        div.innerHTML = html;
    }
    function filterInventory() { renderInventory(); }
    // ── Loadout ──────────────────────────────────────────────
    async function toggleLoadout(itemId, btn) {
        try {
            const data = await apiCall(`/api/inventory/${itemId}/loadout`, { method: 'POST' });
            if (data.success) {
                // Update state and re-render
                const item = (state.inventory || []).find(i => String(i.id) === String(itemId));
                if (item) item.in_loadout = data.in_loadout;
                renderInventory();
                loadLoadout();
                showToast(data.in_loadout ? '⭐ Added to loadout' : 'Removed from loadout');
            }
        } catch(e) { showToast('❌ Error updating loadout'); }
    }

    // ── Protect (blocks sell/trade-up/stake, not sticker cosmetics) ──
    async function toggleProtect(itemId) {
        try {
            const data = await apiCall(`/api/inventory/${itemId}/protect`, { method: 'PATCH' });
            if (data.success) {
                const item = (state.inventory || []).find(i => String(i.id) === String(itemId));
                if (item) item.protected = data.protected;
                renderInventory();
                showToast(data.protected ? '🔒 Item protected' : '🔓 Item unprotected');
            }
        } catch(e) { showToast('❌ Error updating protection'); }
    }

    function loadoutCardHtml(item) {
        const name = item.display_name || item.item_name || '';
        const img  = item.image_url || `/api/skin-image?name=${encodeURIComponent(name)}&t=${Date.now()}`;
        const stickers = (item.applied_stickers || []).slice(0,4);
        const stickerDots = stickers.map(s =>
            `<div class="loadout-card-sticker"><img src="${esc(s.sticker_image)}" title="${esc(s.sticker_name)}" onerror="this.style.display='none'"></div>`
        ).join('');
        return `
            <div class="loadout-card">
                <img src="${esc(img)}" alt="${esc(name)}" onerror="this.src='/static/images/Default CS2 Weapons/weapon_ak47.png'">
                <div class="loadout-card-name">${esc(name)}</div>
                ${item.condition ? `<div style="display:flex;align-items:center;justify-content:center;gap:4px;margin-top:2px;">${condBadgeHtml(item.condition)}${item.float_value != null ? `<span class="float-val">${Number(item.float_value).toFixed(4)}</span>` : ''}</div>` : ''}
                <div class="loadout-card-stickers">${stickerDots}</div>
            </div>`;
    }

    async function loadLoadout() {
        try {
            const data = await apiCall('/api/loadout');
            const grid = document.getElementById('loadoutGrid');
            if (!grid) return;
            if (!data.items || data.items.length === 0) {
                grid.innerHTML = '<div class="loadout-empty">No items in loadout — click ⭐ on any weapon to equip it.</div>';
                return;
            }
            grid.innerHTML = data.items.map(loadoutCardHtml).join('');
        } catch(e) {}
    }

    // ── Named loadouts (multiple showcases) ───────────────────
    async function loadLoadoutsList() {
        try {
            const data = await apiCall('/api/loadouts');
            const sel = document.getElementById('loadoutSelector');
            if (!sel) return;
            const loadouts = data.loadouts || [];
            state.loadouts = loadouts;
            if (loadouts.length === 0) {
                sel.innerHTML = '<option value="">My Loadout</option>';
                return;
            }
            sel.innerHTML = loadouts.map(l =>
                `<option value="${l.id}"${l.is_active ? ' selected' : ''}>${esc(l.name)} (${l.item_count})</option>`
            ).join('');
        } catch(e) {}
    }

    async function switchLoadout(loadoutId) {
        if (!loadoutId) return;
        try {
            await apiCall(`/api/loadouts/${loadoutId}/activate`, { method: 'POST' });
            await loadLoadoutsList();
            await loadLoadout();
            await loadInventory(state.currentPage);
            showToast('🎽 Switched loadout');
        } catch(e) { showToast('❌ Error switching loadout'); }
    }

    async function createLoadoutPrompt() {
        const name = prompt('Name this loadout:');
        if (!name || !name.trim()) return;
        try {
            await apiCall('/api/loadouts', { method: 'POST', body: JSON.stringify({ name: name.trim() }) });
            await loadLoadoutsList();
            await loadLoadout();
            await loadInventory(state.currentPage);
            showToast('✅ Loadout created');
        } catch(e) { showToast('❌ ' + (e.message || 'Error creating loadout')); }
    }

    async function renameLoadoutPrompt() {
        const sel = document.getElementById('loadoutSelector');
        const loadoutId = sel && sel.value;
        if (!loadoutId) { showToast('❌ No loadout selected'); return; }
        const current = (state.loadouts || []).find(l => String(l.id) === String(loadoutId));
        const name = prompt('Rename loadout:', current ? current.name : '');
        if (!name || !name.trim()) return;
        try {
            await apiCall(`/api/loadouts/${loadoutId}`, { method: 'PATCH', body: JSON.stringify({ name: name.trim() }) });
            await loadLoadoutsList();
            showToast('✅ Loadout renamed');
        } catch(e) { showToast('❌ Error renaming loadout'); }
    }

    async function deleteLoadoutPrompt() {
        const sel = document.getElementById('loadoutSelector');
        const loadoutId = sel && sel.value;
        if (!loadoutId) { showToast('❌ No loadout selected'); return; }
        if (!confirm('Delete this loadout? Items stay in your inventory.')) return;
        try {
            await apiCall(`/api/loadouts/${loadoutId}`, { method: 'DELETE' });
            await loadLoadoutsList();
            await loadLoadout();
            await loadInventory(state.currentPage);
            showToast('🗑 Loadout deleted');
        } catch(e) { showToast('❌ Error deleting loadout'); }
    }

    // ── Welcome modal (first 2 logins) ────────────────────────
    function maybeShowWelcomeModal(loginCount) {
        if (loginCount == null || loginCount > 2) return;
        try {
            if (sessionStorage.getItem('welcome_modal_shown') === '1') return;
            sessionStorage.setItem('welcome_modal_shown', '1');
        } catch(e) {}
        const overlay = document.getElementById('welcomeModalOverlay');
        if (overlay) overlay.classList.add('show');
    }

    function closeWelcomeModal() {
        const overlay = document.getElementById('welcomeModalOverlay');
        if (overlay) overlay.classList.remove('show');
    }

    // ── Sticker application ───────────────────────────────────
    let _currentInspectItemId = null;

    // Composites a weapon's applied stickers onto its image using plain
    // absolutely-positioned <img> overlays -- no canvas/3D needed, this
    // codebase's art is flat 2D and layered <img>+CSS transform is enough.
    // `justAppliedSlot` (optional) gets the CSS class that plays the
    // stamp-in animation once, for the sticker that was just applied.
    function renderWeaponWithStickers(item, imgUrl, displayName, justAppliedSlot) {
        const appliedStickers = Array.isArray(item.applied_stickers) ? item.applied_stickers : [];
        const overlays = appliedStickers.map(s => {
            if (!s.sticker_image) return '';
            const x = (s.x != null ? s.x : 0.5) * 100;
            const y = (s.y != null ? s.y : 0.5) * 100;
            const rot = s.rotation != null ? s.rotation : 0;
            const scale = s.scale != null ? s.scale : 1.0;
            const justApplied = justAppliedSlot != null && s.slot === justAppliedSlot;
            return `<img class="item-inspect-sticker-overlay${justApplied ? ' stamp-in' : ''}" src="${esc(s.sticker_image)}" alt="${esc(s.sticker_name || '')}"
                        style="left:${x}%;top:${y}%;--srot:${rot}deg;--sscale:${scale};cursor:pointer;pointer-events:auto;"
                        title="Click to reposition" onclick="openStickerSandboxForExisting('${item.id}',${s.slot})"
                        onerror="this.style.display='none'">`;
        }).join('');
        return `
            <div class="item-inspect-weapon-wrap">
                <img class="item-inspect-img" src="${imgUrl}" alt="${esc(displayName)}" onerror="this.style.display='none'">
                ${overlays}
            </div>
        `;
    }

    function openInspectModal(itemId, justAppliedSlot) {
        const item = (state.inventory || []).find(i => String(i.id) === String(itemId));
        if (!item) return;
        _currentInspectItemId = String(itemId);
        const cat = classifyItem(item);
        const cleanName = cleanItemName((item.item_name || '').trim());
        const rar = (item.rarity || 'Blue').trim();
        const price = typeof item.price === 'number' ? item.price : parseFloat(item.price || 0);
        const isST = item.is_stattrak;
        const cond = item.condition || '';

        let displayName, rarLabel, rarColor, imgUrl;
        if (cat === 'sticker') {
            displayName = cleanName;
            const stickerRar = STICKER_RARITY_LABELS[rar];
            rarLabel = stickerRar ? stickerRar.label : rar;
            rarColor = stickerRar ? stickerRar.color : '#4488ff';
            imgUrl = item.image_url || '';
        } else if (cat === 'gold') {
            displayName = cleanName.replace(/^★\s*/, '').trim();
            rarLabel = 'Gold';
            rarColor = '#ffd700';
            imgUrl = item.image_url || `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;
        } else {
            displayName = cleanName;
            rarLabel = rar;
            rarColor = SKIN_RARITY_COLORS[rar] || '#4488ff';
            imgUrl = item.image_url || `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;
        }

        const stPrefix = isST ? '<span style="color:#ff6b00;">🔥 StatTrak™ </span>' : '';

        const statsHtml = `
            <div><span class="label">PRICE</span><span class="value">$${price.toFixed(2)}</span></div>
            ${cond ? `<div><span class="label">CONDITION</span><span class="value">${esc(cond)}</span></div>` : ''}
            ${item.float_value != null && item.float_value !== '' ? `<div><span class="label">FLOAT</span><span class="value">${Number(item.float_value).toFixed(4)}</span></div>` : ''}
            <div><span class="label">STATTRAK</span><span class="value">${isST ? 'Yes 🔥' : 'No'}</span></div>
        `;

        let stickersHtml = '';
        if (cat !== 'sticker') {
            const appliedStickers = Array.isArray(item.applied_stickers) ? item.applied_stickers : [];
            const slots = [0,1,2,3].map(slot => {
                const s = appliedStickers.find(x => x.slot === slot);
                if (s && s.sticker_image) {
                    return `<div class="item-inspect-sticker-slot" title="${esc(s.sticker_name)}" onclick="removeStickerSlot('${itemId}',${slot});closeItemInspect();">
                                <img src="${esc(s.sticker_image)}" onerror="this.parentElement.textContent='🏷️'">
                            </div>`;
                }
                return `<div class="item-inspect-sticker-slot" title="Slot ${slot+1} — click to apply" onclick="openStickerPicker('${itemId}',${slot})">+</div>`;
            }).join('');
            stickersHtml = `<div class="item-inspect-stickers">${slots}</div>`;
        }

        const actionsHtml = `
            <div class="item-inspect-actions">
                ${cat !== 'sticker' ? `<button class="btn btn-secondary" onclick="toggleLoadout('${itemId}', null); closeItemInspect();">⭐ ${item.in_loadout ? 'Remove Loadout' : 'Add Loadout'}</button>` : ''}
                ${cat !== 'sticker' ? `<button class="btn btn-secondary" onclick="toggleProtect('${itemId}'); closeItemInspect();">${item.protected ? '🔓 Unprotect' : '🔒 Protect'}</button>` : ''}
                <button class="btn btn-danger" onclick="closeItemInspect(); sellItem('${itemId}');" ${item.protected ? 'disabled title="Unprotect this item first"' : ''}>Sell</button>
            </div>
        `;

        const heroHtml = cat === 'sticker'
            ? `<div class="item-inspect-hero"><img class="item-inspect-img" src="${imgUrl}" alt="${esc(displayName)}" onerror="this.style.display='none'"></div>`
            : renderWeaponWithStickers(item, imgUrl, displayName, justAppliedSlot);

        document.getElementById('itemInspectBody').innerHTML = `
            ${heroHtml}
            <div class="item-inspect-name" style="color:${rarColor};">${stPrefix}${esc(displayName)}</div>
            <div class="item-inspect-rarity" style="color:${rarColor};">${esc(rarLabel)}</div>
            <div class="item-inspect-stats">${statsHtml}</div>
            ${stickersHtml}
            ${actionsHtml}
        `;
        document.getElementById('itemInspectOverlay').classList.add('show');
    }

    function closeItemInspect() {
        document.getElementById('itemInspectOverlay').classList.remove('show');
        _currentInspectItemId = null;
    }

    // ── Sticker application ──────────────────────────────────
    let _lastStickerPickerList = [];

    async function openStickerPicker(weaponId, slot) {
        try {
            const data = await apiCall('/api/user/me/inventory?item_type=sticker&limit=100');
            const stickers = (data.items || []);
            _lastStickerPickerList = stickers;
            if (stickers.length === 0) {
                showToast('You have no stickers — open sticker capsules first!');
                return;
            }
            const stickerList = stickers.map(s => {
                const img = s.image_url
                    ? `<img src="${esc(s.image_url)}" style="width:44px;height:44px;object-fit:contain;flex-shrink:0;" onerror="this.style.display='none'">`
                    : '<span style="font-size:30px;flex-shrink:0;">🏷️</span>';
                return `<div style="display:flex;align-items:center;gap:12px;padding:12px 10px;border-radius:8px;border:1px solid rgba(255,255,255,0.08);cursor:pointer;background:rgba(255,255,255,0.03);min-height:60px;"
                             onclick="openStickerSandbox('${weaponId}',${slot},'${s.id}')">
                            ${img}
                            <div style="flex:1;min-width:0;">
                                <div style="font-size:12px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(s.item_name || '')}</div>
                                <div style="font-size:10px;color:#888;margin-top:2px;">${esc(s.rarity || '')}</div>
                            </div>
                        </div>`;
            }).join('');
            document.getElementById('popupBody').innerHTML = `
                <h3 style="margin-bottom:12px;">🏷️ Apply Sticker — Slot ${slot + 1}</h3>
                <p style="color:#888;font-size:12px;margin-bottom:12px;">Pick a sticker, then position it on the sandbox before it's applied.</p>
                <div style="display:flex;flex-direction:column;gap:8px;max-height:50vh;overflow-y:auto;-webkit-overflow-scrolling:touch;">${stickerList}</div>
                <button class="btn btn-secondary" style="margin-top:14px;width:100%;padding:12px;font-size:14px;" onclick="closePopup()">Cancel</button>
            `;
            document.getElementById('popupOverlay').classList.add('show');
        } catch(e) { showToast('❌ Error loading stickers'); }
    }

    async function applySticker(weaponId, slot, stickerId, x, y, rotation, scale) {
        try {
            const body = { sticker_id: parseInt(stickerId), slot: slot };
            if (x != null) body.x = x;
            if (y != null) body.y = y;
            if (rotation != null) body.rotation = rotation;
            if (scale != null) body.scale = scale;
            const data = await apiCall(`/api/inventory/${weaponId}/sticker`, {
                method: 'POST',
                body: JSON.stringify(body)
            });
            if (data.success) {
                closePopup();
                // Update local state
                const item = (state.inventory || []).find(i => String(i.id) === String(weaponId));
                if (item) item.applied_stickers = data.applied_stickers;
                // Remove sticker from inventory state
                state.inventory = (state.inventory || []).filter(i => String(i.id) !== String(stickerId));
                renderInventory();
                loadLoadout();
                showToast('🏷️ Sticker applied!');
            }
        } catch(e) { showToast('❌ Error applying sticker'); }
    }

    async function repositionSticker(weaponId, slot, x, y, rotation, scale) {
        try {
            const data = await apiCall(`/api/inventory/${weaponId}/sticker/${slot}`, {
                method: 'PATCH',
                body: JSON.stringify({ x, y, rotation, scale })
            });
            if (data.success) {
                const item = (state.inventory || []).find(i => String(i.id) === String(weaponId));
                if (item) item.applied_stickers = data.applied_stickers;
                renderInventory();
                loadLoadout();
                showToast('🏷️ Sticker repositioned!');
            }
        } catch(e) { showToast('❌ Error repositioning sticker'); }
    }

    // ── Sticker sandbox (drag / rotate / resize) ──────────────
    // All preview here is purely client-side -- apply/reposition is only
    // called on Confirm. This is the ONLY path that ever calls applySticker
    // now (the plain sticker-list click used to apply instantly; it now
    // opens the sandbox first), and openStickerSandboxForExisting is the
    // entry point for repositioning an already-applied sticker by clicking
    // it directly on the composited weapon image.
    let _sandboxState = null;
    let _sandboxDragMode = null;

    function openStickerSandbox(weaponId, slot, stickerId, existing) {
        closePopup();
        const item = (state.inventory || []).find(i => String(i.id) === String(weaponId));
        if (!item) return;

        let stickerName, stickerImage;
        if (existing) {
            stickerName = existing.stickerName;
            stickerImage = existing.stickerImage;
        } else {
            const picked = _lastStickerPickerList.find(s => String(s.id) === String(stickerId));
            if (!picked) { showToast('❌ Sticker not found'); return; }
            stickerName = picked.item_name || '';
            stickerImage = picked.image_url || '';
        }
        if (!stickerImage) { showToast('❌ This sticker has no image to preview'); return; }

        const cleanName = cleanItemName((item.item_name || '').trim());
        const imgUrl = item.image_url || `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;

        _sandboxState = {
            weaponId: String(weaponId), slot, stickerId,
            x:        existing ? existing.x : 0.5,
            y:        existing ? existing.y : 0.5,
            rotation: existing ? existing.rotation : 0,
            scale:    existing ? existing.scale : 1.0,
            isReposition: !!existing,
        };

        document.getElementById('itemInspectBody').innerHTML = `
            <div class="sticker-sandbox-hint">Drag the sticker to position it. Use the ↻ handle to rotate, ⤡ to resize.</div>
            <div class="sticker-sandbox-wrap" id="sandboxWrap">
                <img class="item-inspect-img" src="${imgUrl}" alt="${esc(cleanName)}" onerror="this.style.display='none'">
                <img class="sticker-sandbox-item" id="sandboxStickerImg" src="${esc(stickerImage)}" alt="${esc(stickerName)}"
                     style="left:${_sandboxState.x * 100}%;top:${_sandboxState.y * 100}%;--srot:${_sandboxState.rotation}deg;--sscale:${_sandboxState.scale};"
                     onerror="this.style.display='none'">
                <div class="sticker-sandbox-handle sticker-sandbox-rotate-handle" id="sandboxRotateHandle" title="Drag to rotate">↻</div>
                <div class="sticker-sandbox-handle sticker-sandbox-resize-handle" id="sandboxResizeHandle" title="Drag to resize">⤡</div>
            </div>
            <div class="item-inspect-actions">
                <button class="btn btn-secondary" onclick="cancelStickerSandbox()">Cancel</button>
                <button class="btn btn-primary" onclick="confirmStickerSandbox()">Confirm Placement</button>
            </div>
        `;
        document.getElementById('itemInspectOverlay').classList.add('show');
        _positionSandboxHandles();
        _bindSandboxPointerEvents();
    }

    function openStickerSandboxForExisting(weaponId, slot) {
        const item = (state.inventory || []).find(i => String(i.id) === String(weaponId));
        if (!item) return;
        const s = (item.applied_stickers || []).find(x => x.slot === slot);
        if (!s || !s.sticker_image) return;
        openStickerSandbox(weaponId, slot, s.sticker_id, {
            stickerName: s.sticker_name, stickerImage: s.sticker_image,
            x:        s.x != null ? s.x : 0.5,
            y:        s.y != null ? s.y : 0.5,
            rotation: s.rotation != null ? s.rotation : 0,
            scale:    s.scale != null ? s.scale : 1.0,
        });
    }

    function _positionSandboxHandles() {
        const wrap   = document.getElementById('sandboxWrap');
        const sticker = document.getElementById('sandboxStickerImg');
        const rotateHandle = document.getElementById('sandboxRotateHandle');
        const resizeHandle = document.getElementById('sandboxResizeHandle');
        if (!wrap || !sticker || !rotateHandle || !resizeHandle) return;
        const wrapRect = wrap.getBoundingClientRect();
        const stickerRect = sticker.getBoundingClientRect();
        const centerX = stickerRect.left + stickerRect.width / 2 - wrapRect.left;
        const centerY = stickerRect.top + stickerRect.height / 2 - wrapRect.top;
        const halfW = stickerRect.width / 2, halfH = stickerRect.height / 2;
        rotateHandle.style.left = centerX + 'px';
        rotateHandle.style.top  = (centerY - halfH - 22) + 'px';
        resizeHandle.style.left = (centerX + halfW) + 'px';
        resizeHandle.style.top  = (centerY + halfH) + 'px';
    }

    function _bindSandboxPointerEvents() {
        const sticker = document.getElementById('sandboxStickerImg');
        const rotateHandle = document.getElementById('sandboxRotateHandle');
        const resizeHandle = document.getElementById('sandboxResizeHandle');
        if (!sticker || !rotateHandle || !resizeHandle) return;
        [sticker, rotateHandle, resizeHandle].forEach(el => { el.style.touchAction = 'none'; });
        sticker.onpointerdown      = (e) => _sandboxPointerDown(e, 'move');
        rotateHandle.onpointerdown = (e) => _sandboxPointerDown(e, 'rotate');
        resizeHandle.onpointerdown = (e) => _sandboxPointerDown(e, 'resize');
    }

    function _sandboxPointerDown(e, mode) {
        e.preventDefault();
        e.stopPropagation();
        _sandboxDragMode = mode;
        document.addEventListener('pointermove', _sandboxPointerMove);
        document.addEventListener('pointerup', _sandboxPointerUp);
    }

    function _sandboxPointerMove(e) {
        if (!_sandboxState || !_sandboxDragMode) return;
        const wrap = document.getElementById('sandboxWrap');
        const sticker = document.getElementById('sandboxStickerImg');
        if (!wrap || !sticker) return;
        const rect = wrap.getBoundingClientRect();
        const stickerRect = sticker.getBoundingClientRect();
        const centerX = stickerRect.left + stickerRect.width / 2;
        const centerY = stickerRect.top + stickerRect.height / 2;

        if (_sandboxDragMode === 'move') {
            let x = (e.clientX - rect.left) / rect.width;
            let y = (e.clientY - rect.top) / rect.height;
            x = Math.max(0, Math.min(1, x));
            y = Math.max(0, Math.min(1, y));
            _sandboxState.x = x;
            _sandboxState.y = y;
            sticker.style.left = (x * 100) + '%';
            sticker.style.top  = (y * 100) + '%';
        } else if (_sandboxDragMode === 'rotate') {
            const dx = e.clientX - centerX;
            const dy = e.clientY - centerY;
            let angle = Math.atan2(dy, dx) * 180 / Math.PI + 90;
            angle = (angle + 360) % 360;
            _sandboxState.rotation = angle;
            sticker.style.setProperty('--srot', angle + 'deg');
        } else if (_sandboxDragMode === 'resize') {
            const dx = e.clientX - centerX;
            const dy = e.clientY - centerY;
            const dist = Math.sqrt(dx * dx + dy * dy);
            const currentHalfDiag = Math.sqrt(Math.pow(stickerRect.width / 2, 2) + Math.pow(stickerRect.height / 2, 2));
            const naturalHalfDiag = currentHalfDiag / (_sandboxState.scale || 1);
            let scale = naturalHalfDiag > 0 ? dist / naturalHalfDiag : 1;
            scale = Math.max(0.3, Math.min(2.5, scale));
            _sandboxState.scale = scale;
            sticker.style.setProperty('--sscale', scale);
        }
        _positionSandboxHandles();
    }

    function _sandboxPointerUp() {
        _sandboxDragMode = null;
        document.removeEventListener('pointermove', _sandboxPointerMove);
        document.removeEventListener('pointerup', _sandboxPointerUp);
    }

    async function confirmStickerSandbox() {
        if (!_sandboxState) return;
        const { weaponId, slot, stickerId, x, y, rotation, scale, isReposition } = _sandboxState;
        _sandboxState = null;
        _sandboxDragMode = null;
        if (isReposition) {
            await repositionSticker(weaponId, slot, x, y, rotation, scale);
            openInspectModal(weaponId);
        } else {
            await applySticker(weaponId, slot, stickerId, x, y, rotation, scale);
            openInspectModal(weaponId, slot);
        }
    }

    function cancelStickerSandbox() {
        const weaponId = _sandboxState ? _sandboxState.weaponId : null;
        _sandboxState = null;
        _sandboxDragMode = null;
        if (weaponId) {
            openInspectModal(weaponId);
        } else {
            closeItemInspect();
        }
    }

    async function removeStickerSlot(weaponId, slot) {
        if (!confirm(`Remove sticker from slot ${slot + 1}? The sticker will be lost.`)) return;
        try {
            const data = await apiCall(`/api/inventory/${weaponId}/sticker/${slot}`, { method: 'DELETE' });
            if (data.success) {
                const item = (state.inventory || []).find(i => String(i.id) === String(weaponId));
                if (item) item.applied_stickers = data.applied_stickers;
                renderInventory();
                loadLoadout();
                showToast('Sticker removed.');
            }
        } catch(e) { showToast('❌ Error removing sticker'); }
    }

    async function sellItem(itemId) {
        // Guard: itemId must be a valid integer
        const id = parseInt(itemId);
        if (!itemId || isNaN(id) || id <= 0) {
            showToast('❌ Invalid item — please refresh inventory', 'error');
            return;
        }
        if (!confirm('Sell this item for 70% of its value?')) return;
        try {
            const res = await fetch('/api/sell-item', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ item_id: id })
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                showToast(`❌ ${data.error || data.detail || `Error ${res.status}`}`, 'error');
                return;
            }
            const data = await res.json();
            if (data.success) {
                showToast(`✅ Sold for $${Number(data.sell_price).toFixed(2)}`);
                await loadBalance();
                await loadInventory(state.currentPage);
                await loadStats();
                await loadQuests();
            } else {
                showToast(`❌ ${data.error || 'Failed to sell'}`, 'error');
            }
        } catch (e) {
            showToast(`❌ Error selling item: ${e.message}`, 'error');
            console.error(e);
        }
    }
    async function upgradeItem(itemId) {
        Sound.click();
        try {
            const res = await fetch('/api/skin-upgrade', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ item_id: parseInt(itemId) }) });
            if (!res.ok) { const text = await res.text(); throw new Error(`HTTP ${res.status}: ${text}`); }
            const data = await res.json();
            if (data.success) {
                if (data.upgraded) {
                    Sound.jackpot();
                    showToast(`⬆️ Success! ${data.old_item_name} → ${data.new_item_name} (${data.new_rarity})`);
                    spawnConfetti('Gold');
                } else {
                    Sound.loss();
                    showToast(`💔 Upgrade failed! Lost ${data.old_item_name}. Cost: $${data.cost}`, 'error');
                }
                await loadInventory(state.currentPage);
                await loadBalance();
                await loadStats();
            } else {
                Sound.error();
                showToast(`❌ ${data.error || 'Upgrade failed'}`, 'error');
            }
        } catch (e) {
            Sound.error();
            showToast(`❌ Error upgrading item: ${e.message}`, 'error');
            console.error(e);
        }
    }

    // ============================================
    // SECTION 11: DAILY, STATS, QUESTS (unchanged)
    // ============================================
    async function claimDaily() {
        const resultDiv = document.getElementById('dailyResult');
        resultDiv.innerHTML = '<span class="loading">Claiming...</span>';
        try {
            const data = await apiCall('/api/daily', { method: 'POST', body: JSON.stringify({}) });
            if (data.success) {
                const jackpotMsg = data.jackpot ? ' 🎰 JACKPOT BONUS!' : '';
                resultDiv.innerHTML = `<span class="success">✅ Claimed $${data.reward}! (${data.streak} day streak)${jackpotMsg}</span>`;
                if (data.jackpot) {
                    spawnConfettiExplosion();
                    spawnRainbowConfetti();
                    spawnCoinShower(50);
                    showToast('🎉🎉 JACKPOT! Extra $50,000! 🎉🎉', 'success');
                } else if (data.streak >= 10) {
                    spawnConfetti('Gold');
                    spawnCoinShower(20);
                }
                await loadBalance();
                await loadStats();
                await loadQuests();
                try { await maybeOpenDailySpin(); } catch(e) {}
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Failed to claim daily')}</span>`;
            }
        } catch (e) {
            resultDiv.innerHTML = '<span class="error">❌ Error claiming daily</span>';
        }
    }

    // ── Daily Spin — bonus ticket wheel unlocked after claiming Daily above.
    // VIP members' flat daily ticket grant (routes/premium.py's midnight cron)
    // is separate and unaffected; this wheel is an additional reward everyone
    // (VIP or not) gets once they've claimed their Daily.
    const DAILY_SPIN_COLORS = ['#4488ff','#aa44ff','#ff69b4','#ff3333','#ffd700','#4488ff','#aa44ff','#ff69b4'];
    let _dailySpinSegments = [];
    let _dailySpinRotation = 0;

    async function maybeOpenDailySpin() {
        try {
            const data = await apiCall('/api/daily/spin/status');
            if (data && data.available) openDailySpinModal(data.segments);
        } catch(e) {}
    }

    function openDailySpinModal(segments) {
        _dailySpinSegments = segments;
        _dailySpinRotation = 0;
        document.getElementById('popupBody').innerHTML = `
            <h3 style="color:#ffd700;text-align:center;margin-bottom:6px;">🎡 Daily Bonus Spin!</h3>
            <p style="color:#888;font-size:12px;text-align:center;margin-bottom:16px;">Spin once a day for free bonus tickets — everyone gets one.</p>
            <div id="dailySpinWheelWrap" style="position:relative;margin:0 auto;">
                <div style="position:absolute;top:-8px;left:50%;transform:translateX(-50%);font-size:22px;color:#ffd700;z-index:2;">▼</div>
                <div id="dailySpinWheel" style="border-radius:50%;border:4px solid #ffd700;position:relative;transition:transform 4.5s cubic-bezier(0.15,0.85,0.25,1);"></div>
            </div>
            <div style="text-align:center;margin-top:20px;">
                <button class="btn btn-gold" id="dailySpinBtn" onclick="spinDailyWheel()">🎡 SPIN FOR TICKETS</button>
            </div>
            <div id="dailySpinResult" style="text-align:center;margin-top:12px;font-size:14px;min-height:20px;"></div>
        `;
        document.getElementById('popupOverlay').classList.add('show');
        // Measure the real available width now that the modal is actually laid
        // out, instead of guessing off window.innerWidth -- that guess ignored
        // the popup's own padding/max-width math and stayed pinned near the old
        // 240px on most real phones (~375-430px wide), which is why the wheel
        // still looked cramped/off after the first pass at this fix.
        const bodyWidth = document.getElementById('popupBody').clientWidth || window.innerWidth * 0.8;
        const wheelSize = Math.round(Math.max(160, Math.min(260, bodyWidth * 0.86)));
        const wrap = document.getElementById('dailySpinWheelWrap');
        wrap.style.width = wheelSize + 'px';
        wrap.style.height = wheelSize + 'px';
        const wheel = document.getElementById('dailySpinWheel');
        wheel.style.width = wheelSize + 'px';
        wheel.style.height = wheelSize + 'px';
        renderDailySpinWheel(segments, wheelSize);
    }

    function renderDailySpinWheel(segments, wheelSize) {
        const wheel = document.getElementById('dailySpinWheel');
        if (!wheel) return;
        wheelSize = wheelSize || 240;
        const labelRadius = Math.round(wheelSize * 0.383); // proportional to the original 92px @ 240px wheel
        const fontSize = wheelSize < 200 ? 14 : 16;
        const n = segments.length;
        const slice = 360 / n;
        const stops = segments.map((_, i) => {
            const color = DAILY_SPIN_COLORS[i % DAILY_SPIN_COLORS.length];
            return `${color} ${i * slice}deg ${(i + 1) * slice}deg`;
        }).join(', ');
        wheel.style.background = `conic-gradient(${stops})`;
        // Numbers only in each slice -- a tiny inline 🎟️ per segment renders as
        // an illegible box at this font size on some devices/browsers. A single
        // larger ticket icon on the center hub gives the same context cleanly.
        const labels = segments.map((amt, i) => {
            const angle = i * slice + slice / 2;
            return `<div style="position:absolute;top:50%;left:50%;width:0;height:0;transform:rotate(${angle}deg) translate(0,-${labelRadius}px) rotate(${-angle}deg);color:#fff;font-weight:bold;font-size:${fontSize}px;text-shadow:0 1px 3px rgba(0,0,0,0.9);white-space:nowrap;">${amt}</div>`;
        }).join('');
        const hubSize = Math.round(wheelSize * 0.22);
        const hub = `<div style="position:absolute;top:50%;left:50%;width:${hubSize}px;height:${hubSize}px;margin:-${hubSize / 2}px 0 0 -${hubSize / 2}px;border-radius:50%;background:#1a1a2e;border:3px solid #ffd700;display:flex;align-items:center;justify-content:center;font-size:${Math.round(hubSize * 0.5)}px;box-shadow:0 0 12px rgba(0,0,0,0.6);">🎟️</div>`;
        wheel.innerHTML = labels + hub;
    }

    async function spinDailyWheel() {
        const btn = document.getElementById('dailySpinBtn');
        const resultDiv = document.getElementById('dailySpinResult');
        if (btn.disabled) return;
        btn.disabled = true;
        resultDiv.textContent = '';
        try {
            const data = await apiCall('/api/daily/spin', { method: 'POST', body: JSON.stringify({}) });
            if (data.success) {
                const n = _dailySpinSegments.length;
                const slice = 360 / n;
                const targetAngle = data.segment_index * slice + slice / 2;
                const spins = 5;
                _dailySpinRotation += spins * 360 + (360 - (_dailySpinRotation % 360) - targetAngle + 360) % 360;
                document.getElementById('dailySpinWheel').style.transform = `rotate(${_dailySpinRotation}deg)`;
                Sound.spin();
                setTimeout(async () => {
                    resultDiv.innerHTML = `<span class="success">🎉 You won ${data.tickets_won} 🎟️ ticket${data.tickets_won === 1 ? '' : 's'}!</span>`;
                    Sound.win();
                    spawnConfetti('Gold');
                    await loadTicketBalance();
                }, 4600);
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Could not spin')}</span>`;
                btn.disabled = false;
                Sound.error();
            }
        } catch (e) {
            resultDiv.innerHTML = `<span class="error">❌ ${esc(e.message || 'Error spinning')}</span>`;
            btn.disabled = false;
            Sound.error();
        }
    }
    async function loadStats() {
        try {
            const data = await apiCall('/api/user/me/stats');
            const grid = document.getElementById('statsGrid');
            if (grid) {
                grid.innerHTML = `
                    <div class="stat-box"><div class="value">${data.total_opens || 0}</div><div class="label">📦 Cases Opened</div></div>
                    <div class="stat-box"><div class="value">${data.total_golds || 0}</div><div class="label">⭐ Golds Found</div></div>
                    <div class="stat-box"><div class="value">${data.total_trades || 0}</div><div class="label">🔄 Trade-Ups</div></div>
                    <div class="stat-box"><div class="value">${data.daily_streak || 0}</div><div class="label">📅 Day Streak</div></div>
                    <div class="stat-box"><div class="value">${data.inventory_count || 0}</div><div class="label">🎮 Inventory Items</div></div>
                    <div class="stat-box"><div class="value">$${data.inventory_value ? data.inventory_value.toFixed(2) : '0.00'}</div><div class="label">💎 Inventory Value</div></div>
                    <div class="stat-box"><div class="value">🔥 ${state.streakData.current_streak || 0}</div><div class="label">Current Streak</div></div>
                    <div class="stat-box"><div class="value">🏆 ${state.streakData.best_streak || 0}</div><div class="label">Best Streak</div></div>
                `;
            }
        } catch (e) { console.error('Load stats error:', e); }
    }
    async function loadQuests() {
        const list = document.getElementById('questsList');
        const claimBtn = document.getElementById('claimQuestsBtn');
        try {
            const data = await apiCall('/api/user/me/quests');
            const quests = data.quests || [];
            if (quests.length === 0) {
                list.innerHTML = '<p style="color:#888;">No quests available.</p>';
                claimBtn.disabled = true;
                return;
            }
            const questNames = { 'open_cases': '🔑 Open Cases', 'get_golds': '✨ Find Gold Items', 'earn_money': '💰 Earn Money', 'trade_up': '🔄 Complete Trade-Ups', 'sell_items': '💸 Sell Items', 'daily_streak': '📅 Daily Streak', 'jackpot_win': '🎲 Hit the Jackpot', 'play_games': '🎮 Play Games', 'win_games': '🏆 Win Games' };
            let allComplete = true;
            list.innerHTML = quests.map(q => {
                const name = questNames[q.quest_type] || q.quest_type;
                const progress = q.progress || 0;
                const required = q.required || 1;
                const pct = Math.min(100, (progress / required) * 100);
                const isComplete = q.completed || progress >= required;
                if (!isComplete) allComplete = false;
                return `
                    <div class="quest-item">
                        <div class="quest-header">
                            <span class="quest-name">${name}</span>
                            <span class="quest-status ${isComplete ? 'completed' : 'incomplete'}">${isComplete ? '✅ Complete' : `${progress}/${required}`}</span>
                        </div>
                        <div class="quest-progress"><div class="quest-fill" style="width:${pct}%;${isComplete ? 'background:linear-gradient(90deg,#4caf50,#2e7d32);' : 'background:linear-gradient(90deg,#ffd700,#ff6b00);'}"></div></div>
                        <div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin-top:5px;"><span>Reward: $${q.reward || 0}</span>${isComplete ? '<span style="color:#4caf50;">✓ Ready to claim!</span>' : ''}</div>
                    </div>
                `;
            }).join('');
            claimBtn.disabled = !allComplete;
            claimBtn.textContent = allComplete ? '🎁 Claim All Rewards!' : '🎁 Complete all quests to claim!';
        } catch (e) {
            list.innerHTML = '<p style="color:#888;">Unable to load quests.</p>';
            claimBtn.disabled = true;
        }
    }
    async function claimQuests() {
        const resultDiv = document.getElementById('questResult');
        resultDiv.innerHTML = '<span class="loading">Claiming...</span>';
        try {
            const data = await apiCall('/api/claim', { method: 'POST', body: JSON.stringify({}) });
            if (data.success) {
                resultDiv.innerHTML = `<span class="success">✅ ${esc(data.message || 'Quests claimed!')}</span>`;
                if (data.total_reward > 2000) { spawnConfetti('Gold'); spawnCoinShower(25); }
                else if (data.total_reward > 1000) { spawnConfetti('Purple'); spawnCoinShower(15); }
                await loadBalance();
                await loadStats();
                await loadQuests();
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Failed to claim')}</span>`;
            }
        } catch (e) {
            resultDiv.innerHTML = '<span class="error">❌ Error claiming quests</span>';
        }
    }


    // ============================================
    // SECTION 13: PREMIUM CASES (modified for new reel)
    // ============================================
    // ============================================
    // SECTION: VIP / PREMIUM SYSTEM
    // ============================================

    const VIP_COLORS = {
        none:     { bg: 'rgba(255,255,255,0.04)', border: 'rgba(255,255,255,0.1)', text: '#888' },
        silver:   { bg: 'rgba(192,192,192,0.08)', border: 'rgba(192,192,192,0.4)', text: '#c0c0c0' },
        gold:     { bg: 'rgba(255,215,0,0.08)',   border: 'rgba(255,215,0,0.5)',   text: '#ffd700' },
        platinum: { bg: 'rgba(140,210,255,0.08)', border: 'rgba(140,210,255,0.5)', text: '#8cd2ff' },
    };

    async function loadVIPStatus() {
        try {
            const status = await apiCall('/api/vip/status');
            _renderVIPBanner(status);
            _renderTiers(status);
            _loadBatchCaseList();
            _loadTicketHistory();
        } catch(e) { console.error('VIP load error:', e); }
    }

    function _renderVIPBanner(s) {
        const el = document.getElementById('vipStatusBanner');
        if (!el) return;
        if (!s.active || s.tier === 'none') {
            el.innerHTML = `
                <div style="padding:14px 18px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:10px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;">
                    <div style="color:#888;font-size:13px;">You don't have an active VIP subscription.</div>
                    <div style="color:#ffd700;font-size:13px;">🎟️ Tickets: <strong>${s.tickets || 0}</strong></div>
                </div>`;
            return;
        }
        const c = VIP_COLORS[s.tier] || VIP_COLORS.none;
        const expires = s.expires_at ? new Date(s.expires_at) : null;
        const daysLeft = expires ? Math.max(0, Math.ceil((expires - Date.now()) / 86400000)) : 0;
        el.innerHTML = `
            <div style="padding:16px 20px;background:${c.bg};border:2px solid ${c.border};border-radius:12px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
                <div>
                    <div style="font-size:18px;font-weight:bold;color:${c.text};">${s.label || s.tier.toUpperCase()} Member</div>
                    <div style="font-size:12px;color:#888;margin-top:2px;">Expires in <strong style="color:${c.text};">${daysLeft} days</strong> · Win Boost: <strong style="color:#4caf50;">+${Math.round((s.boost-1)*100)}%</strong></div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:24px;font-weight:bold;color:#ffd700;">${s.tickets} 🎟️</div>
                    <div style="font-size:11px;color:#888;">Tickets Available</div>
                </div>
                <button class="btn btn-sm" style="background:rgba(255,50,50,0.15);border:1px solid rgba(255,50,50,0.3);color:#ff6b6b;" onclick="cancelVIP()">Cancel VIP</button>
            </div>`;
    }

    async function _renderTiers(currentStatus) {
        const grid = document.getElementById('vipTiersGrid');
        if (!grid) return;
        let tiers;
        try {
            const data = await apiCall('/api/vip/tiers');
            tiers = data.tiers || [];
        } catch(e) { grid.innerHTML = '<p style="color:#888;">Unable to load tiers.</p>'; return; }

        const tierIcons = { silver: '🔰', gold: '🥇', platinum: '👑' };
        grid.innerHTML = tiers.map(t => {
            const c = VIP_COLORS[t.id] || VIP_COLORS.none;
            const isCurrent = currentStatus.active && currentStatus.tier === t.id;
            return `
                <div style="background:${c.bg};border:2px solid ${isCurrent ? c.border : 'rgba(255,255,255,0.08)'};border-radius:12px;padding:18px;position:relative;transition:border-color 0.2s;">
                    ${isCurrent ? `<div style="position:absolute;top:10px;right:10px;background:${c.border};color:#0a0a0f;font-size:9px;font-weight:bold;padding:2px 8px;border-radius:20px;">ACTIVE</div>` : ''}
                    <div style="font-size:28px;margin-bottom:8px;">${tierIcons[t.id] || '⭐'}</div>
                    <div style="font-weight:bold;font-size:16px;color:${c.text};margin-bottom:4px;">${t.label}</div>
                    <div style="font-size:22px;font-weight:bold;color:#ffd700;margin-bottom:10px;">$${t.price.toFixed(2)}<span style="font-size:11px;color:#888;">/mo</span></div>
                    <div style="font-size:12px;color:#aaa;margin-bottom:12px;line-height:1.6;">
                        ${t.perks.map(p => `✅ ${p}`).join('<br>')}
                        <br>🎟️ <strong style="color:#ffd700;">${t.daily_tickets} Arcade Tickets/day (skill games only)</strong>
                        <br>📈 <strong style="color:#4caf50;">+${t.boost_pct}% win boost</strong>
                    </div>
                    <div style="font-size:10px;color:#666;margin-bottom:12px;">Tickets are for skill-based mini-games — Aim Trainer, Reaction Time, Bomb Defuse, and more.</div>
                    <button class="btn btn-gold" style="width:100%;font-size:12px;" onclick="subscribeVIP('${t.id}')" ${isCurrent ? 'disabled' : ''}>
                        ${isCurrent ? '✅ Current Plan' : `Subscribe — $${t.price.toFixed(2)}/mo`}
                    </button>
                </div>`;
        }).join('');
    }

    async function _loadBatchCaseList() {
        const el = document.getElementById('batchCaseList');
        if (!el) return;
        try {
            const data = await apiCall('/api/cases');
            const top5 = (data.cases || [])
                .sort((a,b) => b.price - a.price)
                .slice(0, 5);
            el.innerHTML = `<span style="color:#888;">Cases included: </span>${top5.map(c => `<strong style="color:#ffd700;">${c.emoji||'📦'} ${c.name}</strong>`).join(' · ')}`;
        } catch(e) { el.textContent = ''; }
    }

    async function _loadTicketHistory() {
        const el = document.getElementById('ticketHistory');
        if (!el) return;
        try {
            const data = await apiCall('/api/tickets/history?limit=10');
            const rows = data.history || [];
            if (!rows.length) { el.innerHTML = '<span style="color:#555;">No ticket transactions yet.</span>'; return; }
            const sourceLabel = { subscription:'🎁 Subscription', purchase:'💳 Purchase', daily:'📅 Daily Award', spend_case:'🎰 Case Batch', spend_game:'🎮 High Roller Game' };
            el.innerHTML = `<div style="display:flex;flex-direction:column;gap:6px;">${rows.map(r => `
                <div style="display:flex;justify-content:space-between;padding:8px 12px;background:rgba(255,255,255,0.03);border-radius:6px;font-size:12px;">
                    <span>${sourceLabel[r.source] || r.source}</span>
                    <span style="color:${r.amount>0?'#4caf50':'#ff6b6b'};font-weight:bold;">${r.amount>0?'+':''}${r.amount} 🎟️</span>
                    <span style="color:#555;">${new Date(r.created_at).toLocaleDateString()}</span>
                </div>`).join('')}</div>`;
        } catch(e) { el.innerHTML = '<span style="color:#555;">Unable to load history.</span>'; }
    }

    async function subscribeVIP(tier) {
        try {
            showToast('🔄 Creating checkout...', 'info');
            const data = await apiCall('/api/vip/subscribe', {
                method: 'POST',
                body: JSON.stringify({ tier })
            });
            if (data.checkout_url) {
                window.location.href = data.checkout_url;
            } else {
                showToast('❌ Could not create checkout', 'error');
            }
        } catch(e) {
            showToast('❌ Payment setup failed: ' + (e.message || 'Unknown error'), 'error');
        }
    }

    async function cancelVIP() {
        if (!confirm('Cancel your VIP subscription? You keep access until the end of your current billing period.')) return;
        try {
            await apiCall('/api/vip/cancel', { method: 'POST' });
            showToast('✅ VIP cancelled. Access remains until expiry.', 'success');
            loadVIPStatus();
        } catch(e) { showToast('❌ Error cancelling VIP', 'error'); }
    }

    async function openPremiumBatchNew() {
        const btn = document.getElementById('batchOpenBtn');
        const result = document.getElementById('batchResult');
        if (!state.tickets || state.tickets < 1) {
            showToast('❌ You need at least 1 ticket', 'error');
            return;
        }
        btn.disabled = true;
        btn.textContent = '⏳ Opening...';
        result.innerHTML = '';
        try {
            const data = await apiCall('/api/vip/premium-batch-open', { method: 'POST' });
            if (data.success) {
                await loadTicketBalance();
                await loadStats();
                // Open the reel popup so spinner animations are visible
                const overlay = document.getElementById('popupOverlay');
                overlay.classList.add('show');
                state.popupItems = data.items;
                state.popupCaseId = 'premium_batch';
                showReelForItem(data.items[0], 0, data.items.length);
                result.innerHTML = `<div style="color:#4caf50;font-size:13px;text-align:center;margin-top:8px;">✅ Opened ${data.items.length} cases! Cost: $${data.total_cost.toFixed(2)} · Tickets remaining: ${state.tickets}</div>`;
            } else {
                result.innerHTML = `<div style="color:#ff6b6b;font-size:13px;text-align:center;margin-top:8px;">❌ ${data.error || 'Failed to open'}</div>`;
            }
        } catch(e) {
            result.innerHTML = `<div style="color:#ff6b6b;font-size:13px;text-align:center;margin-top:8px;">❌ ${e.message || 'Error opening cases'}</div>`;
        } finally {
            btn.disabled = false;
            btn.textContent = '🎰 Use 1 Ticket — Open 5 Premium Cases';
        }
    }

    // ─── Ticket Case ────────────────────────────────────────────
    async function openTicketCase() {
        const btn = document.getElementById('ticketCaseBtn');
        const result = document.getElementById('ticketCaseResult');
        if (!state.tickets || state.tickets < 1) {
            showToast('❌ You need at least 1 ticket', 'error');
            return;
        }
        btn.disabled = true;
        btn.textContent = '⏳ Opening...';
        result.innerHTML = '';
        try {
            const data = await apiCall('/api/vip/ticket-case-open', { method: 'POST' });
            if (data.success) {
                await loadTicketBalance();
                await loadStats();
                const overlay = document.getElementById('popupOverlay');
                overlay.classList.add('show');
                state.popupItems = [data.item];
                state.popupCaseId = 'ticket_case';
                showReelForItem(data.item, 0, 1);
                result.innerHTML = '';
            } else {
                result.innerHTML = `<div style="color:#ff6b6b;font-size:13px;text-align:center;margin-top:8px;">❌ ${data.error || 'Failed to open'}</div>`;
            }
        } catch(e) {
            result.innerHTML = `<div style="color:#ff6b6b;font-size:13px;text-align:center;margin-top:8px;">❌ ${e.message || 'Error opening case'}</div>`;
        } finally {
            btn.disabled = false;
            btn.textContent = '🎫 Use 1 Ticket — Open Ticket Case';
        }
    }

    // ─── Case Power-ups ─────────────────────────────────────────
    function toggleGuarantee() {
        if (!powerupGuarantee && (!state.tickets || state.tickets < 2)) {
            showToast('❌ Rarity Guarantee requires 2 tickets', 'error');
            return;
        }
        powerupGuarantee = !powerupGuarantee;
        updatePowerupUI();
    }

    function toggleInsurance() {
        if (!powerupInsurance && (!state.tickets || state.tickets < 1)) {
            showToast('❌ Unboxing Insurance requires 1 ticket', 'error');
            return;
        }
        powerupInsurance = !powerupInsurance;
        updatePowerupUI();
    }

    function updatePowerupUI() {
        const gToggle = document.getElementById('guaranteeToggle');
        const iToggle = document.getElementById('insuranceToggle');
        const gStatus = document.getElementById('guaranteeStatus');
        const iStatus = document.getElementById('insuranceStatus');
        if (gToggle && gStatus) {
            gToggle.style.borderColor   = powerupGuarantee ? '#ffd700' : 'rgba(255,255,255,0.06)';
            gToggle.style.background    = powerupGuarantee ? 'rgba(255,215,0,0.08)' : 'rgba(255,255,255,0.03)';
            gStatus.textContent         = powerupGuarantee ? '✓ ON' : 'OFF';
            gStatus.style.color         = powerupGuarantee ? '#ffd700' : '#555';
        }
        if (iToggle && iStatus) {
            iToggle.style.borderColor   = powerupInsurance ? '#4caf50' : 'rgba(255,255,255,0.06)';
            iToggle.style.background    = powerupInsurance ? 'rgba(76,175,80,0.08)' : 'rgba(255,255,255,0.03)';
            iStatus.textContent         = powerupInsurance ? '✓ ON' : 'OFF';
            iStatus.style.color         = powerupInsurance ? '#4caf50' : '#555';
        }
    }

    // Keep old function names as aliases so any other code that calls them doesn't break
    async function loadPremiumCases() { return loadVIPStatus(); }
    async function refreshPremium()   { return loadVIPStatus(); }

    // ─── Referral System ─────────────────────────────────────────
    async function loadReferralInfo() {
        const el = document.getElementById('referralContent');
        if (!el) return;
        try {
            const d = await apiCall('/api/referral/info');
            const alreadyReferred = d.already_referred;
            el.innerHTML = `
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;">
                    <div style="background:rgba(255,215,0,0.08);border:1px solid rgba(255,215,0,0.25);border-radius:8px;padding:10px 16px;font-family:monospace;font-size:18px;letter-spacing:3px;color:#ffd700;font-weight:700;">${d.code || '...'}</div>
                    <button class="btn btn-sm btn-primary" onclick="copyReferralCode('${d.code}')">📋 Copy Code</button>
                    <button class="btn btn-sm btn-outline" onclick="copyReferralLink('${d.code}')">🔗 Share Link</button>
                </div>
                <div style="display:flex;gap:20px;margin-bottom:14px;flex-wrap:wrap;">
                    <div style="text-align:center;"><div style="font-size:22px;font-weight:700;color:#ffd700;">${d.referral_count}</div><div style="font-size:11px;color:#888;">Referrals</div></div>
                    <div style="text-align:center;"><div style="font-size:22px;font-weight:700;color:#4caf50;">$${(d.total_earned_balance||0).toLocaleString()}</div><div style="font-size:11px;color:#888;">Earned</div></div>
                    <div style="text-align:center;"><div style="font-size:22px;font-weight:700;color:#9c27b0;">${d.total_earned_tickets||0} 🎫</div><div style="font-size:11px;color:#888;">Tickets Won</div></div>
                </div>
                ${alreadyReferred
                    ? `<div style="color:#4caf50;font-size:13px;">✅ You already used a referral code.</div>`
                    : `<div style="margin-top:6px;">
                        <p style="color:#888;font-size:12px;margin-bottom:8px;">Have a friend's code? Enter it below to claim your $500 bonus:</p>
                        <div style="display:flex;gap:8px;flex-wrap:wrap;">
                            <input id="referralCodeInput" type="text" placeholder="ENTER CODE" maxlength="7"
                                style="background:#111;border:1px solid rgba(255,215,0,0.3);border-radius:6px;padding:8px 12px;color:#fff;font-family:monospace;font-size:15px;letter-spacing:2px;text-transform:uppercase;width:140px;"
                                oninput="this.value=this.value.toUpperCase()">
                            <button class="btn btn-sm btn-gold" onclick="applyReferralCode()">Apply</button>
                        </div>
                        <div id="referralApplyResult" style="margin-top:8px;font-size:13px;"></div>
                       </div>`
                }`;
        } catch (e) {
            el.innerHTML = '<span style="color:#888;">Sign in to view your referral code.</span>';
        }
    }

    function copyReferralCode(code) {
        navigator.clipboard.writeText(code).then(() => showToast('✅ Code copied!')).catch(() => {
            const inp = document.createElement('input');
            inp.value = code;
            document.body.appendChild(inp);
            inp.select();
            document.execCommand('copy');
            document.body.removeChild(inp);
            showToast('✅ Code copied!');
        });
    }

    function copyReferralLink(code) {
        const base = window.location.origin;
        const url = base + '/?ref=' + encodeURIComponent(code);
        navigator.clipboard.writeText(url).then(() => showToast('🔗 Share link copied!')).catch(() => {
            const inp = document.createElement('input');
            inp.value = url;
            document.body.appendChild(inp);
            inp.select();
            document.execCommand('copy');
            document.body.removeChild(inp);
            showToast('🔗 Share link copied!');
        });
    }

    async function applyReferralCode() {
        const code = (document.getElementById('referralCodeInput')?.value || '').trim().toUpperCase();
        const resultEl = document.getElementById('referralApplyResult');
        if (!code) { if (resultEl) resultEl.innerHTML = '<span style="color:#f44;">Enter a code first.</span>'; return; }
        try {
            const d = await apiCall('/api/referral/apply', { method: 'POST', body: JSON.stringify({ code }) });
            if (resultEl) resultEl.innerHTML = `<span style="color:#4caf50;">✅ ${d.message}</span>`;
            await loadTicketBalance();
            setTimeout(loadReferralInfo, 800);
        } catch (e) {
            if (resultEl) resultEl.innerHTML = `<span style="color:#f44;">❌ ${e.message || 'Invalid code or already used.'}</span>`;
        }
    }


    // ============================================
    // SECTION 23: TRADE - FIXED: IDs as Strings (only one copy)
    // ============================================
    function loadTradeTab() {
        const container = document.getElementById('tradeContent');
        container.innerHTML = `
            <div class="card">
                <h3>🔄 Weapon Trade-Up</h3>
                <p style="color:#888;font-size:13px;margin-bottom:10px;">Select the required items of the same rarity to trade up</p>
                <div style="display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap;">
                    <button class="btn btn-blue btn-sm" onclick="Sound.click();loadTradeInventory('Blue')">🔵 Blue (10 → Purple)</button>
                    <button class="btn btn-purple btn-sm" onclick="Sound.click();loadTradeInventory('Purple')">🟪 Purple (10 → Pink)</button>
                    <button class="btn btn-danger btn-sm" onclick="Sound.click();loadTradeInventory('Pink')">💗 Pink (10 → Red)</button>
                    <button class="btn btn-danger btn-sm" style="background:linear-gradient(135deg,#c0392b,#e74c3c);" onclick="Sound.click();loadTradeInventory('Red')">🔴 Red (5 → Gold)</button>
                    <button class="btn btn-secondary btn-sm" onclick="Sound.click();autoSelectTradeItems()">🤖 Auto-Fill</button>
                </div>
                <div id="tradeInventory"></div>
                <div id="tradePreview" style="margin-top:15px;"></div>
                <button class="btn btn-primary" onclick="executeTrade()" id="tradeConfirmBtn" disabled>✅ Confirm Trade</button>
                <div id="tradeResult" style="margin-top:10px;"></div>
            </div>
        `;
        loadTradeInventory('Blue');
    }

async function loadTradeInventory(rarity) {
    tradeRarity = rarity;
    selectedTradeIds = [];
    document.getElementById('tradeConfirmBtn').disabled = true;
    
    const rarityMap = {
        'Blue':   { next: 'Purple',   count: 10 },
        'Purple': { next: 'Pink',     count: 10 },
        'Pink':   { next: 'Red',      count: 10 },
        'Red':    { next: 'Gold',     count: 5  },
    };
    const config = rarityMap[rarity];
    
    try {
        const data = await apiCall(`/api/user/me/inventory?limit=200&rarity=${rarity}`);
        tradeItems = data.items;
        const div = document.getElementById('tradeInventory');
        if (data.items.length === 0) {
            div.innerHTML = `<p style="color:#888;">No ${rarity} items found!</p>`;
            return;
        }
        div.innerHTML = `
            <div style="margin-bottom:10px;color:#888;font-size:12px;">
                Select ${config.count} ${rarity} items to trade up to ${config.next} — check condition and float before selecting to avoid trading away a good one
                ${rarity === 'Gold' ? ' (Gold tier progression)' : ''}
            </div>
            <div class="trade-grid" style="max-height:340px;overflow-y:auto;padding:2px;">
                ${data.items.map(buildTradeCard).join('')}
            </div>
            <div style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;">
                <button class="btn btn-sm btn-primary" onclick="Sound.click();autoSelectTradeItems()">🤖 Auto-Fill</button>
                <button class="btn btn-sm btn-danger" onclick="Sound.click();clearTradeSelection()">🗑️ Clear All</button>
                <span style="color:#888;font-size:12px;align-self:center;" id="tradeSelectionCount">Selected: 0 / ${config.count}</span>
            </div>
        `;
        updateTradeSelection();
    } catch (e) {
        console.error('Load trade inventory error:', e);
    }
}
    function buildTradeCard(item) {
        const cleanName = cleanItemName(item.item_name || '');
        const rar       = (item.rarity || 'Blue').trim();
        const rarColor  = SKIN_RARITY_COLORS[rar] || '#4488ff';
        const price     = typeof item.price === 'number' ? item.price : parseFloat(item.price || 0);
        const imgUrl    = item.image_url || `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;
        const stPrefix  = item.is_stattrak ? `<span style="color:#ff6b00;font-size:9px;">🔥 StatTrak™ </span><br>` : '';
        return `
            <div class="inv-card trade-card rarity-${rar}" data-item-id="${item.id}" onclick="toggleTradeCard(${item.id})">
                <div class="inv-rarity-stripe" style="background:${rarColor};"></div>
                <div class="trade-check"></div>
                <div class="inv-img-wrap">
                    <img src="${imgUrl}" alt="${esc(cleanName)}" onerror="this.src='/static/images/Default CS2 Weapons/weapon_ak47.png'">
                </div>
                <div class="inv-info">
                    <div class="inv-name" style="color:${rarColor};">${stPrefix}${esc(cleanName)}</div>
                    <div class="inv-sub">${condBadgeHtml(item.condition)}</div>
                    ${floatLineHtml(item.float_value)}
                    <div class="inv-price">$${price.toFixed(2)}</div>
                </div>
            </div>`;
    }

    function _syncTradeCardVisuals() {
        document.querySelectorAll('.trade-card').forEach(card => {
            const id = parseInt(card.dataset.itemId);
            const selected = selectedTradeIds.includes(id);
            card.classList.toggle('selected', selected);
            const check = card.querySelector('.trade-check');
            if (check) check.textContent = selected ? '✓' : '';
        });
    }

    function toggleTradeCard(itemId) {
        const idx = selectedTradeIds.indexOf(itemId);
        if (idx === -1) selectedTradeIds.push(itemId);
        else selectedTradeIds.splice(idx, 1);
        _syncTradeCardVisuals();
        updateTradeSelection();
    }

    function updateTradeSelection() {
    const rarityMap = { 'Blue': { count: 10 }, 'Purple': { count: 10 }, 'Pink': { count: 10 }, 'Red': { count: 5 } };
    const config = rarityMap[tradeRarity] || { count: 10 };
    const countEl = document.getElementById('tradeSelectionCount');
    if (countEl) countEl.textContent = `Selected: ${selectedTradeIds.length} / ${config.count}`;
    const btn = document.getElementById('tradeConfirmBtn');
    btn.disabled = selectedTradeIds.length !== config.count;
    if (selectedTradeIds.length === config.count) {
        const rarityMap2 = { 'Blue': { next: 'Purple', emoji: '🟪' }, 'Purple': { next: 'Pink', emoji: '💗' }, 'Pink': { next: 'Red', emoji: '🔴' }, 'Red': { next: 'Gold', emoji: '⭐' } };
        const config2 = rarityMap2[tradeRarity];
        document.getElementById('tradePreview').innerHTML = `
            <div style="padding:10px;background:rgba(255,215,0,0.05);border-radius:8px;border:1px solid rgba(255,215,0,0.1);">
                <div style="color:#ffd700;">Ready to trade!</div>
                <div style="color:#888;font-size:12px;">${selectedTradeIds.length} items → ${config2.emoji} ${config2.next}</div>
            </div>
        `;
    } else {
        document.getElementById('tradePreview').innerHTML = '';
    }
}

    function autoSelectTradeItems() {
        const rarityMap = { 'Blue': { count: 10 }, 'Purple': { count: 10 }, 'Pink': { count: 10 }, 'Red': { count: 5 } };
        const config = rarityMap[tradeRarity] || { count: 10 };
        selectedTradeIds = tradeItems.slice(0, config.count).map(item => item.id);
        _syncTradeCardVisuals();
        updateTradeSelection();
    }

    function clearTradeSelection() {
        selectedTradeIds = [];
        _syncTradeCardVisuals();
        updateTradeSelection();
    }

async function executeTrade() {
    Sound.click();
    const rarityMap = { 'Blue': { count: 10 }, 'Purple': { count: 10 }, 'Pink': { count: 10 }, 'Red': { count: 5 } };
    const config = rarityMap[tradeRarity];
    if (selectedTradeIds.length !== config.count) {
        showToast(`Please select exactly ${config.count} items!`);
        return;
    }
    try {
        const ids = selectedTradeIds.map(id => parseInt(id));
        const data = await apiCall('/api/quick-trade', {
            method: 'POST',
            body: JSON.stringify({ rarity: tradeRarity, item_ids: ids })
        });
        const resultDiv = document.getElementById('tradeResult');
        if (data.success) {
            const newRarity = data.new_item?.rarity || 'Blue';
            if (newRarity === 'Gold') { Sound.jackpot(); }
            else if (newRarity === 'Red') { Sound.bigWin(); }
            else { Sound.win(); }
            resultDiv.innerHTML = `
                <div style="padding:15px;background:rgba(76,175,80,0.1);border-radius:8px;border:1px solid #4caf50;">
                    <div style="color:#4caf50;font-size:18px;">✅ Trade Complete!</div>
                    <div style="margin-top:10px;">${data.message}</div>
                </div>
            `;
            
            // 🎉 CONFETTI: Trade success
            if (data.new_item) {
                if (newRarity === 'Gold' || newRarity === 'Red') {
                    spawnConfetti('Gold');
                    spawnCoinShower(30);
                } else if (newRarity === 'Pink' || newRarity === 'Purple') {
                    spawnConfetti(newRarity);
                    spawnCoinShower(15);
                }
                closePopup();
                setTimeout(() => showTradeResultPopup(data.new_item), 300);
            }
            
            await loadBalance();
            await loadInventory(state.currentPage);
            await loadStats();
            await loadQuests();
            loadTradeInventory(tradeRarity);
        } else {
            Sound.loss();
            resultDiv.innerHTML = `<div class="error">❌ ${esc(data.error || 'Trade failed')}</div>`;
            showToast(`❌ ${data.error || 'Trade failed'}`);
        }
    } catch (e) {
        Sound.error();
        document.getElementById('tradeResult').innerHTML = `<div class="error">❌ Error during trade-up</div>`;
        showToast('❌ Error during trade-up');
        console.error(e);
    }
}

    function showTradeResultPopup(item) {
        const color = RARITY_COLORS[item.rarity] || '#888';
        const glow = RARITY_GLOWS[item.rarity] || '0 0 60px rgba(255,255,255,0.1)';
        const emoji = RARITY_EMOJIS[item.rarity] || '🎯';
        const cleanName = (item.name || '').replace(/^[\u{1F300}-\u{1FFFF}\u{2600}-\u{27BF}\u{FE00}-\u{FEFF}🟦🟪🟥🟨🟩⬛⬜🟫🔥⭐💫👑✨\s]+/gu, '').trim();
        const displayLabel = item.display_name || cleanName || item.name;
        const imgUrl = item.image_url
            ? item.image_url
            : `/api/skin-image?name=${encodeURIComponent(cleanName)}&t=${Date.now()}`;
        const overlay = document.getElementById('popupOverlay');
        overlay.classList.add('show');
        document.getElementById('popupBody').innerHTML = `
            <div style="color:#4caf50;font-size:18px;margin-bottom:15px;">✅ Trade Complete!</div>
            <div style="display:flex;gap:8px;justify-content:center;margin:20px 0;padding:20px;background:rgba(0,0,0,0.3);border-radius:12px;border:1px solid rgba(255,255,255,0.05);min-height:120px;align-items:center;position:relative;overflow:visible;">
                ${[0,1,2,3,4].map(i => `
                    <div style="width:80px;height:80px;object-fit:contain;background:rgba(0,0,0,0.3);border-radius:8px;padding:8px;transition:all 0.1s;filter:drop-shadow(0 4px 20px rgba(0,0,0,0.5));display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:24px;position:relative;border:2px solid ${color};box-shadow:${glow};">
                        <div style="font-size:28px;line-height:1;">${emoji}</div>
                        <div style="font-size:8px;color:#888;margin-top:2px;text-align:center;max-width:60px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${i === 2 ? displayLabel : ALL_WEAPONS[Math.floor(Math.random() * ALL_WEAPONS.length)]}</div>
                    </div>
                `).join('')}
                <div style="position:absolute;width:100%;height:100%;top:0;left:0;pointer-events:none;border-radius:12px;background:radial-gradient(ellipse at center, ${color}22, transparent 70%);"></div>
            </div>
            <div style="background:radial-gradient(ellipse at center, ${color}33, transparent 70%);width:200px;height:200px;margin:0 auto 20px;border-radius:50%;display:flex;align-items:center;justify-content:center;position:relative;transition:all 0.5s;">
                <img src="${imgUrl}" alt="${displayLabel}" style="width:150px;height:150px;object-fit:contain;border-radius:12px;position:relative;z-index:2;filter:drop-shadow(0 8px 30px rgba(0,0,0,0.5));border:3px solid ${color};box-shadow:${glow};">
            </div>
            <div class="item-name rarity-${item.rarity}">${displayLabel}</div>
            <div class="item-details">💰 Value: $${item.price.toFixed(2)}</div>
            ${item.condition ? `<div class="item-details">${condBadgeHtml(item.condition) || item.condition}</div>` : ''}
            ${item.float !== undefined && item.float !== null ? `<div class="item-details" style="font-family:monospace;color:#888;">Float: ${Number(item.float).toFixed(4)}</div>` : ''}
            ${item.is_stattrak ? '<div class="item-details" style="color:#ff6b00;">🔥 StatTrak™</div>' : ''}
            <div class="popup-buttons">
                <button class="btn btn-success" onclick="closePopup()">✅ Done</button>
                <button class="btn btn-primary" onclick="closePopup(); switchTab('inventory');">📋 View Inventory</button>
            </div>
        `;
        if (item.rarity === 'Gold' || item.rarity === 'Red') {
            spawnConfetti(item.rarity);
            spawnParticles(item.rarity);
        }
    }


    // ============================================
    // SECTION 25: (Ticket Shop removed -- tickets are earned only, never
    // purchasable with real money; see Arcade Tickets card on the tab.)
    // ============================================

    // ============================================
    // SECTION 26: GAME POPUPS (unchanged)
    // ============================================
    function openGamePopup(gameType) {
        const overlay = document.getElementById('popupOverlay');
        overlay.classList.add('show');
        const html = {
            'coinflip': `
                <h3 style="color:#ffd700;">🪙 Coinflip vs Computer</h3>
                <p style="color:#888;font-size:13px;">50/50 chance to win! Play against the computer.</p>
                <div style="margin:15px 0;">
                    <label style="color:#888;font-size:11px;">Bet Amount ($)</label>
                    <input type="number" id="coinflipAmount" value="100" min="100" step="100" style="width:100%;padding:10px;border-radius:8px;background:rgba(20,20,40,0.8);color:white;border:1px solid rgba(255,215,0,0.1);font-family:'Orbitron',sans-serif;">
                </div>
                <button class="btn btn-primary" onclick="playCoinflip()" style="width:100%;">🪙 Flip Coin</button>
                <div id="coinflipResult" style="margin-top:15px;"></div>
            `,
            'dice': `
                <h3 style="color:#ffd700;">🎲 3D Dice</h3>
                <p style="color:#888;font-size:13px;">Roll the dice! Over/Under with realistic 3D dice.</p>
                <div style="margin:15px 0;">
                    <label style="color:#888;font-size:11px;">Bet Amount ($)</label>
                    <input type="number" id="diceAmount" value="100" min="100" step="100" style="width:100%;padding:10px;border-radius:8px;background:rgba(20,20,40,0.8);color:white;border:1px solid rgba(255,215,0,0.1);font-family:'Orbitron',sans-serif;">
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
                    <div>
                        <label style="color:#888;font-size:11px;">Bet Type</label>
                        <select id="diceBetType" style="width:100%;padding:10px;border-radius:8px;background:rgba(20,20,40,0.8);color:white;border:1px solid rgba(255,215,0,0.1);font-family:'Orbitron',sans-serif;">
                            <option value="over">Over</option>
                            <option value="under">Under</option>
                        </select>
                    </div>
                    <div>
                        <label style="color:#888;font-size:11px;">Bet Number (2-99)</label>
                        <input type="number" id="diceBetNumber" value="50" min="2" max="99" style="width:100%;padding:10px;border-radius:8px;background:rgba(20,20,40,0.8);color:white;border:1px solid rgba(255,215,0,0.1);font-family:'Orbitron',sans-serif;">
                    </div>
                </div>
                <button class="btn btn-primary" onclick="playDice()" style="margin-top:10px;width:100%;">🎲 Roll Dice</button>
                <div id="diceResult" style="margin-top:15px;"></div>
            `,
            'mines': `
                <h3 style="color:#ffd700;">💣 Mines</h3>
                <p style="color:#888;font-size:13px;">Reveal tiles without hitting mines. Cash out anytime!</p>
                <div style="margin:15px 0;">
                    <label style="color:#888;font-size:11px;">Bet Amount ($)</label>
                    <input type="number" id="minesAmount" value="100" min="100" step="100" style="width:100%;padding:10px;border-radius:8px;background:rgba(20,20,40,0.8);color:white;border:1px solid rgba(255,215,0,0.1);font-family:'Orbitron',sans-serif;">
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
                    <div>
                        <label style="color:#888;font-size:11px;">Grid Size</label>
                        <select id="minesGridSize" style="width:100%;padding:10px;border-radius:8px;background:rgba(20,20,40,0.8);color:white;border:1px solid rgba(255,215,0,0.1);font-family:'Orbitron',sans-serif;">
                            <option value="3">3x3</option>
                            <option value="4">4x4</option>
                            <option value="5" selected>5x5</option>
                            <option value="6">6x6</option>
                        </select>
                    </div>
                    <div>
                        <label style="color:#888;font-size:11px;">Mines</label>
                        <input type="number" id="minesCount" value="3" min="1" max="10" style="width:100%;padding:10px;border-radius:8px;background:rgba(20,20,40,0.8);color:white;border:1px solid rgba(255,215,0,0.1);font-family:'Orbitron',sans-serif;">
                    </div>
                </div>
                <button class="btn btn-primary" onclick="startMines()" style="margin-top:10px;width:100%;">💣 Start Mines</button>
                <div id="minesResult" style="margin-top:15px;"></div>
                <div id="minesBoard" class="mines-board"></div>
                <button class="btn btn-success" onclick="cashoutMines()" id="minesCashoutBtn" style="display:none;margin-top:10px;width:100%;">💰 Cash Out</button>
                <div class="mines-stats" id="minesStats"></div>
            `,
            'slots': `
                <h3 style="color:#ffd700;">🎰 Slots</h3>
                <p style="color:#888;font-size:13px;">Spin the reels and match symbols to win!</p>
                <div style="margin:15px 0;">
                    <label style="color:#888;font-size:11px;">Bet Amount ($)</label>
                    <input type="number" id="slotsAmount" value="50" min="50" step="50" style="width:100%;padding:10px;border-radius:8px;background:rgba(20,20,40,0.8);color:white;border:1px solid rgba(255,215,0,0.1);font-family:'Orbitron',sans-serif;">
                </div>
                <button class="btn btn-primary" onclick="playSlots()" style="width:100%;">🎰 Spin!</button>
                <div id="slotsResult" style="margin-top:15px;"></div>
                <div id="slotMachineGame" style="margin-top:15px;"></div>
            `
        };
        document.getElementById('popupBody').innerHTML = html[gameType] || '<p style="color:#888;">Game not found</p>';
    }

    // ============================================
    // SECTION 27: COINFLIP (unchanged)
    // ============================================
    async function playCoinflip() {
        const amount = parseFloat(document.getElementById('coinflipAmount').value);
        const resultDiv = document.getElementById('coinflipResult');
        if (!amount || amount < 100) {
            resultDiv.innerHTML = '<span class="error">❌ Minimum bet is $100</span>';
            return;
        }
        resultDiv.innerHTML = '<span class="loading">Flipping coin...</span>';
        try {
            const data = await apiCall('/api/games/coinflip/create', {
                method: 'POST',
                body: JSON.stringify({ amount })
            });
            if (data && data.success !== undefined && data.success !== false) {
                const result = data.user_wins ? '🎉 YOU WIN!' : '💀 COMPUTER WINS!';
                const color = data.user_wins ? '#4caf50' : '#ff4444';
                resultDiv.innerHTML = `
                    <div style="padding:15px;background:rgba(0,0,0,0.2);border-radius:8px;text-align:center;">
                        <div style="font-size:48px;margin:10px 0;">${data.user_wins ? '🪙' : '💻'}</div>
                        <div style="font-size:24px;font-weight:bold;color:${color};">${result}</div>
                        ${data.user_wins ? `<div style="color:#4caf50;font-size:18px;">Won $${data.win_amount}</div>` : `<div style="color:#ff4444;">Lost $${data.amount}</div>`}
                        <div style="color:#888;font-size:12px;margin-top:5px;">${data.message || ''}</div>
                    </div>
                `;
                if (data.user_wins) {
                    spawnConfetti('Gold');
                    spawnParticles('Gold');
                    spawnCoinShower(25);
                }
                await loadBalance();
                await loadQuests();
                updateGameHistory('coinflip', data.user_wins ? 'win' : 'lose', data.win_amount || 0);
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data?.error || 'Failed to flip coin')}</span>`;
            }
        } catch (e) {
            console.error('Coinflip error:', e);
                resultDiv.innerHTML = `<span class="error">❌ Error flipping coin: ${esc(e.message || 'Unknown error')}</span>`;
        }
    }

    // ============================================
    // SECTION 28: DICE (unchanged)
    // ============================================
    function createDiceFaces() {
        const positions = [1, 2, 3, 4, 5, 6];
        const dotsMap = {
            1: [5],
            2: [3, 7],
            3: [3, 5, 7],
            4: [1, 3, 7, 9],
            5: [1, 3, 5, 7, 9],
            6: [1, 3, 4, 6, 7, 9]
        };
        return positions.map(pos => {
            const dotPositions = dotsMap[pos] || [];
            const grid = [];
            for (let i = 1; i <= 9; i++) {
                grid.push(dotPositions.includes(i) ? '<div class="dot"></div>' : '<div class="dot empty"></div>');
            }
            return `
                <div class="dice-face dice-face-${pos}">
                    ${grid.join('')}
                </div>
            `;
        }).join('');
    }

    function showDiceRollAnimation() {
        const resultDiv = document.getElementById('diceResult');
        resultDiv.innerHTML = `
            <div class="dice-container">
                <div class="dice-wrapper rolling" id="diceWrapper1">
                    ${createDiceFaces()}
                </div>
                <div class="dice-wrapper rolling" id="diceWrapper2">
                    ${createDiceFaces()}
                </div>
            </div>
            <div style="color:#888;font-size:14px;margin-top:10px;">🎲 Rolling...</div>
        `;

        const wrapper1 = document.getElementById('diceWrapper1');
        const wrapper2 = document.getElementById('diceWrapper2');

        if (wrapper1) {
            wrapper1.style.transition = 'transform 0.1s';
            let rollInterval = setInterval(() => {
                if (!wrapper1) { clearInterval(rollInterval); return; }
                wrapper1.style.transform = `rotateX(${Math.random() * 720}deg) rotateY(${Math.random() * 720}deg)`;
            }, 100);
            setTimeout(() => clearInterval(rollInterval), 2000);
        }
        if (wrapper2) {
            wrapper2.style.transition = 'transform 0.1s';
            let rollInterval2 = setInterval(() => {
                if (!wrapper2) { clearInterval(rollInterval2); return; }
                wrapper2.style.transform = `rotateX(${Math.random() * 720}deg) rotateY(${Math.random() * 720}deg)`;
            }, 100);
            setTimeout(() => clearInterval(rollInterval2), 2000);
        }
    }

    function updateDiceDisplay(roll) {
        const wrapper1 = document.getElementById('diceWrapper1');
        const wrapper2 = document.getElementById('diceWrapper2');

        let die1, die2;
        if (roll <= 6) {
            die1 = roll;
            die2 = 1;
        } else if (roll <= 12) {
            die1 = Math.min(6, roll - 1);
            die2 = roll - die1;
            if (die2 > 6) { die2 = 6; die1 = roll - die2; }
        } else {
            die1 = 6;
            die2 = 6;
        }

        const rotations = {
            1: 'rotateX(0deg) rotateY(0deg)',
            2: 'rotateX(180deg) rotateY(0deg)',
            3: 'rotateY(-90deg) rotateX(0deg)',
            4: 'rotateY(90deg) rotateX(0deg)',
            5: 'rotateX(-90deg) rotateY(0deg)',
            6: 'rotateX(90deg) rotateY(0deg)'
        };

        if (wrapper1) {
            wrapper1.style.transition = 'transform 0.8s cubic-bezier(0.34, 1.56, 0.64, 1)';
            wrapper1.style.transform = rotations[die1] || 'rotateX(0deg) rotateY(0deg)';
        }
        if (wrapper2) {
            wrapper2.style.transition = 'transform 0.8s cubic-bezier(0.34, 1.56, 0.64, 1)';
            wrapper2.style.transform = rotations[die2] || 'rotateX(0deg) rotateY(0deg)';
        }
    }

    async function playDice() {
        const amount = parseFloat(document.getElementById('diceAmount').value);
        const betType = document.getElementById('diceBetType').value;
        const betNumber = parseInt(document.getElementById('diceBetNumber').value);
        const resultDiv = document.getElementById('diceResult');
        if (!amount || amount < 100) {
            resultDiv.innerHTML = '<span class="error">❌ Minimum bet is $100</span>';
            return;
        }
        if (betNumber < 2 || betNumber > 99) {
            resultDiv.innerHTML = '<span class="error">❌ Bet number must be between 2 and 99</span>';
            return;
        }
        resultDiv.innerHTML = '<span class="loading">Rolling dice...</span>';
        showDiceRollAnimation();
        try {
            const data = await apiCall('/api/games/dice/play', {
                method: 'POST',
                body: JSON.stringify({ amount, bet_type: betType, bet_number: betNumber })
            });
            if (data.success) {
                const result = data.win ? '🎉 WIN!' : '💀 LOST';
                const color = data.win ? '#4caf50' : '#ff4444';
                updateDiceDisplay(data.roll);
                resultDiv.innerHTML = `
                    <div style="padding:15px;background:rgba(0,0,0,0.2);border-radius:8px;text-align:center;">
                        <div style="font-size:24px;font-weight:bold;color:${color};">${result}</div>
                        <div style="font-size:16px;color:#ffd700;">Rolled: ${data.roll}</div>
                        <div style="color:#888;font-size:14px;">${data.bet_type} ${data.bet_number} (${data.multiplier}x)</div>
                        ${data.win ? `<div style="color:#4caf50;font-size:18px;">Won $${data.win_amount}</div>` : `<div style="color:#ff4444;">Lost $${data.amount}</div>`}
                    </div>
                `;
                if (data.win) {
                    spawnConfetti('Gold');
                    spawnParticles('Gold');
                    spawnCoinShower(20);
                }
                await loadBalance();
                await loadQuests();
                updateGameHistory('dice', data.win ? 'win' : 'lose', data.win ? data.win_amount : 0);
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Failed to roll dice')}</span>`;
            }
        } catch (e) {
                resultDiv.innerHTML = `<span class="error">❌ Error rolling dice: ${esc(e.message || 'Unknown error')}</span>`;
        }
    }

    // ============================================
    // SECTION 29: MINES - FIXED
    // ============================================

    async function startMines() {
        const amount = parseFloat(document.getElementById('minesAmount').value);
        const gridSize = parseInt(document.getElementById('minesGridSize').value);
        const mineCount = parseInt(document.getElementById('minesCount').value);
        const resultDiv = document.getElementById('minesResult');
        const boardDiv = document.getElementById('minesBoard');
        const cashoutBtn = document.getElementById('minesCashoutBtn');
        const statsDiv = document.getElementById('minesStats');

        if (!amount || amount < 100) {
            resultDiv.innerHTML = '<span class="error">❌ Minimum bet is $100</span>';
            return;
        }
        const maxMines = gridSize * gridSize - 2;
        if (mineCount < 1 || mineCount > maxMines) {
            resultDiv.innerHTML = `<span class="error">❌ Max mines for ${gridSize}x${gridSize} is ${maxMines}</span>`;
            return;
        }

        resultDiv.innerHTML = '<span class="loading">Starting game...</span>';
        boardDiv.innerHTML = '';
        cashoutBtn.style.display = 'none';
        statsDiv.innerHTML = '';

        try {
            const data = await apiCall('/api/games/mines/start', {
                method: 'POST',
                body: JSON.stringify({ amount, grid_size: gridSize, mine_count: mineCount })
            });
            if (data.success) {
                state.currentMinesGame = data;
                resultDiv.innerHTML = `<span class="success">✅ Game started! Bet: $${data.bet_amount}</span>`;
                renderMinesBoard(data);
                cashoutBtn.style.display = 'block';
                cashoutBtn.textContent = `💰 Cash Out ($${data.bet_amount})`;
                statsDiv.innerHTML = `
                    <div class="mines-stat"><span class="label">💣 Mines:</span> <span class="value">${data.mine_count}</span></div>
                    <div class="mines-stat"><span class="label">✅ Safe:</span> <span class="value">${data.remaining}</span></div>
                    <div class="mines-stat"><span class="label">📊 Multiplier:</span> <span class="value">1.0x</span></div>
                `;
                await loadBalance();
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Failed to start game')}</span>`;
            }
        } catch (e) {
                resultDiv.innerHTML = `<span class="error">❌ Error starting game: ${esc(e.message || 'Unknown error')}</span>`;
        }
    }

    function renderMinesBoard(game) {
        const boardDiv = document.getElementById('minesBoard');
        const size = game.grid_size;
        boardDiv.style.gridTemplateColumns = `repeat(${size}, 1fr)`;
        boardDiv.innerHTML = '';
        const revealed = game.revealed_tiles || [];
        const total = size * size;
        for (let i = 0; i < total; i++) {
            const tile = document.createElement('div');
            tile.className = `mines-tile${revealed.includes(i) ? ' revealed' : ''}`;
            tile.dataset.index = i;
            tile.onclick = () => revealMinesTile(i);
            boardDiv.appendChild(tile);
        }
    }

    async function revealMinesTile(tileIndex) {
        if (!state.currentMinesGame) return;
        const resultDiv = document.getElementById('minesResult');
        const cashoutBtn = document.getElementById('minesCashoutBtn');
        const statsDiv = document.getElementById('minesStats');

        resultDiv.innerHTML = '<span class="loading">Revealing...</span>';

        try {
            const data = await apiCall('/api/games/mines/reveal', {
                method: 'POST',
                body: JSON.stringify({ game_id: state.currentMinesGame.game_id, tile_index: tileIndex })
            });
            if (data.success) {
                state.currentMinesGame.revealed_tiles = data.revealed;
                state.currentMinesGame.multiplier = data.multiplier;
                renderMinesBoard(state.currentMinesGame);

                if (data.game_won) {
                    resultDiv.innerHTML = `<span class="success">🎉 YOU WON $${data.win_amount}! (${data.multiplier}x)</span>`;
                    cashoutBtn.style.display = 'none';
                    statsDiv.innerHTML = '';
                    spawnConfetti('Gold');
                    spawnParticles('Gold');
                    spawnCoinShower(30);
                    await loadBalance();
                    await loadQuests();
                    updateGameHistory('mines', 'win', data.win_amount);
                    state.currentMinesGame = null;
                } else {
                    resultDiv.innerHTML = `
                        <span class="success">✅ Safe! ${data.remaining} tiles remaining</span>
                        <br><span style="color:#ffd700;">Multiplier: ${data.multiplier}x</span>
                        <br><span style="color:#888;">Cash out: $${data.cash_out_amount}</span>
                    `;
                    cashoutBtn.textContent = `💰 Cash Out ($${data.cash_out_amount})`;
                    if (statsDiv) {
                        statsDiv.innerHTML = `
                            <div class="mines-stat"><span class="label">💣 Mines:</span> <span class="value">${state.currentMinesGame.mine_count}</span></div>
                            <div class="mines-stat"><span class="label">✅ Safe:</span> <span class="value">${data.remaining}</span></div>
                            <div class="mines-stat"><span class="label">📊 Multiplier:</span> <span class="value">${data.multiplier}x</span></div>
                        `;
                    }
                }
            } else if (data.exploded) {
                const tiles = document.querySelectorAll('.mines-tile');
                if (tiles[tileIndex]) {
                    tiles[tileIndex].classList.add('mine-hit');
                    tiles[tileIndex].textContent = '';
                    const minePositions = state.currentMinesGame.mine_positions || [];
                    tiles.forEach((t, i) => {
                        if (minePositions.includes(i) && i !== tileIndex) {
                            t.textContent = '💣';
                            t.style.borderColor = '#ff4444';
                            t.style.background = 'rgba(255,68,68,0.1)';
                        }
                    });
                    spawnConfetti('Red');
                    spawnParticles('Red');
                }
                resultDiv.innerHTML = `<span class="error">💥 BOOM! You hit a mine! Game over.</span>`;
                cashoutBtn.style.display = 'none';
                statsDiv.innerHTML = '';
                state.currentMinesGame = null;
                await loadBalance();
                await loadQuests();
                updateGameHistory('mines', 'lose', 0);
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Failed to reveal tile')}</span>`;
            }
        } catch (e) {
                resultDiv.innerHTML = `<span class="error">❌ Error revealing tile: ${esc(e.message || 'Unknown error')}</span>`;
        }
    }

    async function cashoutMines() {
        if (!state.currentMinesGame) return;
        const resultDiv = document.getElementById('minesResult');
        const cashoutBtn = document.getElementById('minesCashoutBtn');
        const statsDiv = document.getElementById('minesStats');

        resultDiv.innerHTML = '<span class="loading">Cashing out...</span>';

        try {
            const data = await apiCall('/api/games/mines/cashout', {
                method: 'POST',
                body: JSON.stringify({ game_id: state.currentMinesGame.game_id })
            });
            if (data.success) {
                resultDiv.innerHTML = `<span class="success">💰 Cashed out $${data.win_amount}! (${data.multiplier}x)</span>`;
                cashoutBtn.style.display = 'none';
                statsDiv.innerHTML = '';
                spawnConfetti('Gold');
                spawnCoinShower(15);
                state.currentMinesGame = null;
                await loadBalance();
                await loadQuests();
                updateGameHistory('mines', 'win', data.win_amount);
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Failed to cash out')}</span>`;
            }
        } catch (e) {
                resultDiv.innerHTML = `<span class="error">❌ Error cashing out: ${esc(e.message || 'Unknown error')}</span>`;
        }
    }

    // ============================================
    // SECTION 30: SLOTS
    // ============================================

    async function playSlots() {
        const amount = parseFloat(document.getElementById('slotsAmount').value);
        const resultDiv = document.getElementById('slotsResult');
        const gameDiv = document.getElementById('slotMachineGame');

        if (!amount || amount < 50) {
            resultDiv.innerHTML = '<span class="error">❌ Minimum bet is $50</span>';
            return;
        }

        resultDiv.innerHTML = '<span class="loading">Spinning...</span>';
        gameDiv.innerHTML = '';

        try {
            const data = await apiCall('/api/games/slots/play', {
                method: 'POST',
                body: JSON.stringify({ amount })
            });
            if (data.success) {
                renderSlotMachine(data.symbols, data.win_amount, data.multiplier, data.bet_amount);
                const result = data.win_amount > 0 ? '🎉 WIN!' : '💀 LOST';
                const color = data.win_amount > 0 ? '#4caf50' : '#ff4444';
                resultDiv.innerHTML = `
                    <div style="padding:15px;background:rgba(0,0,0,0.2);border-radius:8px;text-align:center;">
                        <div style="font-size:24px;font-weight:bold;color:${color};">${result}</div>
                        ${data.win_amount > 0 ? `<div style="color:#4caf50;font-size:18px;">Won $${data.win_amount} (${data.multiplier}x)</div>` : `<div style="color:#ff4444;">Lost $${data.bet_amount}</div>`}
                    </div>
                `;
                if (data.win_amount > 50) {
                    spawnConfetti('Gold');
                    spawnParticles('Gold');
                    spawnCoinShower(25);
                }
                if (data.win_amount > 200) {
                    spawnConfetti('Gold');
                    spawnConfetti('Gold');
                    spawnCoinShower(50);
                }
                await loadBalance();
                await loadQuests();
                updateGameHistory('slots', data.win_amount > 0 ? 'win' : 'lose', data.win_amount);
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Failed to spin slots')}</span>`;
            }
        } catch (e) {
                resultDiv.innerHTML = `<span class="error">❌ Error spinning slots: ${esc(e.message || 'Unknown error')}</span>`;
        }
    }

    function renderSlotMachine(symbols, winAmount, multiplier, betAmount) {
        const gameDiv = document.getElementById('slotMachineGame');
        const isWin = winAmount > 0;

        const allSymbols = ['🍒', '🍋', '🍊', '🍇', '💎', '7️⃣', '🎰'];
        const reelData = [];
        for (let i = 0; i < 3; i++) {
            const reel = [];
            for (let j = 0; j < 7; j++) {
                reel.push(allSymbols[Math.floor(Math.random() * allSymbols.length)]);
            }
            reel[3] = symbols[i];
            reelData.push(reel);
        }

        gameDiv.innerHTML = `
            <div class="slot-machine-container">
                <div class="slot-reels">
                    ${reelData.map((reel, idx) => `
                        <div class="slot-reel ${isWin ? 'winning-reel' : ''}">
                            <div class="reel-strip spinning" id="reelStrip${idx}">
                                ${reel.map(sym => `
                                    <div class="reel-symbol">${sym}</div>
                                `).join('')}
                            </div>
                            <div class="slot-payline ${isWin ? 'highlight' : ''}"></div>
                        </div>
                    `).join('')}
                </div>
                ${isWin ? `
                    <div style="text-align:center;margin-top:15px;font-size:18px;color:#ffd700;animation:winPulse 0.5s ease 3;">
                        🎰 WINNER! $${winAmount} (${multiplier}x)
                    </div>
                ` : `
                    <div style="text-align:center;margin-top:15px;font-size:14px;color:#888;">
                        Better luck next time!
                    </div>
                `}
            </div>
        `;

        const reels = document.querySelectorAll('.reel-strip');
        reels.forEach((reel, idx) => {
            const delay = 500 + (idx * 300);
            setTimeout(() => {
                reel.classList.remove('spinning');
                const symbolHeight = 50;
                const offset = -symbolHeight * 3;
                reel.style.transform = `translateY(${offset}px)`;
                reel.style.transition = 'transform 0.8s cubic-bezier(0.34, 1.56, 0.64, 1)';
            }, delay);
        });
    }

    // ============================================
    // SECTION 31: HOURLY & WEEKLY CLAIMS
    // ============================================

    async function claimHourly() {
        const resultDiv = document.getElementById('claimResult');
        resultDiv.innerHTML = '<span class="loading">Claiming...</span>';
        try {
            const data = await apiCall('/api/games/hourly', { method: 'POST', body: JSON.stringify({}) });
            if (data.success) {
                resultDiv.innerHTML = `<span class="success">🕐 Claimed $${data.reward}! (${data.total_claimed} total claims)</span>`;
                if (data.total_claimed % 10 === 0) {
                    spawnConfetti('Gold');
                    spawnCoinShower(20);
                }
                await loadBalance();
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Failed to claim hourly')}</span>`;
            }
        } catch (e) {
            resultDiv.innerHTML = '<span class="error">❌ Error claiming hourly</span>';
        }
    }

    async function claimWeekly() {
        const resultDiv = document.getElementById('claimResult');
        resultDiv.innerHTML = '<span class="loading">Claiming...</span>';
        try {
            const data = await apiCall('/api/games/weekly', { method: 'POST', body: JSON.stringify({}) });
            if (data.success) {
                resultDiv.innerHTML = `<span class="success">📅 Claimed $${data.reward}! (${data.total_claimed} total claims)</span>`;
                spawnConfetti('Gold');
                spawnCoinShower(30);
                await loadBalance();
            } else {
                resultDiv.innerHTML = `<span class="error">❌ ${esc(data.error || 'Failed to claim weekly')}</span>`;
            }
        } catch (e) {
            resultDiv.innerHTML = '<span class="error">❌ Error claiming weekly</span>';
        }
    }

    // ============================================
    // SECTION 32: PROFILE
    // ============================================

    async function loadProfile() {
        try {
            const [data, meData] = await Promise.all([
                apiCall('/api/user/me/profile'),
                apiCall('/api/user/me'),
            ]);
            const oldLevel = parseInt(document.getElementById('profileLevel').textContent) || 1;
            const newLevel = data.level;

            document.getElementById('profileLevel').textContent = data.level;
            document.getElementById('profilePrestige').textContent = data.prestige;
            document.getElementById('profileXP').textContent = data.xp;
            document.getElementById('profileXPNeeded').textContent = data.xp_needed;
            document.getElementById('profileProgressBar').style.width = Math.min(100, data.xp_progress) + '%';

            if (newLevel > oldLevel) {
                spawnConfetti('Gold', 120);
                spawnCoinShower(30);
                showToast(`🎉 LEVEL UP! You are now level ${newLevel}! 🎉`, 'success');
                if (data.prestige > 0) {
                    setTimeout(() => { spawnConfettiExplosion(); showToast(`🌟 PRESTIGE ${data.prestige}! 🌟`, 'success'); }, 500);
                }
            }

            // Avatar: use server-resolved URL (works for both Discord and Google)
            const avatarUrl = meData.avatar_url || document.getElementById('userAvatar').src || 'https://cdn.discordapp.com/embed/avatars/0.png';
            document.getElementById('profileAvatar').src = avatarUrl;
            document.getElementById('profileName').textContent = meData.username || document.getElementById('userName').textContent;

            const provider = meData.primary_provider || 'discord';
            const googleLinked = meData.google_linked || false;
            const discordLinked = meData.discord_linked !== false;

            document.getElementById('profileBadge').textContent = provider === 'google' ? '🔗 Google (primary)' : '🔗 Discord (primary)';

            // Linked accounts section
            const linkedEl = document.getElementById('profileLinkedAccounts');
            if (linkedEl) {
                linkedEl.innerHTML = `
                    <div style="margin-top:16px;border-top:1px solid rgba(255,215,0,0.1);padding-top:14px;">
                        <div style="color:#888;font-size:11px;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px;">Linked Accounts</div>
                        <div style="display:flex;flex-direction:column;gap:8px;">
                            <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(88,101,242,0.1);border:1px solid rgba(88,101,242,0.3);border-radius:8px;padding:10px 14px;">
                                <div style="display:flex;align-items:center;gap:10px;">
                                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 127.14 96.36" width="20" height="16"><path fill="#5865f2" d="M107.7,8.07A105.15,105.15,0,0,0,81.47,0a72.06,72.06,0,0,0-3.36,6.83A97.68,97.68,0,0,0,49,6.83,72.37,72.37,0,0,0,45.64,0,105.89,105.89,0,0,0,19.39,8.09C2.79,32.65-1.71,56.6.54,80.21h0A105.73,105.73,0,0,0,32.71,96.36,77.7,77.7,0,0,0,39.6,85.25a68.42,68.42,0,0,1-10.85-5.18c.91-.66,1.8-1.34,2.66-2.05a75.57,75.57,0,0,0,58.74,0c.86.71,1.75,1.39,2.66,2.05a68.42,68.42,0,0,1-10.85,5.18,77.7,77.7,0,0,0,6.89,11.1A105.73,105.73,0,0,0,126.6,80.22h0C129.24,52.84,122.09,28.11,107.7,8.07Z"/></svg>
                                    <span style="font-size:13px;font-weight:600;">Discord</span>
                                    ${provider === 'discord' ? '<span style="font-size:10px;color:#ffd700;background:rgba(255,215,0,0.1);padding:2px 6px;border-radius:4px;">PRIMARY</span>' : ''}
                                </div>
                                ${discordLinked
                                    ? '<span style="color:#4caf50;font-size:12px;">✅ Linked</span>'
                                    : `<a href="/auth/discord/link" class="btn btn-sm" style="background:#5865f2;color:#fff;font-size:11px;padding:4px 10px;border-radius:6px;text-decoration:none;">Link Discord</a>`
                                }
                            </div>
                            <div style="display:flex;align-items:center;justify-content:space-between;background:rgba(66,133,244,0.08);border:1px solid rgba(66,133,244,0.25);border-radius:8px;padding:10px 14px;">
                                <div style="display:flex;align-items:center;gap:10px;">
                                    <svg viewBox="0 0 48 48" width="20" height="20"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>
                                    <div>
                                        <span style="font-size:13px;font-weight:600;">Google</span>
                                        ${meData.google_email ? `<div style="font-size:10px;color:#888;">${meData.google_email}</div>` : ''}
                                    </div>
                                    ${provider === 'google' ? '<span style="font-size:10px;color:#ffd700;background:rgba(255,215,0,0.1);padding:2px 6px;border-radius:4px;">PRIMARY</span>' : ''}
                                </div>
                                ${googleLinked
                                    ? '<span style="color:#4caf50;font-size:12px;">✅ Linked</span>'
                                    : `<a href="/auth/google" class="btn btn-sm" style="background:#4285f4;color:#fff;font-size:11px;padding:4px 10px;border-radius:6px;text-decoration:none;">Link Google</a>`
                                }
                            </div>
                        </div>
                    </div>`;
            }
            // Load inventory value history chart
            loadValueHistory();
        } catch (e) { console.error('Load profile error:', e); }
    }

    async function loadValueHistory() {
        try {
            const rows = await apiCall('/api/profile/value-history');
            const card = document.getElementById('valueHistoryCard');
            if (!card) return;
            if (!rows || rows.length < 2) {
                // Show card but indicate data accumulates over time
                card.style.display = '';
                const wrap = document.getElementById('valueChartWrap');
                if (wrap) wrap.innerHTML = '<div style="color:#555;font-size:12px;padding:20px 0;text-align:center;">Data accumulates daily. Check back tomorrow!</div>';
                return;
            }
            card.style.display = '';
            const values = rows.map(r => parseFloat(r.value) || 0);
            const minV = Math.min(...values);
            const maxV = Math.max(...values);
            const range = maxV - minV || 1;
            const W = 400, H = 80, pad = 4;

            const pts = values.map((v, i) => {
                const x = pad + (i / (values.length - 1)) * (W - pad * 2);
                const y = H - pad - ((v - minV) / range) * (H - pad * 2);
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            });

            const svg = document.getElementById('valueHistoryChart');
            if (!svg) return;
            const latestV = values[values.length - 1];
            const prevV   = values[values.length - 2];
            const lineColor = latestV >= prevV ? '#22c55e' : '#ef4444';
            const fillColor = latestV >= prevV ? 'rgba(34,197,94,0.08)' : 'rgba(239,68,68,0.08)';
            const firstPt = pts[0].split(',');
            const lastPt  = pts[pts.length - 1].split(',');

            svg.innerHTML = `
                <defs>
                    <linearGradient id="chartGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stop-color="${lineColor}" stop-opacity="0.3"/>
                        <stop offset="100%" stop-color="${lineColor}" stop-opacity="0"/>
                    </linearGradient>
                </defs>
                <path d="M${pts.join('L')}L${lastPt[0]},${H - pad}L${firstPt[0]},${H - pad}Z" fill="url(#chartGrad)"/>
                <polyline points="${pts.join(' ')}" fill="none" stroke="${lineColor}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
                <circle cx="${lastPt[0]}" cy="${lastPt[1]}" r="3" fill="${lineColor}"/>
            `;
            const minEl = document.getElementById('valueChartMin');
            const maxEl = document.getElementById('valueChartMax');
            const curEl = document.getElementById('valueChartCurrent');
            if (minEl) minEl.textContent = '$' + minV.toFixed(2);
            if (maxEl) maxEl.textContent = '$' + maxV.toFixed(2);
            if (curEl) curEl.textContent = '$' + latestV.toFixed(2);
        } catch (e) { /* ignore */ }
    }

    // ============================================
    // SECTION 33: GAME STATS & HISTORY
    // ============================================

    async function loadGameStats() {
        const grid = document.getElementById('gameStats');
        if (!grid) return;
        try {
            const data = await apiCall('/api/games/stats');
            grid.innerHTML = `
                <div class="stat-box"><div class="value">${data.coinflip.wins}/${data.coinflip.losses}</div><div class="label">🪙 Coinflip W/L</div></div>
                <div class="stat-box"><div class="value">${data.dice.wins}/${data.dice.losses}</div><div class="label">🎲 Dice W/L</div></div>
                <div class="stat-box"><div class="value">${data.mines.wins}/${data.mines.losses}</div><div class="label">💣 Mines W/L</div></div>
                <div class="stat-box"><div class="value">${data.slots.wins}/${data.slots.losses}</div><div class="label">🎰 Slots W/L</div></div>
            `;
        } catch (e) {
            const counts = { coinflip: {wins:0,losses:0}, dice: {wins:0,losses:0}, mines: {wins:0,losses:0}, slots: {wins:0,losses:0} };
            state.gameHistory.forEach(h => {
                if (counts[h.game]) {
                    if (h.result === 'win') counts[h.game].wins++;
                    else counts[h.game].losses++;
                }
            });
            grid.innerHTML = `
                <div class="stat-box"><div class="value">${counts.coinflip.wins}/${counts.coinflip.losses}</div><div class="label">🪙 Coinflip W/L</div></div>
                <div class="stat-box"><div class="value">${counts.dice.wins}/${counts.dice.losses}</div><div class="label">🎲 Dice W/L</div></div>
                <div class="stat-box"><div class="value">${counts.mines.wins}/${counts.mines.losses}</div><div class="label">💣 Mines W/L</div></div>
                <div class="stat-box"><div class="value">${counts.slots.wins}/${counts.slots.losses}</div><div class="label">🎰 Slots W/L</div></div>
            `;
        }
    }

    function updateGameHistory(game, result, amount) {
        state.gameHistory.unshift({ game, result, amount, time: new Date().toLocaleTimeString() });
        if (state.gameHistory.length > 10) state.gameHistory.pop();
        const historyDiv = document.querySelector('.game-history');
        if (historyDiv) renderGameHistory(historyDiv);
    }

    function renderGameHistory(container) {
        if (state.gameHistory.length === 0) {
            container.innerHTML = '<div style="color:#888;font-size:11px;text-align:center;">No games played yet</div>';
            return;
        }
        container.innerHTML = state.gameHistory.map(h => `
            <div class="history-item ${h.result}">
                <span>${h.game === 'coinflip' ? '🪙' : h.game === 'dice' ? '🎲' : h.game === 'mines' ? '💣' : '🎰'} ${h.game}</span>
                <span>${h.result === 'win' ? '✅ +' : '❌ -'}$${h.amount}</span>
                <span style="color:#666;">${h.time}</span>
            </div>
        `).join('');
    }


// ─── Load battle history ────────────────────────────────────
async function loadBattleHistory() {
    try {
        const data = await apiCall('/api/battles/history');
        const list = document.getElementById('battleHistoryList');
        if (!data || data.length === 0) {
            list.innerHTML = '<span class="text-muted">No battles yet.</span>';
            return;
        }
        list.innerHTML = data.map(b => `
            <div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:11px;display:flex;justify-content:space-between;">
                <span>${b.battle_type === 'pvp' ? '👥' : '🤖'} $${b.entry_fee} | ${b.win_condition}</span>
                <span>${b.winner_id == state.userId ? '🏆 Win' : '💀 Loss'} | ${new Date(b.ended_at).toLocaleDateString()}</span>
            </div>
        `).join('');
    } catch (e) {
        console.error('Load battle history error:', e);
    }
}

// ─── Load fee options ──────────────────────────────────────
async function loadBattleFeeOptions() {
    try {
        const data = await apiCall('/api/battles/settings');
        const select = document.getElementById('battleFee');
        select.innerHTML = '';
        (data.fee_tiers || [1000]).forEach(fee => {
            const opt = document.createElement('option');
            opt.value = fee;
            opt.textContent = `$${fee}`;
            select.appendChild(opt);
        });
    } catch (e) {
        console.error('Load fee options error:', e);
    }
}

// ─── Mode toggle (show/hide difficulty) ──────────────────
const modeToggle = document.getElementById('battleMode');
if (modeToggle) {
    modeToggle.addEventListener('change', function() {
        const diffDiv = document.getElementById('battleDifficultyDiv');
        if (diffDiv) {
            diffDiv.style.display = this.value === 'pve' ? 'block' : 'none';
        }
    });
}

// ─── Join queue (PvP & PvE) ──────────────────────────────
async function joinBattleQueue() {
    const mode = document.getElementById('battleMode').value;
    const fee = parseFloat(document.getElementById('battleFee').value);
    const rounds = parseInt(document.getElementById('battleRounds').value);
    const winCondition = document.getElementById('battleWinCondition').value;
    const difficulty = document.getElementById('battleDifficulty').value;
    const selectedCase = document.getElementById('battleCaseSelect').value;
    const statusDiv = document.getElementById('battleStatus');

    if (!selectedCase) {
        statusDiv.innerHTML = '<span class="error">❌ Please select a case to battle with.</span>';
        return;
    }

    statusDiv.innerHTML = '<span class="loading">⏳ Connecting...</span>';

    try {
        if (mode === 'pvp') {
            // ── 1. Close any existing matchmaking WS ──
            if (matchmakingWS) {
                try { matchmakingWS.close(); } catch(e) {}
                matchmakingWS = null;
            }

            // ── 2. Open WebSocket to matchmaking listener ──
            const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/battles/matchmaking`;
            matchmakingWS = new WebSocket(wsUrl);

            matchmakingWS.onopen = () => {
                statusDiv.innerHTML = '<span class="success">✅ Connected! Queuing for PvP...</span>';
                // Send the queue request now that WS is open
                apiCall('/api/battles/queue', {
                    method: 'POST',
                    body: JSON.stringify({ fee, rounds, win_condition: winCondition })
                }).then(data => {
                    if (data.success) {
                        statusDiv.innerHTML = `<span class="success">✅ Queued! Waiting for opponent...</span>`;
                    } else {
                        statusDiv.innerHTML = `<span class="error">❌ ${esc(data.error)}</span>`;
                        matchmakingWS.close();
                    }
                }).catch(err => {
                    statusDiv.innerHTML = `<span class="error">❌ Queue error: ${esc(err.message)}</span>`;
                    matchmakingWS.close();
                });
            };

            matchmakingWS.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.type === 'match_found') {
                    statusDiv.innerHTML = `<span class="success">✅ Match found! Redirecting...</span>`;
                    matchmakingWS.close();
                    window.location.href = `/battle?id=${data.battle_id}&fee=${fee}&rounds=${rounds}&win=${winCondition}&case=${selectedCase}`;
                }
            };

            matchmakingWS.onerror = () => {
                statusDiv.innerHTML = '<span class="error">❌ WebSocket error. Please try again.</span>';
            };

            matchmakingWS.onclose = () => {
                if (statusDiv.innerHTML.includes('Queued')) {
                    statusDiv.innerHTML = '<span class="error">❌ Disconnected from matchmaking. Please try again.</span>';
                }
            };

        } else {
            // ── PvE: Direct redirect ──
            const data = await apiCall('/api/battles/pve/start', {
                method: 'POST',
                body: JSON.stringify({ fee, rounds, win_condition: winCondition, difficulty })
            });
            if (data.success) {
                statusDiv.innerHTML = `<span class="success">✅ Battle started! Redirecting...</span>`;
                window.location.href = `/battle?id=${data.battle_id}&fee=${fee}&rounds=${rounds}&win=${winCondition}&diff=${difficulty}&case=${selectedCase}`;
            } else {
                statusDiv.innerHTML = `<span class="error">❌ ${esc(data.error)}</span>`;
            }
        }
    } catch (e) {
        statusDiv.innerHTML = `<span class="error">❌ Error: ${esc(e.message)}</span>`;
    }
}

// ─── Override switchTab to load battle data ──────────────
const originalSwitchTab = window.switchTab;
window.switchTab = function(tab) {
    originalSwitchTab(tab);
    if (tab === 'battles') {
        loadBattleHistory();
        loadBattleFeeOptions();
    }
};
    // ============================================
    // SECTION 34: GOAL DATA
    // ============================================

    async function loadGoalData() {
        try {
            const data = await apiCall('/api/goals');
            const donationPercent = Math.min(100, (data.donations / 500) * 100);
            const elDon = document.getElementById('goalDonations');
            const elDonBar = document.getElementById('goalDonationsBar');
            const elDonPct = document.getElementById('goalDonationsProgress');
            if (elDon) elDon.textContent = `$${data.donations} / $500`;
            if (elDonBar) elDonBar.style.width = donationPercent + '%';
            if (elDonPct) elDonPct.textContent = Math.round(donationPercent) + '%';
            const userPercent = Math.min(100, (data.users / 1000) * 100);
            const elUsr = document.getElementById('goalUsers');
            const elUsrBar = document.getElementById('goalUsersBar');
            const elUsrPct = document.getElementById('goalUsersProgress');
            if (elUsr) elUsr.textContent = `${data.users} / 1000`;
            if (elUsrBar) elUsrBar.style.width = userPercent + '%';
            if (elUsrPct) elUsrPct.textContent = Math.round(userPercent) + '%';
        } catch (e) { console.error('Load goal data error:', e); }
    }

    // ============================================
    // SECTION 34.5: REFRESH FUNCTIONS
    // ============================================

    async function refreshBalance() {
        showToast('🔄 Refreshing balance...', 'info');
        await loadBalance();
        await loadTicketBalance();
        showToast('✅ Balance refreshed!', 'success');
    }

    async function refreshStats() {
        showToast('🔄 Refreshing stats...', 'info');
        await loadStats();
        showToast('✅ Stats refreshed!', 'success');
    }

    async function refreshQuests() {
        showToast('🔄 Refreshing quests...', 'info');
        await loadQuests();
        showToast('✅ Quests refreshed!', 'success');
    }

    async function refreshPremium() {
        showToast('🔄 Refreshing premium...', 'info');
        await loadPremiumCases();
        await loadTicketBalance();
        await loadGoalData();
        showToast('✅ Premium refreshed!', 'success');
    }

    async function refreshAll() {
        showToast('🔄 Refreshing all data...', 'info');
        await loadBalance();
        await loadStats();
        await loadQuests();
        await loadInventory(state.currentPage);
        await loadPremiumCases();
        await loadTicketBalance();
        await loadGoalData();
        showToast('✅ All data refreshed!', 'success');
    }

    document.addEventListener('keydown', function(e) {
        if (e.key === 'R' && e.shiftKey) {
            refreshAll();
            e.preventDefault();
        }
    });

    // ============================================
    // SECTION 35: INITIALIZATION
    // ============================================

    document.getElementById('popupOverlay').addEventListener('click', function(e) {
        if (e.target === this) closePopup();
    });
    document.getElementById('settingsModal').addEventListener('click', function(e) {
        if (e.target === this) closeSettings();
    });

    // ============================================
    // SECTION 36: EXTRA CONFETTI & SHORTCUTS
    // ============================================

    const originalLoadStreak = loadStreak;
    loadStreak = async function() {
        try {
            const data = await apiCall('/api/user/streak');
            state.streakData = data;

            if (data.current_streak > 0 && data.current_streak % 10 === 0) {
                spawnConfetti('Gold', 150);
                spawnCoinShower(50);
                spawnRainbowConfetti();
                showToast(`🔥 ${data.current_streak} STREAK! Amazing! 🔥`, 'success');
            }

        } catch (e) { console.error('Load streak error:', e); }
    };

    window.testConfetti = function() {
        spawnConfetti('Gold', 200);
        spawnConfettiExplosion();
        spawnRainbowConfetti();
        spawnCoinShower(50);
        spawnParticles('Gold', 50);
        console.log('🎊 Confetti test complete!');
        console.log('🎉 If you see confetti, everything works!');
    };

    console.log('💡 Type testConfetti() in the console to test confetti!');

    document.addEventListener('keydown', function(e) {
        if (e.key === 'C' && e.shiftKey) {
            if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA' && e.target.tagName !== 'SELECT') {
                testConfetti();
                e.preventDefault();
            }
        }
    });

    // ============================================
    // KEYBOARD SHORTCUTS - MAIN HANDLER
    // ============================================

    document.addEventListener('keydown', function(e) {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') {
            return;
        }

        const overlay = document.getElementById('popupOverlay');
        const isPopupOpen = overlay.classList.contains('show');

        if (isPopupOpen) {
            switch(e.key) {
                case 'Escape':
                    closePopup();
                    e.preventDefault();
                    break;
                case ' ':
                case 'Space':
                    const primaryBtn = document.querySelector('.popup-content .btn-success, .popup-content .btn-primary');
                    if (primaryBtn) {
                        primaryBtn.click();
                        e.preventDefault();
                    }
                    break;
                case 'k':
                case 'K':
                    const keepBtn = document.querySelector('.popup-content .btn-success');
                    if (keepBtn) {
                        keepBtn.click();
                        e.preventDefault();
                    }
                    break;
                case 's':
                case 'S':
                    const sellBtn = document.querySelector('.popup-content .btn-danger');
                    if (sellBtn) {
                        sellBtn.click();
                        e.preventDefault();
                    }
                    break;
                case 'n':
                case 'N':
                    const nextBtn = document.querySelector('.popup-content .btn-primary');
                    if (nextBtn && nextBtn.textContent.includes('Next')) {
                        nextBtn.click();
                        e.preventDefault();
                    }
                    break;
            }
            return;
        }

        switch(e.key) {
            case 'q':
            case 'Q':
                setBulkQuantity(1);
                e.preventDefault();
                break;
            case 'w':
            case 'W':
                setBulkQuantity(5);
                e.preventDefault();
                break;
            case 'e':
            case 'E':
                setBulkQuantity(10);
                e.preventDefault();
                break;
            case 'r':
            case 'R':
                setBulkQuantity(25);
                e.preventDefault();
                break;
        }

        const tabMap = {
            '1': 'cases',
            '2': 'capsules',
            '3': 'games',
            '4': 'quests',
            '5': 'inventory',
            '6': 'trade',
            '7': 'stats',
            '8': 'leaderboard',
            '9': 'premium',
            '0': 'achievements',
            '-': 'profile'
        };

        const tab = tabMap[e.key];
        if (tab) {
            switchTab(tab);
            e.preventDefault();
            showToast(`📋 Switched to ${tab.charAt(0).toUpperCase() + tab.slice(1)}`, 'info');
        }

        switch(e.key) {
            case 'c':
            case 'C':
                claimDaily();
                e.preventDefault();
                break;
            case 'h':
            case 'H':
                claimHourly();
                e.preventDefault();
                break;
        }
    });

    function showShortcutsHelp() {
        const shortcuts = `
            ⌨️ Keyboard Shortcuts:

            ─── Navigation ───
            1  Cases
            2  Capsules
            3  Games
            4  Quests
            5  Inventory
            6  Trade
            7  Stats
            8  Leaderboard
            9  Premium
            0  Achievements
            -  Profile

            ─── Actions ───
            Q  1 Case
            W  5 Cases
            E  10 Cases
            R  25 Cases
            C  Claim Daily
            H  Claim Hourly
            Shift+C  Test Confetti
            Shift+R  Refresh All
            ESC  Close Popup
            Space  Confirm/Keep
            K  Keep Item
            S  Sell Item
            N  Next Item (Bulk)
        `;
        showToast(shortcuts, 'info');
    }

    document.addEventListener('keydown', function(e) {
        if (e.key === '?' && e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA' && e.target.tagName !== 'SELECT') {
            showShortcutsHelp();
            e.preventDefault();
        }
    });

    checkAuth();

    // Show post-payment success toast when redirected back from Stripe
    (function() {
        const params = new URLSearchParams(window.location.search);
        const p = params.get('payment');
        if (p === 'tickets') {
            history.replaceState(null, '', '/');
            setTimeout(() => showToast('🎟️ Tickets purchased! Your balance has been updated.'), 800);
        } else if (p === 'vip') {
            history.replaceState(null, '', '/');
            setTimeout(() => showToast('⭐ VIP activated! Enjoy your perks.'), 800);
        }

        // Deep-link from games page: /?arcade=reaction etc.
        const arcade = params.get('arcade');
        if (arcade) {
            history.replaceState(null, '', '/');
            const arcadeMap = {
                reaction: '/games/reaction.html',
                aim:      '/games/aim-trainer.html',
                bomb:     '/games/bomb-defuse.html',
                float:    '/games/float-guesser.html',
                memory:   '/games/memory-sequence.html',
            };
            const url = arcadeMap[arcade];
            if (url) setTimeout(() => { location.href = url; }, 600);
        }

        // Auto-apply referral code from ?ref=CODE URL param
        const refCode = params.get('ref');
        if (refCode) {
            history.replaceState(null, '', '/');
            // Wait for auth to load, then apply the code
            setTimeout(async () => {
                try {
                    const d = await apiCall('/api/referral/apply', {
                        method: 'POST',
                        body: JSON.stringify({ code: refCode.toUpperCase() })
                    });
                    showToast('🎉 Referral applied! +$500 bonus added.');
                    loadTicketBalance();
                } catch(e) {
                    // Silently ignore if already referred or invalid
                }
            }, 2000);
        }
    })();

    // Live lobby ticker moved to static/js/live-ticker.js (self-contained,
    // shared across every page that includes it, not just the dashboard).

    // ============================================
    // SECTION 99: FRIENDS SYSTEM
    // ============================================

    let _friendsLoaded = false;
    async function loadFriendsTab() {
        await Promise.all([loadFriends(), loadFriendRequests(), loadPvpChallenges()]);
    }

    async function loadFriends() {
        try {
            const data = await apiCall('/api/friends');
            const list = document.getElementById('friendsList');
            if (!list) return;
            const online = data.friends.filter(f => f.online).length;
            const countEl = document.getElementById('friendsOnlineCount');
            if (countEl) countEl.textContent = `${online} online · ${data.friends.length} total`;
            if (!data.friends.length) {
                list.innerHTML = '<div style="color:#888;font-size:13px;text-align:center;padding:20px;">No friends yet — add someone by username!</div>';
                return;
            }
            list.innerHTML = data.friends.map(f => `
                <div class="social-row" style="padding:12px;background:rgba(255,255,255,0.03);border-radius:10px;border:1px solid rgba(255,255,255,0.06);">
                    <div style="position:relative;flex-shrink:0;">
                        <img src="${f.avatar_url || 'https://cdn.discordapp.com/embed/avatars/0.png'}" style="width:42px;height:42px;border-radius:50%;object-fit:cover;" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
                        <div style="position:absolute;bottom:0;right:0;width:12px;height:12px;border-radius:50%;border:2px solid #12121e;background:${f.online ? '#4caf50' : '#555'};"></div>
                    </div>
                    <div class="social-row-info">
                        <div class="name">${esc(f.username)}</div>
                        <div style="color:#888;font-size:11px;">Level ${f.level} · ${f.online ? '<span style="color:#4caf50;">Online</span>' : 'Offline'}</div>
                    </div>
                    <div class="social-row-actions">
                        <button class="btn btn-sm" style="background:rgba(255,215,0,0.1);border-color:rgba(255,215,0,0.3);font-size:10px;" onclick="viewFriendProfile('${f.user_id}')">👤 Profile</button>
                        <button class="btn btn-sm" style="background:rgba(88,101,242,0.15);border-color:rgba(88,101,242,0.4);font-size:10px;" onclick="challengeFriend('${f.user_id}', '${esc(f.username)}')">⚔️ Challenge</button>
                        <button class="btn btn-sm" style="background:rgba(231,76,60,0.1);border-color:rgba(231,76,60,0.3);font-size:10px;" onclick="removeFriend('${f.user_id}', '${esc(f.username)}')">✕</button>
                    </div>
                </div>
            `).join('');
        } catch(e) { console.error('loadFriends:', e); }
    }

    async function loadFriendRequests() {
        try {
            const data = await apiCall('/api/friends/requests');
            const card = document.getElementById('friendRequestsCard');
            const hasAny = data.incoming.length || data.outgoing.length;
            if (card) card.style.display = hasAny ? 'block' : 'none';
            if (!hasAny) return;

            const inEl = document.getElementById('incomingRequests');
            const outEl = document.getElementById('outgoingRequests');
            if (inEl) {
                inEl.innerHTML = data.incoming.length ? `
                    <div style="color:#888;font-size:11px;margin-bottom:8px;text-transform:uppercase;">Incoming</div>
                    ${data.incoming.map(r => `
                        <div class="social-row" style="padding:10px;background:rgba(76,175,80,0.06);border:1px solid rgba(76,175,80,0.2);border-radius:8px;margin-bottom:6px;">
                            <img src="${r.avatar_url || 'https://cdn.discordapp.com/embed/avatars/0.png'}" style="width:34px;height:34px;border-radius:50%;flex-shrink:0;" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
                            <div class="social-row-info"><span class="name">${esc(r.username)}</span></div>
                            <div class="social-row-actions">
                                <button class="btn btn-sm" style="background:#4caf50;color:#fff;font-size:10px;" onclick="acceptFriendRequest(${r.id})">✓ Accept</button>
                                <button class="btn btn-sm" style="background:rgba(231,76,60,0.2);font-size:10px;" onclick="declineFriendRequest(${r.id})">✕ Decline</button>
                            </div>
                        </div>
                    `).join('')}
                ` : '';
            }
            if (outEl) {
                outEl.innerHTML = data.outgoing.length ? `
                    <div style="color:#888;font-size:11px;margin-bottom:8px;text-transform:uppercase;">Sent</div>
                    ${data.outgoing.map(r => `
                        <div class="social-row" style="padding:10px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);border-radius:8px;margin-bottom:6px;">
                            <img src="${r.avatar_url || 'https://cdn.discordapp.com/embed/avatars/0.png'}" style="width:34px;height:34px;border-radius:50%;flex-shrink:0;" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
                            <div class="social-row-info"><span class="name">${esc(r.username)}</span></div>
                            <span style="color:#888;font-size:11px;flex-shrink:0;">Pending...</span>
                        </div>
                    `).join('')}
                ` : '';
            }
        } catch(e) { console.error('loadFriendRequests:', e); }
    }

    async function loadPvpChallenges() {
        try {
            const data = await apiCall('/api/friends/challenges');
            const card = document.getElementById('challengesCard');
            const list = document.getElementById('challengesList');
            const badge = document.getElementById('challengesBadge');
            if (!card || !list) return;
            const ticketChallenges = data.challenges || [];
            const duelChallenges = data.duel_challenges || [];
            const total = ticketChallenges.length + duelChallenges.length;
            card.style.display = total ? 'block' : 'none';
            if (badge) badge.textContent = total || '';
            if (!total) return;

            const ticketRows = ticketChallenges.map(c => `
                <div class="social-row" style="padding:12px;background:rgba(88,101,242,0.08);border:1px solid rgba(88,101,242,0.3);border-radius:10px;">
                    <img src="${c.challenger_avatar || 'https://cdn.discordapp.com/embed/avatars/0.png'}" style="width:38px;height:38px;border-radius:50%;flex-shrink:0;" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
                    <div class="social-row-info">
                        <div class="name">${esc(c.challenger_name)}</div>
                        <div style="color:#888;font-size:11px;">Ticket coinflip · ${c.bet_tickets} 🎟️ each</div>
                    </div>
                    <div class="social-row-actions">
                        <button class="btn btn-sm" style="background:#4caf50;color:#fff;font-size:11px;" onclick="acceptPvpChallenge(${c.id})">Accept</button>
                        <button class="btn btn-sm" style="background:rgba(231,76,60,0.2);font-size:11px;" onclick="declinePvpChallenge(${c.id})">Decline</button>
                    </div>
                </div>
            `).join('');

            const duelRows = duelChallenges.map(c => {
                const label = GAME_CHALLENGE_LABELS[c.game_type] || c.game_type;
                const inviteeSummary = c.invitees.map(iv => `${esc(iv.username)} (${iv.status})`).join(', ');
                if (c.is_challenger) {
                    return `
                        <div class="social-row" style="padding:12px;background:rgba(255,215,0,0.06);border:1px solid rgba(255,215,0,0.2);border-radius:10px;">
                            <div class="social-row-info">
                                <div class="name">${label} — you challenged</div>
                                <div style="color:#888;font-size:11px;">${inviteeSummary || 'Waiting for response'}</div>
                            </div>
                        </div>`;
                }
                return `
                    <div class="social-row" style="padding:12px;background:rgba(88,101,242,0.08);border:1px solid rgba(88,101,242,0.3);border-radius:10px;">
                        <img src="${c.challenger_avatar || 'https://cdn.discordapp.com/embed/avatars/0.png'}" style="width:38px;height:38px;border-radius:50%;flex-shrink:0;" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
                        <div class="social-row-info">
                            <div class="name">${esc(c.challenger_name)}</div>
                            <div style="color:#888;font-size:11px;">${label}${inviteeSummary ? ' · ' + inviteeSummary : ''}</div>
                        </div>
                        <div class="social-row-actions">
                            <button class="btn btn-sm" style="background:#4caf50;color:#fff;font-size:11px;" onclick="acceptGameChallenge(${c.id}, '${c.game_type}')">Accept</button>
                            <button class="btn btn-sm" style="background:rgba(231,76,60,0.2);font-size:11px;" onclick="declineGameChallenge(${c.id})">Decline</button>
                        </div>
                    </div>`;
            }).join('');

            list.innerHTML = ticketRows + duelRows;
        } catch(e) { console.error('loadPvpChallenges:', e); }
    }

    async function sendFriendRequest(usernameOrId) {
        const input = document.getElementById('friendSearchInput');
        const result = document.getElementById('friendSearchResult');
        const q = usernameOrId || (input?.value || '').trim();
        if (!q) return;
        closeFriendSearchDropdown();
        try {
            const data = await apiCall('/api/friends/request', {method:'POST', body: JSON.stringify({username_or_id: q})});
            if (result) result.innerHTML = `<span style="color:#4caf50;">${esc(data.message)}</span>`;
            if (input) input.value = '';
            setTimeout(() => { if(result) result.innerHTML=''; loadFriendRequests(); }, 2000);
        } catch(e) {
            if (result) result.innerHTML = `<span style="color:#e74c3c;">${e.message || 'Error sending request'}</span>`;
        }
    }

    // -- Add-friend autocomplete --------------------------------

    let _friendSearchDebounce = null;
    const _relLabels = {
        friends:  '<span style="color:#4caf50;font-size:11px;">Friends</span>',
        outgoing: '<span style="color:#888;font-size:11px;">Pending...</span>',
        incoming: '<span style="color:#ffd700;font-size:11px;">Wants to add you</span>',
    };

    function onFriendSearchInput() {
        const input = document.getElementById('friendSearchInput');
        const q = (input?.value || '').trim();
        clearTimeout(_friendSearchDebounce);
        if (q.length < 2) { closeFriendSearchDropdown(); return; }
        _friendSearchDebounce = setTimeout(() => runFriendSearch(q), 250);
    }

    async function runFriendSearch(q) {
        const dropdown = document.getElementById('friendSearchDropdown');
        if (!dropdown) return;
        try {
            const data = await apiCall(`/api/friends/search?q=${encodeURIComponent(q)}`);
            if (!data.results.length) {
                dropdown.innerHTML = '<div style="padding:10px 12px;color:#888;font-size:12px;">No users found</div>';
                dropdown.style.display = 'block';
                return;
            }
            dropdown.innerHTML = data.results.map(u => `
                <div style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid rgba(255,255,255,0.05);">
                    <img src="${u.avatar_url || 'https://cdn.discordapp.com/embed/avatars/0.png'}" style="width:26px;height:26px;border-radius:50%;object-fit:cover;" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
                    <span style="flex:1;font-size:13px;">${esc(u.username)}</span>
                    ${u.relationship === 'none'
                        ? `<button class="btn btn-sm" style="font-size:10px;" onclick="sendFriendRequest('${u.user_id}')">Add</button>`
                        : (_relLabels[u.relationship] || '')}
                </div>
            `).join('');
            dropdown.style.display = 'block';
        } catch(e) { console.error('runFriendSearch:', e); }
    }

    function closeFriendSearchDropdown() {
        const dropdown = document.getElementById('friendSearchDropdown');
        if (dropdown) { dropdown.style.display = 'none'; dropdown.innerHTML = ''; }
    }

    document.addEventListener('click', (e) => {
        const dropdown = document.getElementById('friendSearchDropdown');
        const input = document.getElementById('friendSearchInput');
        if (!dropdown || dropdown.style.display === 'none') return;
        if (e.target !== input && !dropdown.contains(e.target)) closeFriendSearchDropdown();
    });

    async function acceptFriendRequest(id) {
        try {
            await apiCall(`/api/friends/accept/${id}`, {method:'POST'});
            showToast('Friend request accepted!', 'success');
            loadFriendsTab();
        } catch(e) { showToast(e.message || 'Error', 'error'); }
    }

    async function declineFriendRequest(id) {
        try {
            await apiCall(`/api/friends/decline/${id}`, {method:'POST'});
            loadFriendRequests();
        } catch(e) { showToast(e.message || 'Error', 'error'); }
    }

    async function removeFriend(friendId, name) {
        if (!confirm(`Remove ${name} from friends?`)) return;
        try {
            await apiCall(`/api/friends/${friendId}`, {method:'DELETE'});
            showToast(`Removed ${name}`, 'info');
            loadFriends();
        } catch(e) { showToast(e.message || 'Error', 'error'); }
    }

    async function viewFriendProfile(friendId) {
        try {
            const p = await apiCall(`/api/friends/${friendId}/profile`);
            let loadoutItems = [];
            try { loadoutItems = (await apiCall(`/api/friends/${friendId}/loadout`)).items || []; } catch(e) {}
            const rarityColor = {Gold:'#ffd700',Red:'#e74c3c',Pink:'#e040fb',Purple:'#9c27b0',Blue:'#2196f3'};
            openArcade(`
                <div style="text-align:center;">
                    <div style="position:relative;display:inline-block;margin-bottom:12px;">
                        <img src="${p.avatar_url || 'https://cdn.discordapp.com/embed/avatars/0.png'}" style="width:80px;height:80px;border-radius:50%;border:3px solid #ffd700;" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
                        <div style="position:absolute;bottom:2px;right:2px;width:16px;height:16px;border-radius:50%;border:2px solid #12121e;background:${p.online ? '#4caf50' : '#555'};"></div>
                    </div>
                    <div style="font-size:20px;font-weight:700;margin-bottom:4px;">${esc(p.username)}</div>
                    <div style="color:#888;font-size:12px;margin-bottom:16px;">${p.online ? '🟢 Online' : '⚫ Offline'}</div>
                    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:20px;">
                        <div style="background:rgba(255,215,0,0.08);border:1px solid rgba(255,215,0,0.2);border-radius:8px;padding:12px;">
                            <div style="font-size:22px;font-weight:700;color:#ffd700;">${p.level}</div>
                            <div style="color:#888;font-size:10px;">Level</div>
                        </div>
                        <div style="background:rgba(255,215,0,0.08);border:1px solid rgba(255,215,0,0.2);border-radius:8px;padding:12px;">
                            <div style="font-size:22px;font-weight:700;color:#ffd700;">${p.prestige}</div>
                            <div style="color:#888;font-size:10px;">Prestige</div>
                        </div>
                        <div style="background:rgba(255,215,0,0.08);border:1px solid rgba(255,215,0,0.2);border-radius:8px;padding:12px;">
                            <div style="font-size:22px;font-weight:700;color:#ffd700;">${p.item_count}</div>
                            <div style="color:#888;font-size:10px;">Items</div>
                        </div>
                    </div>
                    ${loadoutItems.length ? `
                        <div style="text-align:left;margin-bottom:20px;">
                            <div style="color:#888;font-size:11px;text-transform:uppercase;margin-bottom:8px;">Loadout</div>
                            <div class="loadout-grid">${loadoutItems.map(loadoutCardHtml).join('')}</div>
                        </div>
                    ` : ''}
                    ${p.recent_drops.length ? `
                        <div style="text-align:left;">
                            <div style="color:#888;font-size:11px;text-transform:uppercase;margin-bottom:8px;">Recent Drops</div>
                            ${p.recent_drops.map(d => `
                                <div style="padding:6px 10px;background:rgba(255,255,255,0.03);border-radius:6px;margin-bottom:4px;font-size:12px;border-left:3px solid ${rarityColor[d.rarity]||'#555'};display:flex;align-items:center;justify-content:space-between;gap:8px;">
                                    <span>${esc(d.name)} <span style="color:${rarityColor[d.rarity]||'#888'};font-size:10px;">${d.rarity}</span></span>
                                    <span style="display:flex;align-items:center;gap:6px;flex-shrink:0;">${condBadgeHtml(d.condition)}${d.float_value != null ? `<span class="float-val">${Number(d.float_value).toFixed(4)}</span>` : ''}</span>
                                </div>
                            `).join('')}
                        </div>
                    ` : '<div style="color:#888;font-size:13px;">No drops yet</div>'}
                    <button class="btn btn-primary" style="margin-top:16px;width:100%;" onclick="closeArcade();challengeFriend('${p.user_id}','${esc(p.username)}')">⚔️ Challenge</button>
                </div>
            `);
        } catch(e) { showToast(e.message || 'Error loading profile', 'error'); }
    }

    const GAME_CHALLENGE_LABELS = {
        dice_duel: '🎲 Dice Duel', weapon_duel: '🔫 Weapon Duel', reaction_duel: '⚡ Reaction Duel',
        case_draft_duel: '🃏 Case Draft Duel', item_wager_duel: '⚔️ Item Wager Duel',
        item_trade_up_duel: '🔺 Item Trade-Up Duel', case_battles: '⚔️ Case Battles',
        ladder_race: '🪜 Ladder Race', mines_race: '💣 Mines Race', sync_slots: '🎰 Sync-Spin Slots',
        koth_ladder: '🏔️ King of the Hill Ladder', battle_royale_mines: '💥 Battle Royale Minefield',
        speed_case_race: '📦 Speed Case Race', live_blackjack: '♠️ Live Blackjack',
        live_roulette: '🎡 Live Roulette', live_keno: '🎱 Live Keno Draw',
    };
    const GAME_CHALLENGE_ITEM = new Set(['item_wager_duel', 'item_trade_up_duel']);
    const GAME_CHALLENGE_MULTI = new Set(['case_battles']);
    const GAME_CHALLENGE_PAGE = {
        dice_duel: '/games/dice-duel.html', weapon_duel: '/games/weapon-duel.html',
        reaction_duel: '/games/reaction-duel.html', case_draft_duel: '/games/case-draft-duel.html',
        item_wager_duel: '/games/item-wager-duel.html', item_trade_up_duel: '/games/item-trade-up-duel.html',
        case_battles: '/battle',
        ladder_race: '/games/ladder-race.html', mines_race: '/games/mines-race.html',
        sync_slots: '/games/sync-spin-slots.html', koth_ladder: '/games/koth-ladder.html',
        battle_royale_mines: '/games/battle-royale-mines.html', speed_case_race: '/games/speed-case-race.html',
        live_blackjack: '/games/live-blackjack.html', live_roulette: '/games/live-roulette.html',
        live_keno: '/games/live-keno.html',
    };
    // Most games read the target room off a `?duel=` URL param, but the 5
    // elimination/race + 3 live-table games (Session 9) each already use
    // their own `?round=`/`?table=` naming convention on their own pages --
    // this map lets the accept-and-redirect logic below build the right
    // query param per game_type instead of assuming `?duel=` universally.
    const GAME_CHALLENGE_PARAM = {
        ladder_race: 'round', mines_race: 'round', sync_slots: 'round', koth_ladder: 'round',
        battle_royale_mines: 'round', speed_case_race: 'round', live_roulette: 'round', live_keno: 'round',
        live_blackjack: 'table',
    };
    let _challengeSelectedItemId = null;
    let _challengeExtraFriendIds = [];

    async function challengeFriend(friendId, name) {
        openArcade(`
            <h3 style="margin-bottom:16px;">⚔️ Challenge ${esc(name)}</h3>
            <div style="margin-bottom:14px;">
                <label style="color:#888;font-size:12px;display:block;margin-bottom:6px;">Game</label>
                <select id="challengeGameType" onchange="renderChallengeForm('${friendId}','${esc(name)}')" style="width:100%;padding:10px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,215,0,0.3);border-radius:8px;color:#fff;font-size:13px;">
                    <option value="ticket_coinflip">🪙 Ticket Coinflip (classic)</option>
                    ${Object.entries(GAME_CHALLENGE_LABELS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}
                </select>
            </div>
            <div id="challengeFormBody"></div>
        `);
        _challengeSelectedItemId = null;
        _challengeExtraFriendIds = [];
        renderChallengeForm(friendId, name);
    }

    async function renderChallengeForm(friendId, name) {
        const gameType = document.getElementById('challengeGameType')?.value || 'ticket_coinflip';
        const body = document.getElementById('challengeFormBody');
        if (!body) return;

        if (gameType === 'ticket_coinflip') {
            body.innerHTML = `
                <p style="color:#888;font-size:13px;margin-bottom:20px;">Winner takes all — ticket coinflip (50/50). Your tickets are held until the challenge expires or is resolved.</p>
                <div style="margin-bottom:16px;">
                    <label style="color:#888;font-size:12px;display:block;margin-bottom:6px;">Tickets to wager (1–10)</label>
                    <input type="number" id="challengeBetInput" min="1" max="10" value="1"
                        style="width:100%;padding:10px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,215,0,0.3);border-radius:8px;color:#fff;font-size:16px;text-align:center;">
                </div>
                <button class="btn btn-gold" style="width:100%;font-size:15px;" onclick="_submitChallenge('${friendId}')">Send Challenge ⚔️</button>
            `;
            return;
        }

        const isItemGame = GAME_CHALLENGE_ITEM.has(gameType);
        const isMulti = GAME_CHALLENGE_MULTI.has(gameType);
        let extraHtml = '';

        if (isMulti) {
            extraHtml += `<div style="margin-bottom:14px;"><label style="color:#888;font-size:12px;display:block;margin-bottom:6px;">Also invite (optional, up to 3 total)</label><div id="challengeExtraFriends" style="max-height:140px;overflow-y:auto;">Loading friends…</div></div>`;
        }

        if (isItemGame) {
            extraHtml += `
                <div style="margin-bottom:14px;">
                    <label style="color:#888;font-size:12px;display:block;margin-bottom:6px;">Item to stake</label>
                    <div id="challengeItemPicker" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-height:180px;overflow-y:auto;">Loading inventory…</div>
                </div>`;
        } else {
            extraHtml += `
                <div style="margin-bottom:14px;">
                    <label style="color:#888;font-size:12px;display:block;margin-bottom:6px;">${gameType === 'case_battles' ? 'Entry fee' : 'Stake'} ($10 min)</label>
                    <input type="number" id="challengeStakeInput" min="10" max="750000" value="50"
                        style="width:100%;padding:10px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,215,0,0.3);border-radius:8px;color:#fff;font-size:16px;text-align:center;margin-bottom:8px;">
                    <div style="display:flex;gap:6px;">
                        <button type="button" class="btn btn-sm" onclick="document.getElementById('challengeStakeInput').value=10">$10</button>
                        <button type="button" class="btn btn-sm" onclick="document.getElementById('challengeStakeInput').value=50">$50</button>
                        <button type="button" class="btn btn-sm" onclick="document.getElementById('challengeStakeInput').value=100">$100</button>
                        <button type="button" class="btn btn-sm" onclick="document.getElementById('challengeStakeInput').value=500">$500</button>
                    </div>
                </div>`;
        }

        body.innerHTML = `
            <p style="color:#888;font-size:13px;margin-bottom:16px;">Costs 2 🎟️ to send, they pay 2 🎟️ to accept — on top of the game's own stake.</p>
            ${extraHtml}
            <button class="btn btn-gold" style="width:100%;font-size:15px;" onclick="_submitGameChallenge('${friendId}', '${gameType}')">Send Challenge ⚔️</button>
        `;

        if (isMulti) loadChallengeExtraFriends(friendId);
        if (isItemGame) loadChallengeItemPicker();
    }

    async function loadChallengeExtraFriends(primaryFriendId) {
        const el = document.getElementById('challengeExtraFriends');
        if (!el) return;
        try {
            const data = await apiCall('/api/friends');
            const others = (data.friends || []).filter(f => f.user_id !== String(primaryFriendId));
            _challengeExtraFriendIds = [];
            el.innerHTML = others.length ? others.map(f => `
                <label style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px;">
                    <input type="checkbox" value="${f.user_id}" onchange="toggleChallengeExtraFriend('${f.user_id}', this.checked)">
                    ${esc(f.username)}
                </label>
            `).join('') : '<div style="color:#888;font-size:11px;">No other friends to invite</div>';
        } catch (e) { el.innerHTML = '<div style="color:#e74c3c;font-size:11px;">Error loading friends</div>'; }
    }

    function toggleChallengeExtraFriend(id, checked) {
        if (checked) {
            if (_challengeExtraFriendIds.length >= 2) {
                showToast('Case Battles friend challenges support up to 3 invitees', 'error');
                event.target.checked = false;
                return;
            }
            _challengeExtraFriendIds.push(id);
        } else {
            _challengeExtraFriendIds = _challengeExtraFriendIds.filter(x => x !== id);
        }
    }

    async function loadChallengeItemPicker() {
        const el = document.getElementById('challengeItemPicker');
        if (!el) return;
        _challengeSelectedItemId = null;
        try {
            const data = await apiCall('/api/user/me/inventory?limit=200');
            const items = (data.items || []).filter(it => Number(it.price) >= 0.50 && !it.in_loadout);
            if (!items.length) { el.innerHTML = '<div style="grid-column:1/-1;color:#888;font-size:11px;">No eligible items</div>'; return; }
            el.innerHTML = items.map(it => `
                <div id="challItem-${it.id}" onclick="selectChallengeItem(${it.id})" style="border:2px solid rgba(255,255,255,0.08);border-radius:8px;padding:4px;text-align:center;cursor:pointer;">
                    <img src="${it.image_url || ''}" style="width:100%;aspect-ratio:1;object-fit:contain;" onerror="this.style.display='none'">
                    <div style="font-size:8px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(it.display_name || it.item_name)}</div>
                    <div style="font-size:9px;color:#ffd700;">$${Number(it.price).toFixed(2)}</div>
                </div>
            `).join('');
        } catch (e) { el.innerHTML = '<div style="grid-column:1/-1;color:#e74c3c;font-size:11px;">Error loading inventory</div>'; }
    }

    function selectChallengeItem(id) {
        document.querySelectorAll('#challengeItemPicker > div').forEach(el => el.style.borderColor = 'rgba(255,255,255,0.08)');
        const el = document.getElementById('challItem-' + id);
        if (el) el.style.borderColor = '#ffd700';
        _challengeSelectedItemId = id;
    }

    async function _submitGameChallenge(friendId, gameType) {
        const invited = [String(friendId), ..._challengeExtraFriendIds.map(String)];
        let gameParams = {};
        if (GAME_CHALLENGE_ITEM.has(gameType)) {
            if (!_challengeSelectedItemId) { showToast('Select an item to stake', 'error'); return; }
            gameParams = { inventory_ids: {} };
            gameParams.inventory_ids[String(state.userId)] = _challengeSelectedItemId;
        } else {
            const stake = parseFloat(document.getElementById('challengeStakeInput')?.value || 50);
            gameParams = gameType === 'case_battles'
                ? { fee: stake, rounds: 3, win_condition: 'total_value' }
                : { stake };
        }
        try {
            await apiCall('/api/friends/challenge', {
                method: 'POST',
                body: JSON.stringify({ game_type: gameType, invited_user_ids: invited, game_params: gameParams })
            });
            openArcade(`<div style="text-align:center;padding:20px;">
                <div style="font-size:48px;margin-bottom:12px;">⚔️</div>
                <div style="font-size:18px;font-weight:700;margin-bottom:8px;">Challenge Sent!</div>
                <div style="color:#888;font-size:13px;">Waiting for ${invited.length > 1 ? 'everyone' : 'them'} to accept.</div>
                <button class="btn btn-primary" style="margin-top:16px;" onclick="closeArcade()">Done</button>
            </div>`);
            loadTicketBalance();
        } catch (e) { showToast(e.message || 'Error', 'error'); }
    }

    async function _submitChallenge(friendId) {
        const bet = parseInt(document.getElementById('challengeBetInput')?.value || 1);
        try {
            await apiCall(`/api/friends/${friendId}/challenge`, {method:'POST', body: JSON.stringify({bet_tickets: bet})});
            openArcade(`<div style="text-align:center;padding:20px;">
                <div style="font-size:48px;margin-bottom:12px;">⚔️</div>
                <div style="font-size:18px;font-weight:700;margin-bottom:8px;">Challenge Sent!</div>
                <div style="color:#888;font-size:13px;">Waiting for them to accept. ${bet} ticket${bet>1?'s':''} held.</div>
                <button class="btn btn-primary" style="margin-top:16px;" onclick="closeArcade()">Done</button>
            </div>`);
            loadTicketBalance();
        } catch(e) { showToast(e.message || 'Error', 'error'); }
    }

    async function acceptPvpChallenge(id) {
        try {
            const data = await apiCall(`/api/friends/challenges/${id}/accept`, {method:'POST'});
            if (data.you_won) {
                spawnConfetti('Gold', 80);
                openArcade(`<div style="text-align:center;padding:20px;">
                    <div style="font-size:56px;margin-bottom:12px;">🏆</div>
                    <div style="font-size:22px;font-weight:700;color:#ffd700;margin-bottom:8px;">You Won!</div>
                    <div style="color:#888;font-size:14px;">+${data.tickets_won} tickets added to your balance</div>
                    <button class="btn btn-gold" style="margin-top:16px;" onclick="closeArcade()">Collect</button>
                </div>`);
            } else {
                openArcade(`<div style="text-align:center;padding:20px;">
                    <div style="font-size:56px;margin-bottom:12px;">😔</div>
                    <div style="font-size:22px;font-weight:700;margin-bottom:8px;">You Lost</div>
                    <div style="color:#888;font-size:14px;">Better luck next time!</div>
                    <button class="btn btn-primary" style="margin-top:16px;" onclick="closeArcade()">Close</button>
                </div>`);
            }
            loadTicketBalance();
            loadPvpChallenges();
        } catch(e) { showToast(e.message || 'Error', 'error'); }
    }

    async function declinePvpChallenge(id) {
        try {
            await apiCall(`/api/friends/challenges/${id}/decline`, {method:'POST'});
            showToast('Challenge declined — tickets refunded to challenger', 'info');
            loadPvpChallenges();
        } catch(e) { showToast(e.message || 'Error', 'error'); }
    }

    async function acceptGameChallenge(id, gameType) {
        try {
            if (GAME_CHALLENGE_ITEM.has(gameType)) {
                openArcade(`
                    <h3 style="margin-bottom:16px;">${GAME_CHALLENGE_LABELS[gameType] || 'Accept Challenge'}</h3>
                    <p style="color:#888;font-size:13px;margin-bottom:16px;">Pick an item to stake, then accept (costs 2 🎟️).</p>
                    <div id="challengeItemPicker" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;max-height:220px;overflow-y:auto;margin-bottom:14px;">Loading inventory…</div>
                    <button class="btn btn-gold" style="width:100%;" onclick="_finishAcceptGameChallenge(${id})">Accept ⚔️</button>
                `);
                _challengeSelectedItemId = null;
                loadChallengeItemPicker();
                return;
            }
            await _finishAcceptGameChallenge(id);
        } catch(e) { showToast(e.message || 'Error', 'error'); }
    }

    async function _finishAcceptGameChallenge(id) {
        try {
            const body = _challengeSelectedItemId ? { inventory_id: _challengeSelectedItemId } : {};
            const data = await apiCall(`/api/friends/challenge/${id}/accept`, {method:'POST', body: JSON.stringify(body)});
            closeArcade();
            loadTicketBalance();
            loadPvpChallenges();
            if (data.status === 'started' && data.game_room_id && GAME_CHALLENGE_PAGE[data.game_type]) {
                showToast('Challenge accepted — joining now!', 'success');
                let url;
                if (data.game_type === 'case_battles') {
                    const gp = data.game_params || {};
                    url = `/battle?id=${data.game_room_id}&fee=${gp.fee || ''}&rounds=${gp.rounds || 3}&win=${gp.win_condition || 'total_value'}&mode=pvp`;
                } else {
                    const param = GAME_CHALLENGE_PARAM[data.game_type] || 'duel';
                    url = `${GAME_CHALLENGE_PAGE[data.game_type]}?${param}=${data.game_room_id}`;
                }
                setTimeout(() => { location.href = url; }, 800);
            } else {
                showToast('Accepted — waiting on other invitees.', 'info');
            }
        } catch(e) { showToast(e.message || 'Error', 'error'); }
    }

    async function declineGameChallenge(id) {
        try {
            await apiCall(`/api/friends/challenge/${id}/decline`, {method:'POST'});
            showToast('Challenge declined', 'info');
            loadTicketBalance();
            loadPvpChallenges();
        } catch(e) { showToast(e.message || 'Error', 'error'); }
    }

    // ============================================
    // SECTION 100: TICKET ARCADE
    // ============================================

    function openArcade(html) {
        const overlay = document.getElementById('arcadeOverlay');
        const body = document.getElementById('arcadeBody');
        if (!overlay || !body) return;
        body.innerHTML = html;
        overlay.style.display = 'flex';
    }

    function closeArcade() {
        const overlay = document.getElementById('arcadeOverlay');
        if (overlay) overlay.style.display = 'none';
        const body = document.getElementById('arcadeBody');
        if (body) body.innerHTML = '';
    }

    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeArcade(); });

    // ── Reaction Time ─────────────────────────────────────────

    let _reactionState = null;

    async function startReactionGame() {
        try {
            const data = await apiCall('/api/ticket-games/reaction/start', {method:'POST'});
            _reactionState = {token: data.token, startTime: null, waiting: true};
            loadTicketBalance();
            const delay = 2000 + Math.random() * 3000;
            openArcade(`
                <h3 style="margin-bottom:20px;text-align:center;">⚡ Reaction Time</h3>
                <div id="reactionBox" onclick="recordReaction()" style="
                    height:220px;border-radius:14px;background:#e74c3c;
                    display:flex;align-items:center;justify-content:center;
                    cursor:pointer;user-select:none;transition:background 0.2s;font-size:18px;font-weight:700;">
                    Wait for green...
                </div>
                <div id="reactionMsg" style="text-align:center;color:#888;font-size:13px;margin-top:12px;">Get ready...</div>
            `);
            setTimeout(() => {
                if (!_reactionState) return;
                const box = document.getElementById('reactionBox');
                if (box) {
                    box.style.background = '#4caf50';
                    box.textContent = 'CLICK NOW!';
                    _reactionState.startTime = performance.now();
                    _reactionState.waiting = false;
                }
            }, delay);
        } catch(e) { showToast(e.message || 'Not enough tickets', 'error'); }
    }

    async function recordReaction() {
        if (!_reactionState) return;
        if (_reactionState.waiting) {
            // Clicked too early — penalise
            const box = document.getElementById('reactionBox');
            const msg = document.getElementById('reactionMsg');
            if (box) { box.style.background='#e74c3c'; box.textContent='Too early! Wait for green.'; }
            if (msg) msg.textContent = 'Restarting...';
            setTimeout(() => closeArcade(), 1500);
            try { await apiCall('/api/ticket-games/reaction/submit', {method:'POST', body: JSON.stringify({token: _reactionState.token, ms: 9999})}); } catch(_){}
            _reactionState = null;
            return;
        }
        const ms = Math.round(performance.now() - _reactionState.startTime);
        const token = _reactionState.token;
        _reactionState = null;
        try {
            const res = await apiCall('/api/ticket-games/reaction/submit', {method:'POST', body: JSON.stringify({token, ms})});
            const won = res.tickets_won;
            const label = ms < 150 ? 'SUPERHUMAN!' : ms < 250 ? 'Lightning Fast' : ms < 400 ? 'Quick!' : ms < 600 ? 'Not Bad' : 'Too Slow';
            openArcade(`<div style="text-align:center;padding:10px;">
                <div style="font-size:52px;margin-bottom:10px;">${won >= 5 ? '⚡' : won >= 2 ? '🟡' : won === 0 ? '🐢' : '✅'}</div>
                <div style="font-size:26px;font-weight:700;margin-bottom:6px;">${ms} ms</div>
                <div style="color:#ffd700;font-size:15px;margin-bottom:14px;">${label}</div>
                ${won > 0
                    ? `<div style="font-size:18px;color:#4caf50;font-weight:700;margin-bottom:6px;">+${won} tickets won!</div>`
                    : `<div style="color:#888;font-size:14px;margin-bottom:6px;">No tickets — react faster next time</div>`}
                <div style="color:#555;font-size:11px;margin-bottom:20px;">&lt;150ms=8🎟 · &lt;200ms=5🎟 · &lt;300ms=3🎟 · &lt;450ms=2🎟 · &lt;600ms=1🎟</div>
                <div style="display:flex;gap:10px;justify-content:center;">
                    <button class="btn btn-primary" onclick="startReactionGame()">Play Again (1 🎟️)</button>
                    <button class="btn" onclick="closeArcade()">Done</button>
                </div>
            </div>`);
            if (won > 0) { loadTicketBalance(); spawnConfetti('Blue', 40); }
        } catch(e) { showToast(e.message || 'Error', 'error'); closeArcade(); }
    }

    // ── Aim Trainer ───────────────────────────────────────────

    let _aimState = null;

    async function startAimGame() {
        try {
            const data = await apiCall('/api/ticket-games/aim/start', {method:'POST'});
            loadTicketBalance();
            _aimState = {token: data.token, targets: data.targets, hits: 0, idx: 0, timer: null, deadline: Date.now() + 30000};
            openArcade(`
                <h3 style="text-align:center;margin-bottom:8px;">🎯 Aim Trainer</h3>
                <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:13px;">
                    <span>Hits: <strong id="aimHits">0</strong>/20</span>
                    <span>Time: <strong id="aimTimer">30</strong>s</span>
                </div>
                <div id="aimArea" style="position:relative;height:280px;background:rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.1);border-radius:10px;overflow:hidden;cursor:crosshair;"></div>
            `);
            _runAimTimer();
            _showNextTarget();
        } catch(e) { showToast(e.message || 'Not enough tickets', 'error'); }
    }

    function _runAimTimer() {
        if (!_aimState) return;
        const el = document.getElementById('aimTimer');
        const remaining = Math.max(0, Math.ceil((_aimState.deadline - Date.now()) / 1000));
        if (el) el.textContent = remaining;
        if (remaining <= 0) { _endAimGame(); return; }
        _aimState.timer = setTimeout(_runAimTimer, 250);
    }

    function _showNextTarget() {
        if (!_aimState || _aimState.idx >= _aimState.targets.length) { _endAimGame(); return; }
        const area = document.getElementById('aimArea');
        if (!area) { _endAimGame(); return; }
        const t = _aimState.targets[_aimState.idx];
        const dot = document.createElement('div');
        dot.style.cssText = `position:absolute;left:${t.x}%;top:${t.y}%;width:${t.r*2}px;height:${t.r*2}px;
            margin-left:-${t.r}px;margin-top:-${t.r}px;border-radius:50%;
            background:radial-gradient(circle,#ff6b00,#e74c3c);cursor:crosshair;
            box-shadow:0 0 ${t.r}px rgba(255,107,0,0.6);animation:aimPulse 0.4s ease infinite alternate;`;
        dot.onclick = (e) => {
            e.stopPropagation();
            if (!_aimState) return;
            _aimState.hits++;
            _aimState.idx++;
            const hitsEl = document.getElementById('aimHits');
            if (hitsEl) hitsEl.textContent = _aimState.hits;
            dot.remove();
            _showNextTarget();
        };
        area.innerHTML = '';
        area.appendChild(dot);
    }

    async function _endAimGame() {
        if (!_aimState) return;
        clearTimeout(_aimState.timer);
        const {token, hits} = _aimState;
        _aimState = null;
        try {
            const res = await apiCall('/api/ticket-games/aim/submit', {method:'POST', body: JSON.stringify({token, hits})});
            const won = res.tickets_won;
            openArcade(`<div style="text-align:center;padding:10px;">
                <div style="font-size:52px;margin-bottom:10px;">${hits >= 18 ? '🎯' : hits >= 12 ? '✅' : '💨'}</div>
                <div style="font-size:30px;font-weight:700;margin-bottom:6px;">${hits}/20</div>
                <div style="color:#888;font-size:14px;margin-bottom:14px;">Targets hit</div>
                ${won > 0
                    ? `<div style="font-size:18px;color:#4caf50;font-weight:700;margin-bottom:6px;">+${won} tickets won!</div>`
                    : `<div style="color:#888;font-size:14px;margin-bottom:6px;">Hit more targets to win tickets</div>`}
                <div style="color:#555;font-size:11px;margin-bottom:20px;">20=6🎟 · 18+=4🎟 · 15+=3🎟 · 10+=2🎟 · 5+=1🎟</div>
                <div style="display:flex;gap:10px;justify-content:center;">
                    <button class="btn btn-primary" onclick="startAimGame()">Play Again (1 🎟️)</button>
                    <button class="btn" onclick="closeArcade()">Done</button>
                </div>
            </div>`);
            if (won > 0) { loadTicketBalance(); spawnConfetti('Blue', 40); }
        } catch(e) { showToast(e.message || 'Error', 'error'); closeArcade(); }
    }

    // ── Bomb Defuse ───────────────────────────────────────────

    let _bombState = null;

    async function startBombGame() {
        try {
            const data = await apiCall('/api/ticket-games/bomb/start', {method:'POST'});
            loadTicketBalance();
            _bombState = {token: data.token, countdown: 10, timer: null, done: false};
            const wireColors = {red:'#e74c3c',blue:'#3498db',green:'#2ecc71',yellow:'#f1c40f',white:'#ecf0f1'};
            openArcade(`
                <h3 style="text-align:center;margin-bottom:4px;">💣 Bomb Defuse</h3>
                <p style="text-align:center;color:#888;font-size:12px;margin-bottom:16px;">Cut the correct wire before time runs out. 3 tickets if right.</p>
                <div style="text-align:center;margin-bottom:20px;">
                    <div id="bombTimer" style="font-size:52px;font-weight:900;color:#e74c3c;font-family:monospace;">10</div>
                    <div style="color:#888;font-size:12px;">seconds</div>
                </div>
                <div style="display:flex;flex-direction:column;gap:10px;">
                    ${data.wires.map(w => `
                        <div onclick="cutWire('${w}')" style="
                            display:flex;align-items:center;gap:14px;padding:14px 18px;
                            border:2px solid ${wireColors[w]};border-radius:10px;cursor:pointer;
                            background:rgba(${w==='white'?'255,255,255':'0,0,0'},0.05);
                            transition:all 0.15s;" id="wire-${w}"
                            onmouseenter="this.style.background='rgba(255,255,255,0.08)'"
                            onmouseleave="this.style.background='rgba(${w==='white'?'255,255,255':'0,0,0'},0.05)'">
                            <div style="width:36px;height:8px;background:${wireColors[w]};border-radius:4px;flex-shrink:0;box-shadow:0 0 10px ${wireColors[w]}66;"></div>
                            <span style="font-size:14px;font-weight:600;color:${wireColors[w]};text-transform:capitalize;">${w} wire</span>
                            <span style="margin-left:auto;color:#555;font-size:20px;">✂️</span>
                        </div>
                    `).join('')}
                </div>
            `);
            _bombState.timer = setInterval(() => {
                if (!_bombState || _bombState.done) return;
                _bombState.countdown--;
                const el = document.getElementById('bombTimer');
                if (el) {
                    el.textContent = _bombState.countdown;
                    if (_bombState.countdown <= 3) el.style.animation = 'blink 0.5s infinite';
                }
                if (_bombState.countdown <= 0) {
                    clearInterval(_bombState.timer);
                    _bombState.done = true;
                    cutWire('__timeout__');
                }
            }, 1000);
        } catch(e) { showToast(e.message || 'Not enough tickets', 'error'); }
    }

    async function cutWire(color) {
        if (!_bombState || _bombState.done) return;
        _bombState.done = true;
        clearInterval(_bombState.timer);
        const token = _bombState.token;
        _bombState = null;
        try {
            const res = await apiCall('/api/ticket-games/bomb/submit', {method:'POST', body: JSON.stringify({token, wire: color})});
            if (res.won) {
                spawnConfetti('Gold', 80);
                openArcade(`<div style="text-align:center;padding:20px;">
                    <div style="font-size:56px;margin-bottom:12px;">💚</div>
                    <div style="font-size:22px;font-weight:700;color:#4caf50;margin-bottom:8px;">Bomb Defused!</div>
                    <div style="color:#888;margin-bottom:12px;">You cut the ${esc(res.safe_wire)} wire — correct!</div>
                    <div style="font-size:18px;color:#ffd700;font-weight:700;margin-bottom:20px;">+3 tickets won!</div>
                    <div style="display:flex;gap:10px;justify-content:center;">
                        <button class="btn btn-primary" onclick="startBombGame()">Play Again (1 🎟️)</button>
                        <button class="btn" onclick="closeArcade()">Done</button>
                    </div>
                </div>`);
                loadTicketBalance();
            } else {
                const msg = color === '__timeout__' ? "Time's up! 💥" : `Wrong wire! You cut ${esc(res.chose)} — it was ${esc(res.safe_wire)}.`;
                openArcade(`<div style="text-align:center;padding:20px;">
                    <div style="font-size:56px;margin-bottom:12px;">💥</div>
                    <div style="font-size:22px;font-weight:700;color:#e74c3c;margin-bottom:8px;">BOOM!</div>
                    <div style="color:#888;margin-bottom:20px;">${msg}</div>
                    <div style="display:flex;gap:10px;justify-content:center;">
                        <button class="btn btn-primary" onclick="startBombGame()">Try Again (1 🎟️)</button>
                        <button class="btn" onclick="closeArcade()">Done</button>
                    </div>
                </div>`);
            }
        } catch(e) { showToast(e.message || 'Error', 'error'); closeArcade(); }
    }

    // ── Float Guesser ─────────────────────────────────────────

    async function startFloatGame() {
        try {
            const data = await apiCall('/api/ticket-games/float/start', {method:'POST'});
            loadTicketBalance();
            const fmin = data.float_min.toFixed(4), fmax = data.float_max.toFixed(4);
            openArcade(`
                <h3 style="text-align:center;margin-bottom:4px;">🔬 Float Guesser</h3>
                <p style="text-align:center;color:#888;font-size:12px;margin-bottom:16px;">Guess the float value of this skin.</p>
                ${data.skin_image ? `<div style="text-align:center;margin-bottom:12px;"><img src="${data.skin_image}" style="max-height:120px;max-width:100%;object-fit:contain;" onerror="this.style.display='none'"></div>` : ''}
                <div style="text-align:center;font-size:15px;font-weight:600;margin-bottom:16px;">${esc(data.skin_name)}</div>
                <div style="display:flex;justify-content:space-between;font-size:11px;color:#888;margin-bottom:4px;">
                    <span>Min: ${fmin}</span><span>Max: ${fmax}</span>
                </div>
                <input type="range" id="floatSlider" min="${fmin}" max="${fmax}" step="0.0001" value="${((parseFloat(fmin)+parseFloat(fmax))/2).toFixed(4)}"
                    style="width:100%;accent-color:#ffd700;margin-bottom:8px;"
                    oninput="document.getElementById('floatGuessDisplay').textContent=parseFloat(this.value).toFixed(4)">
                <div style="text-align:center;font-size:22px;font-weight:700;color:#ffd700;margin-bottom:20px;" id="floatGuessDisplay">${((parseFloat(fmin)+parseFloat(fmax))/2).toFixed(4)}</div>
                <div style="color:#555;font-size:11px;text-align:center;margin-bottom:16px;">±0.01=8🎟 · ±0.03=5🎟 · ±0.05=3🎟 · ±0.10=1🎟</div>
                <button class="btn btn-gold" style="width:100%;" onclick="_submitFloat('${data.token}')">Lock In Guess</button>
            `);
        } catch(e) { showToast(e.message || 'Not enough tickets', 'error'); }
    }

    async function _submitFloat(token) {
        const guess = parseFloat(document.getElementById('floatSlider')?.value || 0);
        try {
            const res = await apiCall('/api/ticket-games/float/submit', {method:'POST', body: JSON.stringify({token, guess})});
            const won = res.tickets_won;
            const diffStr = res.diff.toFixed(4);
            openArcade(`<div style="text-align:center;padding:10px;">
                <div style="font-size:52px;margin-bottom:10px;">${won >= 5 ? '🎯' : won >= 1 ? '✅' : '😬'}</div>
                <div style="margin-bottom:6px;">Actual: <strong style="color:#ffd700;">${res.actual.toFixed(4)}</strong></div>
                <div style="margin-bottom:6px;">Your guess: <strong>${res.guess.toFixed(4)}</strong></div>
                <div style="color:#888;font-size:13px;margin-bottom:14px;">Difference: ${diffStr}</div>
                ${won > 0
                    ? `<div style="font-size:18px;color:#4caf50;font-weight:700;margin-bottom:6px;">+${won} tickets!</div>`
                    : `<div style="color:#888;margin-bottom:6px;">So close! Try again.</div>`}
                <div style="display:flex;gap:10px;justify-content:center;margin-top:16px;">
                    <button class="btn btn-primary" onclick="startFloatGame()">Play Again (1 🎟️)</button>
                    <button class="btn" onclick="closeArcade()">Done</button>
                </div>
            </div>`);
            if (won > 0) { loadTicketBalance(); spawnConfetti('Blue', 40); }
        } catch(e) { showToast(e.message || 'Error', 'error'); closeArcade(); }
    }

    // ── Memory Sequence ───────────────────────────────────────

    let _memState = null;

    async function startMemoryGame() {
        try {
            const data = await apiCall('/api/ticket-games/memory/start', {method:'POST'});
            loadTicketBalance();
            _memState = {token: data.token, serverSeq: data.sequence, phase: 'watch', round: 1, userInput: []};
            openArcade(`
                <h3 style="text-align:center;margin-bottom:4px;">🧠 Memory Sequence</h3>
                <p style="text-align:center;color:#888;font-size:12px;margin-bottom:14px;" id="memMsg">Watch the sequence...</p>
                <div id="memGrid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;max-width:300px;margin:0 auto 16px;"></div>
                <div style="text-align:center;color:#888;font-size:11px;" id="memStatus">Round 1 / 10</div>
            `);
            _buildMemGrid();
            _memNextRound();
        } catch(e) { showToast(e.message || 'Not enough tickets', 'error'); }
    }

    function _buildMemGrid() {
        const grid = document.getElementById('memGrid');
        if (!grid) return;
        grid.innerHTML = '';
        for (let i = 0; i < 16; i++) {
            const cell = document.createElement('div');
            cell.id = `mc-${i}`;
            cell.style.cssText = 'width:100%;aspect-ratio:1;border-radius:8px;background:rgba(255,255,255,0.06);border:2px solid rgba(255,255,255,0.1);cursor:not-allowed;transition:all 0.15s;';
            cell.onclick = () => _memCellClick(i);
            grid.appendChild(cell);
        }
    }

    function _memFlash(idx, color, duration) {
        const c = document.getElementById(`mc-${idx}`);
        if (!c) return;
        c.style.background = color;
        c.style.boxShadow = `0 0 15px ${color}`;
        setTimeout(() => { c.style.background = 'rgba(255,255,255,0.06)'; c.style.boxShadow = 'none'; }, duration);
    }

    function _memNextRound() {
        if (!_memState) return;
        const round = _memState.round;
        const statusEl = document.getElementById('memStatus');
        const msgEl = document.getElementById('memMsg');
        const grid = document.getElementById('memGrid');
        if (statusEl) statusEl.textContent = `Round ${round} / 10`;
        if (msgEl) msgEl.textContent = 'Watch the sequence...';
        // Disable clicks during playback
        if (grid) grid.querySelectorAll('div').forEach(c => c.style.cursor = 'not-allowed');

        const toShow = _memState.serverSeq.slice(0, round);
        let delay = 400;
        for (const idx of toShow) {
            setTimeout(() => _memFlash(idx, '#ffd700', 500), delay);
            delay += 750;
        }
        setTimeout(() => {
            if (!_memState) return;
            _memState.phase = 'input';
            _memState.userInput = [];
            if (msgEl) msgEl.textContent = `Your turn — repeat ${round} cell${round > 1 ? 's' : ''}`;
            if (grid) grid.querySelectorAll('div').forEach(c => c.style.cursor = 'pointer');
        }, delay);
    }

    function _memCellClick(idx) {
        if (!_memState || _memState.phase !== 'input') return;
        _memFlash(idx, '#4caf50', 300);
        _memState.userInput.push(idx);
        const expected = _memState.serverSeq.slice(0, _memState.round);
        const pos = _memState.userInput.length - 1;
        if (_memState.userInput[pos] !== expected[pos]) {
            // Wrong — flash red and end
            _memFlash(idx, '#e74c3c', 600);
            _memState.phase = 'done';
            const roundsCompleted = _memState.round - 1;
            const token = _memState.token;
            const seq = _memState.serverSeq;
            _memState = null;
            setTimeout(() => _submitMemory(token, seq, roundsCompleted), 800);
            return;
        }
        if (_memState.userInput.length === _memState.round) {
            if (_memState.round >= 10) {
                const token = _memState.token;
                const seq = _memState.serverSeq;
                _memState = null;
                setTimeout(() => _submitMemory(token, seq, 10), 400);
            } else {
                _memState.round++;
                _memState.phase = 'watch';
                setTimeout(_memNextRound, 700);
            }
        }
    }

    async function _submitMemory(token, seq, correct) {
        try {
            const res = await apiCall('/api/ticket-games/memory/submit', {
                method: 'POST',
                body: JSON.stringify({token, sequence: seq.slice(0, correct)})
            });
            const won = res.tickets_won;
            const c = res.correct;
            openArcade(`<div style="text-align:center;padding:10px;">
                <div style="font-size:52px;margin-bottom:10px;">${c >= 8 ? '🏆' : c >= 4 ? '🧠' : '😅'}</div>
                <div style="font-size:28px;font-weight:700;margin-bottom:6px;">${c}/10</div>
                <div style="color:#888;font-size:13px;margin-bottom:14px;">Rounds completed</div>
                ${won > 0
                    ? `<div style="font-size:18px;color:#4caf50;font-weight:700;margin-bottom:6px;">+${won} tickets won!</div>`
                    : `<div style="color:#888;margin-bottom:6px;">Complete more rounds to win tickets</div>`}
                <div style="color:#555;font-size:11px;margin-bottom:20px;">8+=6🎟 · 6+=4🎟 · 4+=3🎟 · 2+=1🎟</div>
                <div style="display:flex;gap:10px;justify-content:center;">
                    <button class="btn btn-primary" onclick="startMemoryGame()">Play Again (1 🎟️)</button>
                    <button class="btn" onclick="closeArcade()">Done</button>
                </div>
            </div>`);
            if (won > 0) { loadTicketBalance(); spawnConfetti('Blue', 40); }
        } catch(e) { showToast(e.message || 'Error', 'error'); closeArcade(); }
    }

    console.log('🎰 CS2CaseBot Dashboard Loaded!');
    console.log('🎮 Games: Coinflip, 3D Dice, Mines, Slots');
    console.log('⭐ Features: Case Opening, Stickers, Trade-Ups, Quests, Achievements');
    console.log('🎯 Premium Goals: $500 or 1000 users unlocks Premium!');
    console.log('🔧 All bugs fixed! Clean organized code!');

    // ============================================
    // SECTION 100: PWA INSTALL BANNER
    // ============================================
    var _deferredInstallPrompt = null;

    function _isInstalled() {
        return window.matchMedia('(display-mode: standalone)').matches ||
               navigator.standalone === true;
    }

    function showPwaBanner() {
        if (_isInstalled()) return;
        if (localStorage.getItem('pwa_dismissed') === '1') return;

        var isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) && !window.MSStream;
        var msg = document.getElementById('pwaBannerMsg');
        var actions = document.getElementById('pwaBannerActions');

        if (isIOS) {
            msg.innerHTML = 'In Safari tap the <strong style="color:#ffd700">Share &#x2B06;</strong> button, then <strong style="color:#ffd700">"Add to Home Screen"</strong>';
            actions.innerHTML = '';
        } else if (_deferredInstallPrompt) {
            msg.textContent = 'Install the app for a faster, full-screen experience.';
            actions.innerHTML = '<button onclick="triggerPwaInstall()" style="background:#ffd700;color:#0a0a0f;border:none;padding:9px 28px;border-radius:6px;font-family:Orbitron,sans-serif;font-weight:700;font-size:13px;cursor:pointer;letter-spacing:0.5px;">Install App</button>';
        } else {
            return;
        }

        document.getElementById('pwaInstallBanner').style.display = 'block';
    }

    function triggerPwaInstall() {
        if (!_deferredInstallPrompt) return;
        _deferredInstallPrompt.prompt();
        _deferredInstallPrompt.userChoice.then(function(r) {
            if (r.outcome === 'accepted') dismissPwaBanner();
            _deferredInstallPrompt = null;
        });
    }

    function dismissPwaBanner() {
        document.getElementById('pwaInstallBanner').style.display = 'none';
        localStorage.setItem('pwa_dismissed', '1');
        var tog = document.getElementById('installPromptToggle');
        if (tog) tog.checked = false;
    }

    function toggleInstallPromptSetting(enabled) {
        if (enabled) {
            localStorage.removeItem('pwa_dismissed');
            showPwaBanner();
        } else {
            localStorage.setItem('pwa_dismissed', '1');
            document.getElementById('pwaInstallBanner').style.display = 'none';
        }
    }

    window.addEventListener('beforeinstallprompt', function(e) {
        e.preventDefault();
        _deferredInstallPrompt = e;
        showPwaBanner();
    });

    window.addEventListener('appinstalled', function() {
        document.getElementById('pwaInstallBanner').style.display = 'none';
    });

    window.addEventListener('load', function() {
        // Show banner on iOS (beforeinstallprompt never fires there)
        var isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) && !window.MSStream;
        if (isIOS) setTimeout(showPwaBanner, 2500);

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch(function() {});
        }
    });
    
