# CS2Casebot — Public Showcase Copy

> **This is a stripped-down local copy, not the live project.** All real CS2 skin/weapon/sticker/agent/case image assets and the underlying skin/case data files (`skins.json`, `containers.json`, `container_contents.json`, `stickers.json`, `sticker_contents.json`) have been intentionally removed — this copy has no working asset pipeline and will not render correctly if run as-is. It exists as a starting point for a future public-facing repo, kept separate from the main private codebase (which retains all real assets). Not affiliated with Valve Corporation; all CS2 trademarks belong to their respective owners.

> **⚠️ DISCLAIMER**: This is a simulation only. All items, currencies, and values are virtual and have no real-world monetary value. No real money is required or used. This project is for entertainment purposes only.

## 🎮 About

**CS2Casebot** is a complete CS2 simulation experience with **47+ games**, **30+ cases**, and a full virtual economy – all free to play. Compete with friends, climb global leaderboards, open cases, stake virtual skins, and more.

**Play now at [cs2casebot.xyz](https://cs2casebot.xyz)**

---

## ✨ Features

### 📦 Case Opening
- **30+ CS2 Cases** – Dreams & Nightmares, Fever, Broken Fang, Riptide, eSports 2013 & more
- **Bulk Open** – Open up to 25 cases at once
- **Sticker Capsules** – Individual and bulk opening
- **Inspect Screen** – View rarity, float, condition, keep or sell
- **Session Summaries** – Track your stats

### 🎰 Casino Games

**Easy (8 Games):** Slots, CS2 Slots, Jackpot, Bomb Slots, Coinflip, Dice, Limbo, Keno

**Medium (6 Games):** Hi-Lo, Crash, Mines, Plinko, Tower, Roulette

**Hard (7 Games):** Shotgun, Slide, Russian Roulette, Ladder Climb, Mystery Box, Baccarat, Blackjack

### ⚔️ PvP Duels (5 Games)
- Dice Duel, Weapon Duel, Reaction Duel, Case Draft Duel, Elimination Coinflip
- **0% rake on most games**

### 🎲 Live Table Games (4 Games)
- Live Roulette, Live Keno Draw, Sync-Spin Slots, Live Blackjack

### 🏁 Elimination / Race Games (5 Games)
- King of the Hill Ladder, Ladder Race, Battle Royale Minefield, Mines Race, Speed Case Race

### 💎 Item Wager Games (4 Games)
- Item Jackpot, Item vs House, Item Wager Duel, Trade-Up Duel

### ⚔️ Case Battles
- PvP (FFA & teams) and PvE (3 difficulty levels)
- Stake items or cash

### 👥 Social Features
- Friends list, direct challenges, user profiles
- Global leaderboards

### 📈 Progression
- Quests, daily streaks, big-win FX

---

## 🚀 Quick Start

### Prerequisites
- Python 3.9+
- PostgreSQL
- Discord Bot Token

### Installation

```bash
# Clone the repository
git clone https://github.com/mk4gtiguy/Cs2casebot-showcase.git
cd Cs2casebot-showcase

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp env.example .env
# Edit .env with your database and Discord credentials

# Run the server
python server.py

# Run the bot
python main.py
```

---

## 🗄️ Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `DISCORD_CLIENT_ID` | Discord application ID |
| `DISCORD_CLIENT_SECRET` | Discord client secret |
| `SECRET_KEY` | Session encryption key |

---

## 🛠️ Technologies

- **Backend:** Python FastAPI
- **Database:** PostgreSQL
- **Frontend:** HTML, CSS, JavaScript
- **WebSockets:** Real-time updates
- **Discord SDK:** OAuth2 authentication

---

## 📄 License

This project is licensed under the GNU General Public License v3.0. See the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- All CS2 skin data is property of Valve Corporation

---

**Made with ❤️ by mk4gtiguy**