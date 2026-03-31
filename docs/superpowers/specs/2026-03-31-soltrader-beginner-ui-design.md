# SolTrader Beginner UI Redesign — Design Document

**Date:** 2026-03-31
**Status:** Approved for Implementation
**Objective:** Make the dashboard accessible to absolute beginners while preserving advanced trading tools through progressive disclosure and milestone-based learning.

---

## Executive Summary

Current state: 9 tabs, overwhelming for new users with no trading background.
**Solution:** Milestone-based progressive disclosure — reveal tabs and advanced features as users complete trading milestones, paired with contextual tooltips.

**Key Principles:**
1. **Start trading immediately** — Wallet setup → Start Bot → First trade (guided moments)
2. **Progressive disclosure** — Main 4 tabs visible, Advanced menu unlocks after first closed trade
3. **Transparent but focused** — Tooltips on first interaction, optional "Learn More" for deep dives
4. **No performance regression** — Blockchain plugin integrates only if benchmarks pass

---

## Part 1: Onboarding & First Trade Experience

### User Journey

**Entry Point: Wallet Connection**
- User arrives at app, clicks "Connect Wallet"
- Post-connection success state, then route directly to **Scan Tab** (no mandatory tour)

### Moment 1: Bot Starts

**Trigger:** User clicks "Start Bot" with configured settings (wallet amount, auto-stop conditions)

**Guided Moment:**
- Overlay tooltip appears with slightly zoomed view of coin evaluation
- Shows real-time monitoring: "Coins flowing in → Screening → Approved → Buy orders placed"
- Visible stats: "Monitoring 47 coins | Approved 3 | Placed orders on 2"
- Text: "Your bot will automatically place buy orders on coins that pass screening. Exits execute when TP1, TP2, or stop loss is hit."
- User clicks "Got it" to dismiss

### Moment 2: First Buy Order Fills

**Trigger:** Bot's passive buy order executes on a coin that passed screening

**Guided Moment:**
- Modal appears showing:
  - Coin name, entry price, position size
  - "Passive buy filled at $0.23. Bot is now watching for TP1 ($0.35) and TP2 ($2.30)."
  - Entry logic + whale tracking data that triggered the order
- "Watch Live" button → routes to Positions tab
- User can dismiss or navigate

### Moment 3: First Position Closes (TP1 or Stop Loss)

**Trigger:** Bot closes the position (hit target or stop loss)

**Guided Moment:**
- Modal shows exit details:
  - "First trade closed: +$12 (+45%)" or "−$8 (−15%)"
  - Why it closed: "Hit TP1 target at $0.35" or "Hit stop loss at $0.17"
  - Session totals: "Session: +$12 | Loss limit: −$50 | Profit goal: +$500"
- **Advanced Menu unlocks:** "🔓 New features unlocked! Check the menu for Signals, Whales, Quant, and more."
- Closed position moves to Session History in Positions tab

**After these 3 moments:**
- All tutorial overlays end
- User is live trading with full UI visible
- Advanced Menu is accessible

---

## Part 2: Tab Architecture (Dropdown Hybrid)

### Main Tabs (Always Visible)

1. **🔍 Scan**
   - Real-time coin evaluation pipeline
   - Shows coins being monitored, screened, approved
   - Displays: Coin name, entry conditions met, whale activity, pass/fail reason
   - Beginner tooltip: "Coins matching your safety filters"
   - Advanced feature: Blockchain risk badges (if benchmarked fast)

2. **📈 Positions**
   - Open live positions with real-time P&L
   - Session history of closed trades
   - Shows: Coin, entry price, current price, TP1/TP2/stop levels, current P&L %
   - Beginner tooltip: "What you own right now and what you've already closed"
   - Advanced feature: Whale exit signals overlay, position heat map

3. **⚙️ Settings**
   - Wallet connection & balance display
   - Bot control: Start/Stop/Reset
   - Auto-stop conditions: Loss limit (e.g., −$50), Profit goal (e.g., +$500)
   - Preset selection: Steady, Max, Custom
   - Risk per trade, position sizing
   - Beginner tooltip: "Control when your bot trades and when it stops"
   - Advanced feature: Friction model customization, shadow trading parameters

