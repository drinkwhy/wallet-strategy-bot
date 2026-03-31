// static/js/tooltip_system.js
// Intelligent tooltip system for beginner guidance

class TooltipSystem {
  constructor() {
    this.storageKey = 'soltrader_tooltips';
    this.tooltips = this.load();
    this.activeTooltip = null;
    this.dismissedTooltips = {};
  }

  load() {
    const stored = localStorage.getItem(this.storageKey);
    return stored ? JSON.parse(stored) : {};
  }

  save() {
    localStorage.setItem(this.storageKey, JSON.stringify(this.tooltips));
  }

  /**
   * Register a tooltip for an element
   * @param {string} elementSelector - CSS selector for the target element
   * @param {object} config - Tooltip configuration
   *   - text: tooltip message
   *   - position: 'top', 'bottom', 'left', 'right' (default: 'bottom')
   *   - dismissible: whether user can close tooltip (default: true)
   *   - showDelay: ms before showing (default: 500)
   *   - persist: whether to show until dismissed (default: false)
   *   - milestone: milestone name to check before showing
   *   - icon: emoji/icon to show (default: '💡')
   */
  register(elementSelector, config) {
    const element = document.querySelector(elementSelector);
    if (!element) {
      console.warn(`[Tooltip] Element not found: ${elementSelector}`);
      return;
    }

    const tooltipId = `tooltip-${Math.random().toString(36).substr(2, 9)}`;
    const isDismissed = this.isDismissed(tooltipId);

    if (isDismissed && !config.persist) {
      return;
    }

    const {
      text = '',
      position = 'bottom',
      dismissible = true,
      showDelay = 500,
      persist = false,
      milestone = null,
      icon = '💡'
    } = config;

    // Check milestone gate
    if (milestone && window.milestoneTracker) {
      if (!window.milestoneTracker.milestones[milestone]) {
        return;
      }
    }

    // Setup hover listeners
    let timeoutId = null;

    element.addEventListener('mouseenter', () => {
      if (isDismissed && !persist) return;
      timeoutId = setTimeout(() => {
        this.show(element, tooltipId, {
          text, position, dismissible, icon
        });
      }, showDelay);
    });

    element.addEventListener('mouseleave', () => {
      clearTimeout(timeoutId);
      if (!persist) {
        this.hide(tooltipId);
      }
    });

    // Store for reference
    if (!this.tooltips[tooltipId]) {
      this.tooltips[tooltipId] = {
        selector: elementSelector,
        config
      };
      this.save();
    }
  }

  /**
   * Show a tooltip
   * @param {Element} element - Target element
   * @param {string} tooltipId - Unique tooltip ID
   * @param {object} options - Display options
   */
  show(element, tooltipId, options) {
    this.hide(tooltipId); // Hide any existing

    const {
      text = '',
      position = 'bottom',
      dismissible = true,
      icon = '💡'
    } = options;

    const tooltip = document.createElement('div');
    tooltip.id = tooltipId;
    tooltip.className = `tooltip tooltip-${position}`;
    tooltip.innerHTML = `
      <div class="tooltip-inner">
        <span class="tooltip-icon">${icon}</span>
        <span class="tooltip-text">${text}</span>
        ${dismissible ? `<button class="tooltip-close" onclick="tooltipSystem.dismiss('${tooltipId}')">×</button>` : ''}
      </div>
      <div class="tooltip-arrow"></div>
    `;

    document.body.appendChild(tooltip);

    // Position tooltip relative to element
    this.positionTooltip(tooltip, element, position);

    this.activeTooltip = tooltipId;
    console.log(`[Tooltip] Showing: ${tooltipId}`);
  }

  /**
   * Hide a tooltip
   * @param {string} tooltipId - Tooltip ID to hide
   */
  hide(tooltipId) {
    const tooltip = document.getElementById(tooltipId);
    if (tooltip) {
      tooltip.remove();
      if (this.activeTooltip === tooltipId) {
        this.activeTooltip = null;
      }
    }
  }

  /**
   * Dismiss a tooltip permanently
   * @param {string} tooltipId - Tooltip ID to dismiss
   */
  dismiss(tooltipId) {
    this.dismissedTooltips[tooltipId] = true;
    this.hide(tooltipId);
    console.log(`[Tooltip] Dismissed: ${tooltipId}`);
  }

  /**
   * Check if tooltip is dismissed
   * @param {string} tooltipId - Tooltip ID
   * @returns {boolean}
   */
  isDismissed(tooltipId) {
    if (window.milestoneTracker) {
      return window.milestoneTracker.isTooltipDismissed(tooltipId);
    }
    return this.dismissedTooltips[tooltipId] || false;
  }

  /**
   * Position tooltip relative to element
   * @private
   */
  positionTooltip(tooltip, element, position) {
    const rect = element.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const gap = 12;
    let left, top;

    switch (position) {
      case 'top':
        left = rect.left + rect.width / 2 - tooltipRect.width / 2;
        top = rect.top - tooltipRect.height - gap;
        break;
      case 'bottom':
        left = rect.left + rect.width / 2 - tooltipRect.width / 2;
        top = rect.bottom + gap;
        break;
      case 'left':
        left = rect.left - tooltipRect.width - gap;
        top = rect.top + rect.height / 2 - tooltipRect.height / 2;
        break;
      case 'right':
        left = rect.right + gap;
        top = rect.top + rect.height / 2 - tooltipRect.height / 2;
        break;
    }

    // Keep tooltip in viewport
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const padding = 12;

    if (left < padding) left = padding;
    if (left + tooltipRect.width > viewportWidth - padding) {
      left = viewportWidth - tooltipRect.width - padding;
    }
    if (top < padding) top = padding;
    if (top + tooltipRect.height > viewportHeight - padding) {
      top = viewportHeight - tooltipRect.height - padding;
    }

    tooltip.style.left = left + 'px';
    tooltip.style.top = top + 'px';
  }

  /**
   * Reset all dismissed tooltips
   */
  resetDismissed() {
    this.dismissedTooltips = {};
    console.log('[Tooltip] All dismissals reset');
  }

  /**
   * Clear all tooltips
   */
  clearAll() {
    document.querySelectorAll('[id^="tooltip-"]').forEach(t => t.remove());
    this.activeTooltip = null;
  }
}

// Create global instance
const tooltipSystem = new TooltipSystem();
