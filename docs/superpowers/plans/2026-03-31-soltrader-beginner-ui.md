# SolTrader Beginner UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement milestone-based progressive disclosure with guided onboarding, dropdown menu for advanced tabs, and contextual tooltips. Preserve rollback to original 9-tab UI via feature flag.

**Architecture:**
- Feature flags control UI mode (beginner vs original)
- Milestone tracker (local storage) monitors: wallet connected, bot started, first trade executed, first position closed
- Conditional rendering in Flask templates based on milestone state
- Tooltip system uses first-encounter detection + Learn More modals
- Advanced menu hidden by default, revealed after first position close

**Tech Stack:** Flask (backend HTML), JavaScript (client-side state), Bootstrap/CSS (styling), SQLite (optional: persistence of tutorial state)

---

## File Structure

**Files to Create:**
- `static/js/milestone_tracker.js` — Tracks user progress (wallet → bot start → first trade → close)
- `static/js/tooltip_system.js` — First encounter detection, Learn More modals
- `templates/components/onboarding_modals.html` — 3 guided moment modals (bot start, first buy, first close)
- `templates/components/advanced_menu.html` — Dropdown for advanced tabs (conditional render)
- `static/css/beginner_ui.css` — Styling for modals, dropdowns, tooltips
- `tests/test_milestone_system.py` — Unit tests for milestone logic

**Files to Modify:**
- `app.py` — Add feature flag check, conditional rendering, milestone API endpoints
- `dashboard.py` — Add milestone state endpoints (GET/POST)
- `templates/dashboard.html` — Conditional tab render, modal injection, menu structure

---

## Phase 1: Feature Flag & Configuration

### Task 1: Add Feature Flag to Environment

**Files:**
- Modify: `.env` (add flag)
- Modify: `app.py` (read flag)

- [ ] **Step 1: Add flag to .env**

```bash
# .env
ENABLE_BEGINNER_UI=true
ENABLE_TOOLTIPS=true
```

- [ ] **Step 2: Add flag reading to app.py (line ~95, after load_environment())**

```python
# In app.py, after load_environment() and before require_env calls:

ENABLE_BEGINNER_UI = os.getenv("ENABLE_BEGINNER_UI", "true").lower() == "true"
ENABLE_TOOLTIPS = os.getenv("ENABLE_TOOLTIPS", "true").lower() == "true"
```

- [ ] **Step 3: Verify flags are readable**

Run: `python -c "from app import ENABLE_BEGINNER_UI, ENABLE_TOOLTIPS; print(f'Beginner UI: {ENABLE_BEGINNER_UI}, Tooltips: {ENABLE_TOOLTIPS}')"`
Expected: `Beginner UI: True, Tooltips: True`

- [ ] **Step 4: Commit**

```bash
git add .env app.py
git commit -m "feat: add feature flags for beginner UI and tooltips"
```

---

## Phase 2: Milestone Tracking System

### Task 2: Create Milestone Tracker JavaScript

**Files:**
- Create: `static/js/milestone_tracker.js`

- [ ] **Step 1: Create milestone_tracker.js with initialization**

```javascript
// static/js/milestone_tracker.js

class MilestoneTracker {
  constructor() {
    this.storageKey = 'soltrader_milestones';
    this.milestones = this.load();
  }

  load() {
    const stored = localStorage.getItem(this.storageKey);
    return stored ? JSON.parse(stored) : {
      wallet_connected: false,
      bot_started: false,
      first_trade_executed: false,
      first_position_closed: false,
      tooltip_dismissed_elements: {},
    };
  }

  save() {
    localStorage.setItem(this.storageKey, JSON.stringify(this.milestones));
  }

  markWalletConnected() {
    if (!this.milestones.wallet_connected) {
      this.milestones.wallet_connected = true;
      this.save();
      console.log('[Milestone] Wallet connected');
    }
  }

  markBotStarted() {
    if (!this.milestones.bot_started) {
      this.milestones.bot_started = true;
      this.save();
      console.log('[Milestone] Bot started');
      window.dispatchEvent(new CustomEvent('milestone:botStarted'));
    }
  }

  markFirstTradeExecuted() {
    if (!this.milestones.first_trade_executed) {
      this.milestones.first_trade_executed = true;
      this.save();
      console.log('[Milestone] First trade executed');
      window.dispatchEvent(new CustomEvent('milestone:firstTradeExecuted'));
    }
  }

  markFirstPositionClosed() {
    if (!this.milestones.first_position_closed) {
      this.milestones.first_position_closed = true;
      this.save();
      console.log('[Milestone] First position closed');
      window.dispatchEvent(new CustomEvent('milestone:firstPositionClosed'));
    }
  }

  isUnlockAdvancedMenu() {
    return this.milestones.first_position_closed;
  }

  dismissTooltip(elementId) {
    if (!this.milestones.tooltip_dismissed_elements[elementId]) {
      this.milestones.tooltip_dismissed_elements[elementId] = true;
      this.save();
    }
  }

  isTooltipDismissed(elementId) {
    return this.milestones.tooltip_dismissed_elements[elementId] || false;
  }

  reset() {
    localStorage.removeItem(this.storageKey);
    this.milestones = this.load();
    console.log('[Milestone] All milestones reset');
  }
}

const milestoneTracker = new MilestoneTracker();
```

- [ ] **Step 2: Verify syntax and load in browser console**

Create test file: `tests/test_milestone_tracker.html` (optional, for manual testing)
```html
<!DOCTYPE html>
<html>
<head><title>Milestone Tracker Test</title></head>
<body>
  <script src="/static/js/milestone_tracker.js"></script>
  <script>
    console.log('Initial milestones:', milestoneTracker.milestones);
    milestoneTracker.markWalletConnected();
    console.log('After wallet connected:', milestoneTracker.milestones);
  </script>
</body>
</html>
```