4. **💹 P&L**
   - Session equity curve (real-time)
   - Win rate, best trade, worst trade, average win/loss
   - Drawdown visualization
   - Beginner tooltip: "Your trading performance over time"
   - Advanced feature: Monte Carlo analysis, correlation matrix

### Advanced Menu (Dropdown, Unlocks After First Close)

5. **📡 Signals** — Why coins passed/failed screening, decision audit trail
6. **🐳 Whales** — Smart money tracking, whale entries/exits
7. **🧮 Quant** — Shadow backtester, AI regime detection, strategy comparison
8. **📊 My Coins** — Portfolio history across sessions, coin performance
9. **📋 Paper** — Risk-free shadow trading simulator

---

## Part 3: Tooltip & Learn-More System

### Three Interaction Modes

**Mode 1: First Encounter (Automatic)**
- When user first hovers over or interacts with an element, tooltip auto-appears
- Shows: 1-2 sentence brief explanation
- Always includes: "Learn More →" button
- User can dismiss with "Got it" or by clicking elsewhere
- Marked internally; won't auto-trigger again for that specific element

**Mode 2: Subsequent Hovers (Optional)**
- Same element, user hovers again
- Tooltip appears only on hover (not auto)
- Still includes "Learn More" button
- User can dismiss or ignore

**Mode 3: Learn More Deep Dive**
- Clicking "Learn More" opens side panel or modal with:
  - Full explanation (3-5 sentences with context)
  - Visual example or diagram (if applicable)
  - Link to related advanced tab (e.g., "Want to dig deeper? Check Signals tab")
  - "Close" button

### Specific Tooltip Content Examples

| Element | First Encounter | Learn More Content |
|---------|-----------------|-------------------|
| Position P&L % | "Your profit/loss percentage" | Explains friction model, why % vs absolute, what's included in P&L calc |
| TP1 / TP2 | "Target prices. Bot sells here automatically." | Shadow trading strategy, why 2-tiered targets, how to adjust |
| Stop Loss | "Bot sells here to limit losses" | Risk management framework, position sizing, why this % |
| Session History | "Closed trades stay visible in this session" | How sessions work, resetting trades, exporting history |
| Whale Alert badge | "Smart money was spotted on this coin" | Whale detection logic, what smart money means, how signals are generated |
| Loss Limit setting | "Bot pauses if losses exceed this amount" | Risk control mechanics, session loss calculation, when it triggers |
| Profit Goal setting | "Bot pauses if profits reach this amount" | Profit-taking strategy, risk/reward, scaling out |

---

## Part 4: Blockchain Plugin Strategy

### Approach: Benchmark-Ready, Integrate If Fast

**Phase 1: Benchmarking (Prerequisite to Implementation)**

Before integrating blockchain plugin, benchmark three data categories:

| Data Type | Plugin Call | Benchmark Against | Pass Threshold | Decision |
|-----------|-------------|-------------------|-----------------|----------|
| **Token Risk** | Holder concentration, mint authority, deployer history | Current Helius API calls | <200ms slower | If pass → integrate into Scan tab |
| **Whale Tracking** | Smart money inflows, exit signals | Current whale detection module | <150ms slower | If pass → enhance Whales tab |
| **Historical Data** | Rug patterns, liquidity locks, deployment timeline | Current token_feature_snapshots query | <300ms slower | If pass → add to Signals tab |

**Phase 2: Conditional Integration**

**If blockchain plugin passes benchmarks:**
- **Scan tab:** Add "Blockchain Risk" badge showing deployer reputation, holder concentration, mint status
- **Signals tab:** Include blockchain validation data in "Why Bot Picked This" explanation
- **Whales tab:** Enhanced smart money tracking with on-chain verification
- Fallback: If any call times out, show cached data from existing RPC

**If blockchain plugin fails benchmarks:**
- Skip integration entirely
- Keep existing RPC/API stack (no regression)
- Document decision and revisit quarterly

**Critical Constraint:** Blockchain data is **additive only** — never blocking on user interaction.

---

## Part 5: Information Hierarchy by User Type

### For Absolute Beginners (First 5 Sessions)

**Visible by default:**
- Scan: Coin name, entry reason, current status
- Positions: Entry price, current price, P&L %
- Settings: Bot start/stop, loss limit, profit goal
- P&L: Win rate, best/worst trade

