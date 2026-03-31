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