- [ ] **Step 3: Commit**

```bash
git add static/js/milestone_tracker.js
git commit -m "feat: add milestone tracking system with localStorage persistence"
```

---

### Task 3: Add Milestone API Endpoints to app.py

**Files:**
- Modify: `app.py` (add routes)

- [ ] **Step 1: Add milestone endpoints after dashboard route (around line 9800)**

```python
# In app.py, after the main dashboard route:

@app.route("/api/milestones", methods=["GET"])
def get_milestones():
    """Return current milestone state for beginner UI"""
    if not ENABLE_BEGINNER_UI:
        return jsonify({"beginner_ui_enabled": False})
    return jsonify({
        "beginner_ui_enabled": True,
        "message": "Client reads milestones from localStorage"
    })

@app.route("/api/milestones/bot-started", methods=["POST"])
def mark_bot_started():
    """Endpoint to mark bot as started (called by JS when user clicks Start Bot)"""
    return jsonify({"status": "success", "milestone": "bot_started"})

@app.route("/api/milestones/trade-executed", methods=["POST"])
def mark_trade_executed():
    """Endpoint to mark first trade executed"""
    return jsonify({"status": "success", "milestone": "trade_executed"})

@app.route("/api/milestones/position-closed", methods=["POST"])
def mark_position_closed():
    """Endpoint to mark first position closed"""
    return jsonify({"status": "success", "milestone": "position_closed"})

@app.route("/api/milestones/reset", methods=["POST"])
def reset_milestones():
    """Reset all milestones (for testing)"""
    return jsonify({"status": "success", "message": "Client should call milestoneTracker.reset()"})
```

- [ ] **Step 2: Test endpoints using curl**

```bash
curl -X GET http://localhost:5000/api/milestones
curl -X POST http://localhost:5000/api/milestones/bot-started
```

Expected: JSON response with status "success"

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "feat: add milestone API endpoints for bot start/trade/position close"
```

---

## Phase 3: Tab Reorganization & Dropdown Menu

### Task 4: Create Advanced Menu Component

**Files:**
- Create: `templates/components/advanced_menu.html`

- [ ] **Step 1: Create advanced_menu.html**

```html
<!-- templates/components/advanced_menu.html -->
<!-- Dropdown menu for advanced tabs (hidden until first position closed) -->

<div id="advanced-menu-wrapper" style="display: none;">
  <div class="advanced-menu-container">
    <button class="menu-toggle" onclick="toggleAdvancedMenu()">
      📁 Advanced Tools <span class="menu-indicator">▼</span>
    </button>
    <div id="advanced-menu-dropdown" class="advanced-menu-dropdown" style="display: none;">
      <button class="tab-btn" data-tab="signals" onclick="activateTab('signals')">
        <span class="tab-btn-label">📡 Signals</span>
        <span class="tab-btn-meta">Why tokens passed or failed</span>
      </button>
      <button class="tab-btn" data-tab="whales" onclick="activateTab('whales')">
        <span class="tab-btn-label">🐳 Whales</span>
        <span class="tab-btn-meta">Tracked smart money flow</span>
      </button>
      <button class="tab-btn" data-tab="quant" onclick="activateTab('quant')">
        <span class="tab-btn-label">🧮 Quant</span>
        <span class="tab-btn-meta">Quantum backtester & strategy</span>
      </button>
      <button class="tab-btn" data-tab="portfolio" onclick="activateTab('portfolio')">
        <span class="tab-btn-label">📊 My Coins</span>
        <span class="tab-btn-meta">Portfolio & trade history</span>
      </button>
      <button class="tab-btn" data-tab="paper" onclick="activateTab('paper')">
        <span class="tab-btn-label">📋 Paper</span>
        <span class="tab-btn-meta">Simulated trades, no real money</span>
      </button>
    </div>
  </div>
</div>

<style>
.advanced-menu-container {
  position: relative;
  display: inline-block;
}
.menu-toggle {
  padding: 10px 16px;
  background: rgba(47,107,255,.1);
  border: 1px solid rgba(47,107,255,.3);
  border-radius: 8px;
  color: var(--t1);
  cursor: pointer;
  font-weight: 600;
  transition: all .2s;
}
.menu-toggle:hover {
  background: rgba(47,107,255,.15);
  border-color: rgba(47,107,255,.5);
}
.menu-indicator {
  font-size: 10px;
  margin-left: 8px;
  transition: transform .2s;
}
.advanced-menu-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  margin-top: 8px;
  background: rgba(10,20,40,.95);
  border: 1px solid rgba(47,107,255,.3);
  border-radius: 8px;
  padding: 8px;
  min-width: 220px;
  box-shadow: 0 8px 32px rgba(0,0,0,.3);
  z-index: 100;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.advanced-menu-dropdown .tab-btn {
  text-align: left;
  margin: 0;
  padding: 10px 12px;
  font-size: 13px;
  border-radius: 4px;
  border: none;
}
</style>

<script>
function toggleAdvancedMenu() {
  const dropdown = document.getElementById('advanced-menu-dropdown');
  const isVisible = dropdown.style.display !== 'none';
  dropdown.style.display = isVisible ? 'none' : 'flex';
}