**Hidden but available:**
- Advanced Menu (after first close)
- Detailed regime detection
- Whale clustering analysis

**Tooltips:** All mandatory on first encounter

### For Experienced Traders / Advanced Users

**Visible by default:**
- All tabs including Advanced Menu
- Blockchain risk badges (if integrated)
- Historical performance data
- Backtest results

**Tooltips:** Optional, dismissible, never mandatory

---

## Part 6: Data Flow & State Management

**Session State:**
- Wallet connection persists
- Opened trades persist
- Closed trades persist until user clicks "Reset Session"
- Tooltip dismissals persist (user-specific, in local storage)
- Milestone progress persists (has user closed a trade? → unlock advanced menu)

**Bot State:**
- Active/paused
- Current positions (open)
- Session P&L
- Auto-stop conditions

**UI State:**
- Active tab
- Expanded/collapsed sections
- Tooltip display state (first-time vs. subsequent)

---

## Part 7: Success Criteria

**Beginner Accessibility:**
- New user can start bot and execute first trade in <3 minutes (post-wallet setup)
- All interactive elements have tooltips available on hover
- "Learn More" links provide 80%+ of advanced context

**No Information Overload:**
- Main dashboard shows ≤4 key metrics per tab
- Advanced features hidden in menu or Advanced tabs
- Blockchain data (if integrated) doesn't delay user interactions (all <200ms added latency)

**Advanced User Power:**
- All original tools remain accessible
- Blockchain plugin adds depth without blocking speed
- Can skip beginner tooltips after first session

---

## Part 8: Implementation Phases

**Phase 1: Onboarding & Milestone System**
- Implement wallet → bot start → first trade → close trade flow
- Add 3 guided moment modals (with zoomed views)
- Implement milestone tracking (has user closed a trade?)
- Unlock Advanced Menu on milestone

**Phase 2: Tab Reorganization & Dropdown Menu**
- Reorganize 9 tabs into Main 4 + Advanced 5
- Implement dropdown menu (hidden until unlock)
- Implement conditional visibility based on milestone

**Phase 3: Tooltip System**
- Add tooltip infrastructure (first encounter vs. hover)
- Implement "Learn More" side panel
- Add dismissal tracking (local storage)
- Add content for all ~40 interactive elements

**Phase 4: Blockchain Plugin Benchmarking & Integration**
- Benchmark blockchain calls vs. existing RPC (separate task)
- Integrate if benchmarks pass
- Add risk badges to Scan tab (conditional)
- Update Signals/Whales tabs with blockchain data (conditional)

---

## Assumptions & Constraints

- Blockchain plugin benchmarking happens in parallel; UI implementation doesn't wait for it
- Existing bot logic (screening, order placement, exit management) remains unchanged
- Session state is ephemeral (resets on logout); no cross-session persistence needed
- Mobile responsiveness: Main 4 tabs stack vertically; Advanced menu remains accessible
- Tooltips stored in local storage; no server-side persistence

---

## Notes for Implementation Team

- "Beginner" doesn't mean "limited" — it means "guided progression"
- Blockchain plugin is a nice-to-have, not a blocker
- Every element must have contextual help; no unexplained UI
- Closed trades in session history are read-only (user can view but not edit/replay)

---

## Rollback Strategy

**Goal:** Preserve ability to revert to original 9-tab UI if new design doesn't meet expectations.

**Implementation:**
- All changes are behind a **feature flag** in settings: `ENABLE_BEGINNER_UI` (default: true)
- If false, app renders original tab structure (all 9 tabs visible, no milestone unlocking, no tutorial modals)
- Tooltip system is separate and can be toggled independently: `ENABLE_TOOLTIPS` (default: true)
- Original HTML/CSS templates preserved in `/templates/original/` for quick revert

**Revert Process:**
1. Set feature flags to false in `.env` or settings panel
2. Clear user's milestone state (optional; can keep it)
3. UI reverts to original 9-tab layout immediately (no rebuild needed)
4. All new code remains in codebase; nothing deleted

**Testing Rollback:**
- Before production deploy, test both `ENABLE_BEGINNER_UI=true` and `false` to confirm both states work
- User can toggle the flag on/off to A/B test if needed

This ensures zero risk: if the new design doesn't resonate, original UI is one config change away.