// Close menu when clicking outside
document.addEventListener('click', function(event) {
  const menu = document.querySelector('.advanced-menu-container');
  if (menu && !menu.contains(event.target)) {
    document.getElementById('advanced-menu-dropdown').style.display = 'none';
  }
});
</script>
```

- [ ] **Step 2: Commit**

```bash
git add templates/components/advanced_menu.html
git commit -m "feat: add advanced menu dropdown component (hidden until unlock)"
```

---

### Task 5: Modify Dashboard Tab Bar for Beginner UI

**Files:**
- Modify: `app.py` (find dashboard HTML, modify tab rendering)

- [ ] **Step 1: Find current tab bar in app.py (search for "data-tab="portfolio")**

The current tabs section is around line 15200. Locate the `<button class="tab-btn"` section.

- [ ] **Step 2: Replace tab bar with conditional rendering**

Find this section in app.py (around line 15200-15208):
```python
    <button class="tab-btn" data-tab="portfolio" onclick="activateTab('portfolio')"><span class="tab-btn-label">My Coins</span><span class="tab-btn-meta">Portfolio &amp; trade history</span></button>
    <button class="tab-btn active" data-tab="scanner" onclick="activateTab('scanner')"><span class="tab-btn-label">Scan</span><span class="tab-btn-meta">Evaluation ramp &amp; approved coins</span></button>
    ...
```

Replace with:
```python
    <!-- Main tabs (always visible) -->
    <button class="tab-btn" data-tab="scanner" onclick="activateTab('scanner')"><span class="tab-btn-label">🔍 Scan</span><span class="tab-btn-meta">Real-time coin evaluation</span></button>
    <button class="tab-btn" data-tab="positions" onclick="activateTab('positions')"><span class="tab-btn-label">📈 Positions</span><span class="tab-btn-meta">Open trades and history</span></button>
    <button class="tab-btn" data-tab="settings" onclick="activateTab('settings')"><span class="tab-btn-label">⚙️ Settings</span><span class="tab-btn-meta">Wallet and bot controls</span></button>
    <button class="tab-btn" data-tab="pnl" onclick="activateTab('pnl')"><span class="tab-btn-label">💹 P&L</span><span class="tab-btn-meta">Performance and equity curve</span></button>

    <!-- Advanced menu (conditionally visible) -->
    <div id="advanced-menu-wrapper" style="display: none;">
      <div class="advanced-menu-container">
        <button class="menu-toggle" onclick="toggleAdvancedMenu()">
          📁 Advanced Tools <span class="menu-indicator">▼</span>
        </button>
        <div id="advanced-menu-dropdown" class="advanced-menu-dropdown" style="display: none;">
          <button class="tab-btn" data-tab="signals" onclick="activateTab('signals')">
            <span class="tab-btn-label">📡 Signals</span>
            <span class="tab-btn-meta">Why tokens passed or failed</span>
          </button>
          <button class="tab-btn" data-tab="whales" onclick="activateTab('whales')">
            <span class="tab-btn-label">🐳 Whales</span>
            <span class="tab-btn-meta">Tracked smart money flow</span>
          </button>
          <button class="tab-btn" data-tab="quant" onclick="activateTab('quant')">
            <span class="tab-btn-label">🧮 Quant</span>
            <span class="tab-btn-meta">Quantum backtester & strategy</span>
          </button>
          <button class="tab-btn" data-tab="portfolio" onclick="activateTab('portfolio')">
            <span class="tab-btn-label">📊 My Coins</span>
            <span class="tab-btn-meta">Portfolio & trade history</span>
          </button>
          <button class="tab-btn" data-tab="paper" onclick="activateTab('paper')">
            <span class="tab-btn-label">📋 Paper</span>
            <span class="tab-btn-meta">Simulated trades, no real money</span>
          </button>
        </div>
      </div>
    </div>
```

- [ ] **Step 3: Add JavaScript to show/hide advanced menu based on milestone**

Add this script in the dashboard HTML (after milestone_tracker.js is loaded):
```javascript
// Show advanced menu if first position closed
document.addEventListener('DOMContentLoaded', function() {
  const advancedMenuWrapper = document.getElementById('advanced-menu-wrapper');
  if (milestoneTracker.isUnlockAdvancedMenu()) {
    advancedMenuWrapper.style.display = 'block';
  }

  // Listen for position close milestone
  window.addEventListener('milestone:firstPositionClosed', function() {
    advancedMenuWrapper.style.display = 'block';
    console.log('[UI] Advanced menu unlocked');
  });
});
```

- [ ] **Step 4: Test in browser**

Navigate to dashboard. Verify:
- Main 4 tabs visible
- Advanced menu NOT visible
- Console shows no errors

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "feat: implement beginner UI tab bar with hidden advanced menu"
```

---

## Phase 4: Guided Moments (Onboarding Modals)

### Task 6: Create Onboarding Modals

**Files:**
- Create: `templates/components/onboarding_modals.html`

- [ ] **Step 1: Create onboarding_modals.html**

```html
<!-- templates/components/onboarding_modals.html -->
<!-- Three guided moments for first-time users -->

<!-- Modal: Bot Start -->
<div id="modal-bot-start" class="onboarding-modal" style="display: none;">
  <div class="modal-overlay" onclick="closeModal('modal-bot-start')"></div>
  <div class="modal-content">
    <button class="modal-close" onclick="closeModal('modal-bot-start')">×</button>
    <h2>Bot is Now Monitoring Coins</h2>
    <p class="modal-subtitle">Real-time evaluation of all new Solana coins</p>

    <div class="modal-visualization">
      <div class="viz-stage">
        <span class="viz-label">Monitoring</span>
        <span class="viz-count" id="viz-monitoring">47</span> coins
      </div>
      <span class="viz-arrow">→</span>
      <div class="viz-stage">
        <span class="viz-label">Screening</span>
        <span class="viz-count" id="viz-screening">12</span> evaluated
      </div>
      <span class="viz-arrow">→</span>
      <div class="viz-stage">
        <span class="viz-label">Approved</span>
        <span class="viz-count" id="viz-approved">3</span> ready
      </div>
    </div>

    <p class="modal-text">
      Your bot will automatically place buy orders on coins that pass your safety filters.
      It will then auto-sell when targets are hit or stop loss triggers.
    </p>

    <button class="btn-primary" onclick="closeModal('modal-bot-start')">Got it</button>
  </div>
</div>

<!-- Modal: First Buy -->
<div id="modal-first-buy" class="onboarding-modal" style="display: none;">
  <div class="modal-overlay" onclick="closeModal('modal-first-buy')"></div>
  <div class="modal-content">
    <button class="modal-close" onclick="closeModal('modal-first-buy')">×</button>
    <h2 id="first-buy-coin-name">First Position Opened</h2>
    <p class="modal-subtitle">Bot executed a buy order</p>

    <div class="modal-position-info">
      <div class="info-row">
        <span class="info-label">Entry Price:</span>
        <span class="info-value" id="first-buy-entry">$0.23</span>
      </div>
      <div class="info-row">
        <span class="info-label">Position Size:</span>
        <span class="info-value" id="first-buy-size">0.05 SOL</span>
      </div>
      <div class="info-row">
        <span class="info-label">Target 1 (TP1):</span>
        <span class="info-value" id="first-buy-tp1" style="color: #14c784;">$0.35 (+50%)</span>
      </div>
      <div class="info-row">
        <span class="info-label">Target 2 (TP2):</span>
        <span class="info-value" id="first-buy-tp2" style="color: #3b82f6;">$2.30 (+900%)</span>
      </div>
      <div class="info-row">
        <span class="info-label">Stop Loss:</span>
        <span class="info-value" id="first-buy-sl" style="color: #f87171;">$0.17 (−25%)</span>
      </div>
    </div>

    <p class="modal-text">
      The bot bought this coin because it passed all your safety screens.
      It's now watching the price and will auto-sell when one of the targets is hit.
    </p>

    <button class="btn-primary" onclick="closeModalAndGoTo('modal-first-buy', 'positions')">Watch Live in Positions Tab</button>
  </div>
</div>

<!-- Modal: First Close -->
<div id="modal-first-close" class="onboarding-modal" style="display: none;">
  <div class="modal-overlay" onclick="closeModal('modal-first-close')"></div>
  <div class="modal-content">
    <button class="modal-close" onclick="closeModal('modal-first-close')">×</button>
    <h2>First Trade Complete</h2>
    <p class="modal-subtitle" id="first-close-result">Trade closed with a profit</p>

    <div class="modal-trade-result">
      <div class="result-coin">
        <span id="first-close-coin">DOGE</span>
      </div>
      <div class="result-value" id="first-close-pnl" style="color: #14c784;">+$12</div>
      <div class="result-pct" id="first-close-pct" style="color: #14c784;">+45%</div>
    </div>

    <p class="modal-text" id="first-close-explanation">
      Hit TP1 target at $0.35. Your bot auto-closed the position and locked in the profit.
    </p>

    <div class="modal-progress">
      <p><strong>🎉 Advanced Features Unlocked!</strong></p>
      <p>You've completed your first trade. The Advanced Tools menu is now available with:</p>
      <ul>
        <li>📡 <strong>Signals</strong> — Understand why this coin was selected</li>
        <li>🐳 <strong>Whales</strong> — See what smart money is doing</li>
        <li>🧮 <strong>Quant</strong> — Backtest and optimize strategies</li>
        <li>📊 <strong>My Coins</strong> — View your full trading history</li>
        <li>📋 <strong>Paper</strong> — Practice risk-free</li>
      </ul>
    </div>

    <button class="btn-primary" onclick="closeModal('modal-first-close')">Explore Advanced Tools</button>
  </div>
</div>

<style>
.onboarding-modal {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0,0,0,.7);
  cursor: pointer;
}
.modal-content {
  position: relative;
  background: linear-gradient(135deg, rgba(13,23,38,.98), rgba(8,16,26,.98));
  border: 1px solid rgba(47,107,255,.3);
  border-radius: 16px;
  padding: 32px;
  max-width: 500px;
  box-shadow: 0 20px 60px rgba(0,0,0,.5);
  animation: modalSlideIn .3s ease;
}
@keyframes modalSlideIn {
  from { opacity: 0; transform: translateY(-20px); }
  to { opacity: 1; transform: translateY(0); }
}
.modal-close {
  position: absolute;
  top: 16px;
  right: 16px;
  width: 32px;
  height: 32px;
  background: none;
  border: none;
  color: var(--t2);
  font-size: 24px;
  cursor: pointer;
  transition: color .2s;
}
.modal-close:hover {
  color: var(--t1);
}
.modal-content h2 {
  margin-top: 0;
  color: var(--t1);
  font-size: 24px;
}
.modal-subtitle {
  color: var(--t3);
  font-size: 13px;
  margin-top: -8px;
}
.modal-visualization {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 20px 0;
  padding: 16px;
  background: rgba(47,107,255,.1);
  border-radius: 8px;
}
.viz-stage {
  flex: 1;
  text-align: center;
  padding: 12px;
  background: rgba(10,20,40,.8);
  border-radius: 4px;
  border: 1px solid rgba(47,107,255,.2);
}
.viz-label {
  display: block;
  font-size: 10px;
  color: var(--t3);
  text-transform: uppercase;
  margin-bottom: 4px;
}
.viz-count {
  display: block;
  font-size: 20px;
  font-weight: bold;
  color: #3b82f6;
}
.viz-arrow {
  color: var(--t3);
  font-weight: bold;
}
.modal-text {
  color: var(--t2);
  font-size: 13px;
  line-height: 1.6;
  margin: 16px 0;
}
.modal-position-info {
  background: rgba(47,107,255,.08);
  border-left: 3px solid #3b82f6;
  padding: 16px;
  border-radius: 4px;
  margin: 16px 0;
}
.info-row {
  display: flex;
  justify-content: space-between;
  padding: 6px 0;
  border-bottom: 1px solid rgba(47,107,255,.1);
  font-size: 13px;
}
.info-row:last-child {
  border-bottom: none;
}
.info-label {
  color: var(--t3);
}
.info-value {
  color: var(--t1);
  font-weight: 600;
}
.modal-trade-result {
  text-align: center;
  padding: 20px;
  background: rgba(20,199,132,.08);
  border: 1px solid rgba(20,199,132,.2);
  border-radius: 8px;
  margin: 16px 0;
}
.result-coin {
  font-size: 24px;
  font-weight: bold;
  color: var(--t1);
  margin-bottom: 8px;
}
.result-value {
  font-size: 32px;
  font-weight: bold;
  margin-bottom: 4px;
}
.result-pct {
  font-size: 18px;
}
.modal-progress {
  background: rgba(168,85,247,.08);
  border: 1px solid rgba(168,85,247,.2);
  border-radius: 8px;
  padding: 16px;
  margin-top: 20px;
}
.modal-progress p {
  margin: 8px 0;
  color: var(--t2);
  font-size: 13px;
}
.modal-progress ul {
  margin: 12px 0;
  padding-left: 20px;
  color: var(--t2);
  font-size: 12px;
}
.modal-progress li {
  margin-bottom: 6px;
}
.btn-primary {
  width: 100%;
  padding: 12px;
  background: #3b82f6;
  color: white;
  border: none;
  border-radius: 8px;
  font-weight: 600;
  cursor: pointer;
  margin-top: 20px;
  transition: background .2s;
}
.btn-primary:hover {
  background: #2563eb;
}
</style>

<script>
function closeModal(modalId) {
  document.getElementById(modalId).style.display = 'none';
}

function closeModalAndGoTo(modalId, tab) {
  closeModal(modalId);
  activateTab(tab);
}

// Listen for milestone events and show modals
window.addEventListener('milestone:botStarted', function() {
  document.getElementById('modal-bot-start').style.display = 'flex';
});

window.addEventListener('milestone:firstTradeExecuted', function() {
  document.getElementById('modal-first-buy').style.display = 'flex';
});

window.addEventListener('milestone:firstPositionClosed', function() {
  document.getElementById('modal-first-close').style.display = 'flex';
});

// Update modal with trade data when position closes
function updateFirstCloseModal(coinName, entryPrice, exitPrice, pnlDollars, pnlPercent, closeReason) {
  document.getElementById('first-close-coin').textContent = coinName;
  document.getElementById('first-close-pnl').textContent = (pnlDollars >= 0 ? '+' : '') + '$' + pnlDollars.toFixed(2);
  document.getElementById('first-close-pct').textContent = (pnlPercent >= 0 ? '+' : '') + pnlPercent.toFixed(1) + '%';

  const pnlElement = document.getElementById('first-close-pnl');
  const pctElement = document.getElementById('first-close-pct');
  if (pnlDollars >= 0) {
    pnlElement.style.color = '#14c784';
    pctElement.style.color = '#14c784';
  } else {
    pnlElement.style.color = '#f87171';
    pctElement.style.color = '#f87171';
  }

  const explanation = closeReason === 'tp1' ? `Hit TP1 target at $${exitPrice}` :
                      closeReason === 'tp2' ? `Hit TP2 target at $${exitPrice}` :
                      `Hit stop loss at $${exitPrice}`;
  document.getElementById('first-close-explanation').textContent = explanation;
}
</script>
```

- [ ] **Step 2: Inject modals into dashboard.html**

Add this line in the dashboard HTML template (before closing `</body>`):
```python
{% if ENABLE_BEGINNER_UI %}
  <!-- Include onboarding modals -->
  {% include "components/onboarding_modals.html" %}
{% endif %}
```

- [ ] **Step 3: Test in browser**

Navigate to dashboard. Verify modals don't show yet (milestones not triggered).

- [ ] **Step 4: Commit**

```bash
git add templates/components/onboarding_modals.html app.py
git commit -m "feat: add onboarding modals for bot start, first buy, first close"
```

---

## Phase 5: Tooltip System

### Task 7: Create Tooltip System JavaScript

**Files:**
- Create: `static/js/tooltip_system.js`

- [ ] **Step 1: Create tooltip_system.js**

```javascript
// static/js/tooltip_system.js

class TooltipSystem {
  constructor() {
    this.isFirstEncounter = (elementId) => !milestoneTracker.isTooltipDismissed(elementId);
  }

  initTooltips() {
    // Find all elements with data-tooltip attribute
    document.querySelectorAll('[data-tooltip]').forEach(element => {
      const tooltipId = element.id || element.getAttribute('data-tooltip-id');

      // First encounter: auto-show on hover
      if (this.isFirstEncounter(tooltipId)) {
        element.addEventListener('mouseenter', () => this.showTooltip(element, tooltipId));
      } else {
        // Subsequent: only on hover, only show brief tooltip
        element.addEventListener('mouseenter', () => this.showTooltipOnHover(element));
      }
    });
  }

  showTooltip(element, tooltipId) {
    // Show tooltip with "Learn More" button
    const tooltip = this.createTooltipElement(
      element.getAttribute('data-tooltip'),
      element.getAttribute('data-tooltip-id'),
      true  // isFirstEncounter
    );
    document.body.appendChild(tooltip);
    this.positionTooltip(tooltip, element);

    // Auto-dismiss on click elsewhere
    const closeHandler = (e) => {
      if (!tooltip.contains(e.target) && e.target !== element) {
        tooltip.remove();
        document.removeEventListener('click', closeHandler);
      }
    };
    document.addEventListener('click', closeHandler);
  }

  showTooltipOnHover(element) {
    // Show brief tooltip only
    const tooltip = this.createTooltipElement(
      element.getAttribute('data-tooltip'),
      element.getAttribute('data-tooltip-id'),
      false  // not first encounter
    );
    document.body.appendChild(tooltip);
    this.positionTooltip(tooltip, element);

    element.addEventListener('mouseleave', () => {
      tooltip.remove();
    });
  }

  createTooltipElement(text, tooltipId, isFirstEncounter) {
    const div = document.createElement('div');
    div.className = 'tooltip-popup';
    div.innerHTML = `
      <div class="tooltip-text">${text}</div>
      ${isFirstEncounter ? `
        <button class="tooltip-learn-more" onclick="tooltipSystem.openLearnMore('${tooltipId}')">
          Learn More →
        </button>
        <button class="tooltip-dismiss" onclick="this.closest('.tooltip-popup').remove(); milestoneTracker.dismissTooltip('${tooltipId}')">
          Got it
        </button>
      ` : `
        <div class="tooltip-hint" style="font-size: 10px; color: rgba(255,255,255,.5); margin-top: 6px;">
          Click "Learn More" for details
        </div>
      `}
    `;
    return div;
  }

  positionTooltip(tooltip, element) {
    const rect = element.getBoundingClientRect();
    tooltip.style.position = 'fixed';
    tooltip.style.top = (rect.bottom + 8) + 'px';
    tooltip.style.left = rect.left + 'px';
  }

  openLearnMore(tooltipId) {
    const learnMoreContent = this.getLearnMoreContent(tooltipId);
    const panel = this.createLearnMorePanel(tooltipId, learnMoreContent);
    document.body.appendChild(panel);
  }

  createLearnMorePanel(tooltipId, content) {
    const div = document.createElement('div');
    div.className = 'learn-more-panel';
    div.innerHTML = `
      <div class="learn-more-overlay" onclick="this.parentElement.remove()"></div>
      <div class="learn-more-content">
        <button class="learn-more-close" onclick="this.closest('.learn-more-panel').remove()">×</button>
        <div class="learn-more-body">
          ${content}
        </div>
      </div>
    `;
    return div;
  }

  getLearnMoreContent(tooltipId) {
    const content = {
      'position-pnl-pct': `
        <h3>Profit/Loss Percentage</h3>
        <p>This shows your gain or loss as a percentage of your entry price.</p>
        <p><strong>Example:</strong> If you bought at $0.23 and it's now $0.35, you're at +52%.</p>
        <p>The percentage includes the bot's friction model (slippage, fees). That's why a 1.5x price move might show 45% profit instead of 50%.</p>
        <p><a href="#" onclick="activateTab('settings')">→ Adjust friction model in Settings</a></p>
      `,
      'tp1-target': `
        <h3>Target 1 (TP1)</h3>
        <p>The first profit target. When the price hits this level, the bot can automatically sell a portion of your position to lock in gains.</p>
        <p><strong>Example:</strong> If you bought at $0.23, TP1 might be $0.35 (+52% profit).</p>
        <p>TP1 is designed to be achievable quickly, taking profits while momentum is strong.</p>
        <p><a href="#" onclick="activateTab('signals')">→ Learn why these targets are chosen in Signals tab</a></p>
      `,
      'tp2-target': `
        <h3>Target 2 (TP2)</h3>
        <p>The second (higher) profit target. This is where you catch moonshots. It's much higher than TP1, requiring more patience and luck.</p>
        <p><strong>Example:</strong> TP2 might be $2.30 (10x return). This happens rarely, but when it does, the gains are massive.</p>
        <p>If TP1 fills, you still keep your remaining position riding toward TP2.</p>
      `,
      'stop-loss': `
        <h3>Stop Loss</h3>
        <p>The price level where the bot auto-exits to prevent catastrophic losses.</p>
        <p><strong>Example:</strong> If you bought at $0.23, stop loss might be $0.17 (−25% loss).</p>
        <p>This is your safety net. It locks in risk so you know your max loss upfront.</p>
        <p><strong>Why 25%?</strong> It's aggressive enough to catch volatility while protecting your capital.</p>
      `,
      'loss-limit': `
        <h3>Loss Limit</h3>
        <p>The total amount of SOL you're willing to lose in a single session before the bot auto-pauses.</p>
        <p><strong>Example:</strong> If you set Loss Limit to −$50, the bot stops trading after losing $50 total.</p>
        <p>This is a session-level circuit breaker. It prevents you from digging deeper after a bad day.</p>
      `,
      'profit-goal': `
        <h3>Profit Goal</h3>
        <p>When your session reaches this profit target, the bot stops trading automatically.</p>
        <p><strong>Example:</strong> Profit Goal of +$500 means the bot pauses once you've made $500 in session profit.</p>
        <p>This locks in wins and prevents over-trading on winning days.</p>
      `,
      'whale-alert': `
        <h3>Whale Alert</h3>
        <p>Smart money detected on this coin. A "whale" (large holder or trading bot) just entered or exited this token.</p>
        <p><strong>Why it matters:</strong> Whales move the market. When they buy early, it's often a strong signal. When they exit, it can trigger a dump.</p>
        <p><a href="#" onclick="activateTab('whales')">→ See all whale activity in the Whales tab</a></p>
      `,
    };
    return content[tooltipId] || '<p>No additional information available.</p>';
  }
}

const tooltipSystem = new TooltipSystem();

// Initialize tooltips when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
  tooltipSystem.initTooltips();
});
```

- [ ] **Step 2: Add CSS for tooltips**

Create `static/css/tooltips.css`:
```css
/* static/css/tooltips.css */

.tooltip-popup {
  position: fixed;
  background: rgba(10,20,40,.95);
  border: 1px solid rgba(47,107,255,.4);
  border-radius: 8px;
  padding: 12px;
  max-width: 220px;
  z-index: 500;
  box-shadow: 0 8px 24px rgba(0,0,0,.4);
  animation: tooltipFadeIn .15s ease;
}
@keyframes tooltipFadeIn {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}
.tooltip-text {
  color: var(--t2);
  font-size: 12px;
  line-height: 1.5;
  margin-bottom: 8px;
}
.tooltip-learn-more {
  display: block;
  width: 100%;
  padding: 6px;
  background: rgba(59,130,246,.2);
  border: 1px solid rgba(59,130,246,.3);
  color: #3b82f6;
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  margin-bottom: 6px;
  transition: all .15s;
}
.tooltip-learn-more:hover {
  background: rgba(59,130,246,.3);
  border-color: rgba(59,130,246,.5);
}
.tooltip-dismiss {
  display: block;
  width: 100%;
  padding: 6px;
  background: rgba(255,255,255,.05);
  border: 1px solid rgba(255,255,255,.1);
  color: var(--t3);
  border-radius: 4px;
  font-size: 11px;
  cursor: pointer;
  transition: all .15s;
}
.tooltip-dismiss:hover {
  background: rgba(255,255,255,.1);
  border-color: rgba(255,255,255,.2);
  color: var(--t2);
}

.learn-more-panel {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.learn-more-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0,0,0,.6);
  cursor: pointer;
}
.learn-more-content {
  position: relative;
  background: linear-gradient(135deg, rgba(13,23,38,.98), rgba(8,16,26,.98));
  border: 1px solid rgba(47,107,255,.3);
  border-radius: 12px;
  max-width: 500px;
  max-height: 70vh;
  overflow-y: auto;
  padding: 24px;
  box-shadow: 0 20px 60px rgba(0,0,0,.5);
}
.learn-more-close {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 28px;
  height: 28px;
  background: none;
  border: none;
  color: var(--t2);
  font-size: 20px;
  cursor: pointer;
}
.learn-more-body {
  color: var(--t2);
  font-size: 13px;
  line-height: 1.6;
}
.learn-more-body h3 {
  color: var(--t1);
  margin-top: 0;
  margin-bottom: 8px;
}
.learn-more-body p {
  margin: 8px 0;
}
.learn-more-body a {
  color: #3b82f6;
  text-decoration: none;
  cursor: pointer;
}
.learn-more-body a:hover {
  text-decoration: underline;
}
```

- [ ] **Step 3: Include tooltip files in dashboard.html**

Add before closing `</head>`:
```html
{% if ENABLE_TOOLTIPS %}
  <link rel="stylesheet" href="/static/css/tooltips.css">
{% endif %}
```

Add before closing `</body>`:
```html
{% if ENABLE_TOOLTIPS %}
  <script src="/static/js/tooltip_system.js"></script>
{% endif %}
```

- [ ] **Step 4: Add data-tooltip attributes to key elements**

Find key stats in dashboard and add attributes:
```html
<!-- Example: Position P&L -->
<div data-tooltip="Your profit/loss percentage" data-tooltip-id="position-pnl-pct">
  <span id="position-pnl-pct">+45%</span>
</div>

<!-- Example: TP1 -->
<span data-tooltip="Target price 1. Bot sells here automatically." data-tooltip-id="tp1-target">
  TP1: $0.35
</span>

<!-- Similar for TP2, Stop Loss, etc. -->
```

- [ ] **Step 5: Test in browser**

Hover over tooltipped elements. Verify:
- First hover: tooltip with "Learn More" + "Got it"
- Click "Got it": tooltip dismissed
- Second hover: tooltip shows but no "Got it" button
- Click "Learn More": side panel opens

- [ ] **Step 6: Commit**

```bash
git add static/js/tooltip_system.js static/css/tooltips.css app.py
git commit -m "feat: implement tooltip system with first-encounter detection and Learn More panels"
```

---

## Phase 6: Integration & Testing

### Task 8: Wire Up Milestone Events

**Files:**
- Modify: `app.py` (add JavaScript trigger points)

- [ ] **Step 1: Add milestone trigger in dashboard (when bot starts)**

Find the "Start Bot" button and add onclick handler:
```html
<button onclick="startBot(); milestoneTracker.markBotStarted(); setTimeout(() => window.dispatchEvent(new CustomEvent('milestone:botStarted')), 100)">
  Start Bot
</button>
```

- [ ] **Step 2: Add milestone trigger when position opens (when first buy executes)**

In the bot execution code (whenever a position is created), add:
```javascript
// After bot places a buy order
milestoneTracker.markFirstTradeExecuted();
window.dispatchEvent(new CustomEvent('milestone:firstTradeExecuted'));
```

- [ ] **Step 3: Add milestone trigger when position closes**

In the position close handler:
```javascript
// After position closes (TP1, TP2, or stop loss hit)
milestoneTracker.markFirstPositionClosed();
window.dispatchEvent(new CustomEvent('milestone:firstPositionClosed'));

// Update close modal with actual data
updateFirstCloseModal(coinName, entryPrice, exitPrice, pnlDollars, pnlPercent, closeReason);
```

- [ ] **Step 4: Test milestone flow manually**

1. Open dashboard
2. Check browser console: `milestoneTracker.milestones` should show all false
3. Click "Start Bot"
4. Check console again: `bot_started` should be true
5. Repeat for other milestones

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "feat: wire up milestone events to bot actions (start, first trade, close)"
```

---

### Task 9: Create Test Suite

**Files:**
- Create: `tests/test_milestone_system.py`

- [ ] **Step 1: Create test file**

```python
# tests/test_milestone_system.py

import pytest
import json
from app import app, ENABLE_BEGINNER_UI, ENABLE_TOOLTIPS

@pytest.fixture
def client():
    with app.test_client() as client:
        yield client

class TestMilestoneAPI:
    def test_beginner_ui_enabled(self, client):
        """Test that feature flag is readable"""
        response = client.get('/api/milestones')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'beginner_ui_enabled' in data
        assert data['beginner_ui_enabled'] == ENABLE_BEGINNER_UI

    def test_mark_bot_started(self, client):
        """Test milestone endpoint"""
        response = client.post('/api/milestones/bot-started')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['status'] == 'success'
        assert data['milestone'] == 'bot_started'

    def test_mark_trade_executed(self, client):
        """Test trade executed milestone"""
        response = client.post('/api/milestones/trade-executed')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['status'] == 'success'

    def test_mark_position_closed(self, client):
        """Test position closed milestone"""
        response = client.post('/api/milestones/position-closed')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['status'] == 'success'

    def test_reset_milestones(self, client):
        """Test milestone reset endpoint"""
        response = client.post('/api/milestones/reset')
        assert response.status_code == 200

class TestFeatureFlags:
    def test_beginner_ui_flag_exists(self):
        """Test that ENABLE_BEGINNER_UI is defined"""
        assert isinstance(ENABLE_BEGINNER_UI, bool)

    def test_tooltips_flag_exists(self):
        """Test that ENABLE_TOOLTIPS is defined"""
        assert isinstance(ENABLE_TOOLTIPS, bool)

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_milestone_system.py -v
```

Expected: All tests pass

- [ ] **Step 3: Add integration test for tab visibility**

Add to test file:
```python
class TestTabVisibility:
    def test_dashboard_loads_with_beginner_ui(self, client):
        """Test that dashboard loads when beginner UI is enabled"""
        if ENABLE_BEGINNER_UI:
            response = client.get('/dashboard')
            assert response.status_code == 200
            # Check that main tabs are in response
            assert b'data-tab="scanner"' in response.data
            assert b'data-tab="positions"' in response.data
```

- [ ] **Step 4: Commit tests**

```bash
git add tests/test_milestone_system.py
git commit -m "test: add milestone API and feature flag tests"
```

---

### Task 10: Rollback Test & Documentation

**Files:**
- Modify: `.env`
- Create: `ROLLBACK_GUIDE.md`

- [ ] **Step 1: Test rollback by disabling beginner UI**

```bash
# Edit .env
ENABLE_BEGINNER_UI=false

# Restart Flask
# Navigate to dashboard
# Verify: All 9 tabs visible, no guided modals, no advanced menu
```

- [ ] **Step 2: Re-enable and verify**

```bash
# Edit .env
ENABLE_BEGINNER_UI=true

# Restart Flask
# Navigate to dashboard
# Verify: 4 main tabs, hidden advanced menu, modals ready
```

- [ ] **Step 3: Create rollback guide**

Create `ROLLBACK_GUIDE.md`:
```markdown
# Beginner UI Rollback Guide

## Quick Rollback (< 1 minute)

If the beginner UI redesign doesn't meet expectations, rollback is instant:

### Option 1: Feature Flag (Recommended)
```bash
# In .env, change:
ENABLE_BEGINNER_UI=false
ENABLE_TOOLTIPS=false

# Restart Flask and refresh browser
# UI reverts to original 9-tab layout
```

### Option 2: Full Revert (if code changes caused issues)
```bash
# Revert to commit before redesign
git log --oneline | grep "beginner UI"
git revert <commit-hash>
# OR
git reset --hard <commit-before-redesign>
```

## What Persists After Rollback

- Milestone tracking data in localStorage (harmless, ignored by original UI)
- Tooltip dismissals (harmless, original UI has no tooltips)
- Bot performance history (untouched by UI redesign)

## Full Cleanup (if needed)
```javascript
// In browser console
localStorage.removeItem('soltrader_milestones');
localStorage.removeItem('soltrader_tooltip_dismissed');
// Refresh page
```

## Verification Checklist After Rollback

- [ ] All 9 tabs visible (My Coins, Scan, Settings, Signals, Whales, Positions, P&L, Quant, Paper)
- [ ] No "Advanced Menu" dropdown
- [ ] No onboarding modals appear
- [ ] No tooltips on hover
- [ ] Bot executes normally
- [ ] No console errors
```

- [ ] **Step 4: Commit**

```bash
git add ROLLBACK_GUIDE.md
git commit -m "docs: add rollback guide for beginner UI feature"
```

---

## Summary of Changes

**New Files Created:**
- `static/js/milestone_tracker.js` — Milestone tracking
- `static/js/tooltip_system.js` — Tooltip system
- `static/css/tooltips.css` — Tooltip styling
- `templates/components/onboarding_modals.html` — Guided moment modals
- `templates/components/advanced_menu.html` — Advanced menu dropdown
- `tests/test_milestone_system.py` — Test suite
- `ROLLBACK_GUIDE.md` — Rollback documentation

**Modified Files:**
- `.env` — Added feature flags
- `app.py` — Feature flag reading, milestone endpoints, tab rendering, modal injection
- `dashboard.html` — Conditional tab/menu rendering, milestone event wiring

**Testing:**
- Unit tests for milestone API
- Integration tests for dashboard with beginner UI
- Manual rollback verification

**Key Features:**
- ✅ Milestone-based progressive disclosure
- ✅ Guided moments (bot start, first buy, first close)
- ✅ Dropdown menu for advanced tabs
- ✅ Tooltip system with Learn More modals
- ✅ Feature flags for instant rollback
- ✅ No blockchain plugin integration yet (benchmarking phase TBD)

---

## Next Steps (Out of Scope for This Plan)

1. **Blockchain Plugin Benchmarking** — Run parallel, benchmark against existing RPC
2. **Performance Optimization** — Profile modal render times, tooltip latency
3. **Mobile Responsiveness** — Test beginner UI on tablet/phone viewports
4. **User Research** — A/B test beginner vs. original UI with real users
5. **Advanced Menu Refinement** — Based on user behavior data

---

## Notes

- All code uses existing Flask/JavaScript patterns in the codebase
- No external dependencies added
- Feature flags allow running both UIs in parallel for testing
- Tooltip content is hardcoded; consider moving to database if expansion is needed
- Milestone state is ephemeral (localStorage only, resets on logout)