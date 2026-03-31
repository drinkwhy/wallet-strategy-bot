# SolTrader Beginner UI - Rollback Guide

## Overview

The Beginner UI is a streamlined dashboard experience designed for new users of SolTrader. It can be enabled or disabled using the `ENABLE_BEGINNER_UI` environment variable flag.

## Quick Toggle

### Enable Beginner UI (Default)
```bash
export ENABLE_BEGINNER_UI=true
```

### Disable Beginner UI (Rollback to Original 9-Tab UI)
```bash
export ENABLE_BEGINNER_UI=false
```

Then restart the application.

## What Changes Between Modes

### Beginner UI Enabled (`ENABLE_BEGINNER_UI=true`)
- **Dashboard**: 4 main tabs always visible: Scan, Positions, Settings, P&L
- **Advanced Menu**: Hidden by default, unlocks after first position closes
- **Advanced Tabs**: Signals, Whales, Quant, My Coins, Paper (in dropdown menu)
- **Onboarding**: 3 guided modals shown at key milestones
- **Tooltips**: Contextual help system for beginners
- **Milestones**: Tracks user progress (bot started, first trade, first close)

### Original UI (`ENABLE_BEGINNER_UI=false`)
- **Dashboard**: All 9 tabs visible at once (original layout)
- **Tabs**: portfolio, scanner, settings, signals, whales, positions, pnl, quant, paper
- **No Modals**: Onboarding guides disabled
- **No Tooltips**: Contextual help disabled
- **No Milestones**: Progress tracking disabled
- **Advanced Menu**: Not rendered

## Files Affected by Beginner UI Flag

### New Files Added (only loaded when flag is true)
- `templates/components/advanced_menu.html` - Advanced menu component
- `templates/components/onboarding_modals.html` - Onboarding modals
- `static/js/milestone_tracker.js` - Milestone tracking system
- `static/js/tooltip_system.js` - Tooltip system
- `static/css/tooltips.css` - Tooltip styles
- `tests/test_milestone_system.py` - Test suite

### Modified Files
- `app.py` - Dashboard tab bar restructured, milestone endpoints added

## Testing the Flag

### Test Beginner UI Enabled
1. Set `ENABLE_BEGINNER_UI=true` (or leave default)
2. Start the application
3. Verify in browser:
   - Dashboard shows only 4 main tabs
   - Advanced menu is NOT visible yet
   - Onboarding modal appears when bot starts
   - Advanced menu becomes visible after first position closes

### Test Rollback to Original UI
1. Set `ENABLE_BEGINNER_UI=false`
2. Restart the application
3. Verify in browser:
   - All 9 tabs appear in the tab bar
   - Advanced menu is gone
   - Onboarding modals do not appear
   - No tooltips visible
   - Dashboard looks like the original implementation

## Verification Checklist

### For Beginner UI Enabled
- [ ] 4 main tabs visible: Scan, Positions, Settings, P&L
- [ ] Advanced menu toggle hidden initially
- [ ] Advanced menu appears after first position closes
- [ ] All 5 advanced tabs accessible via dropdown: Signals, Whales, Quant, My Coins, Paper
- [ ] Bot started modal appears when bot starts
- [ ] First trade modal appears on first buy execution
- [ ] First close modal appears when first position closes
- [ ] Tooltips appear on hover (if enabled for elements)
- [ ] Tab styling matches design (rounded buttons, icons, meta text)

### For Original UI Enabled (ENABLE_BEGINNER_UI=false)
- [ ] All 9 tabs visible: My Coins, Scan, Settings, Signals, Whales, Positions, P&L, Quant, Paper
- [ ] No advanced menu visible
- [ ] No onboarding modals appear
- [ ] No tooltips visible
- [ ] Original tab bar layout matches v1
- [ ] Tab styling unchanged from original

## Performance Considerations

### With Beginner UI Enabled
- Additional JavaScript loaded: `milestone_tracker.js`, `tooltip_system.js`
- Additional CSS loaded: `tooltips.css`
- LocalStorage used for milestone tracking (~200 bytes per user)
- Modal rendering overhead: minimal (only shown at milestones)
- No additional API calls beyond existing functionality

### With Beginner UI Disabled
- No additional files loaded
- No localStorage overhead
- No modal rendering overhead
- Identical performance to original implementation

## Troubleshooting

### Advanced Menu Not Appearing
1. Verify `ENABLE_BEGINNER_UI=true`
2. Check browser console for JavaScript errors
3. Verify milestone_tracker.js loaded successfully
4. Check if `milestoneTracker.isUnlockAdvancedMenu()` returns true

### Modals Not Showing
1. Verify `ENABLE_BEGINNER_UI=true`
2. Ensure `onboarding_modals.html` is loaded
3. Check if modal DOM elements exist: `#onboarding-bot-started-modal`, etc.
4. Check console for dismissal state in localStorage

### Tooltips Not Appearing
1. Verify `tooltips.css` is loaded (check Network tab)
2. Ensure `tooltip_system.js` is loaded
3. Check if `tooltipSystem` global object exists
4. Verify elements have tooltip registrations

### Rollback Not Working
1. Confirm environment variable is set: `echo $ENABLE_BEGINNER_UI`
2. Restart application (not just refresh)
3. Clear browser cache
4. Check server logs for configuration load message
5. Verify old code paths are executing

## Configuration in Docker

If using Docker, set the environment variable in your docker-compose.yml or docker run:

```yaml
environment:
  ENABLE_BEGINNER_UI: "false"  # or "true"
```

Or via command line:
```bash
docker run -e ENABLE_BEGINNER_UI=false soltrader:latest
```

## Git Workflow

To rollback to the original code (before Beginner UI):
```bash
git diff main..HEAD  # See all changes
git checkout main -- app.py  # Revert app.py to main
rm templates/components/advanced_menu.html
rm templates/components/onboarding_modals.html
rm static/js/tooltip_system.js
rm static/css/tooltips.css
rm tests/test_milestone_system.py
```

Or to merge selectively:
```bash
git cherry-pick -n <commit-hash>  # Stage without committing
git reset -- <files-to-remove>     # Unstage unwanted files
git checkout -- <files-to-remove>  # Discard unwanted files
git commit -m "Selective merge without Beginner UI"
```

## Long-Term Maintenance

- Beginner UI is backward compatible with original implementation
- Setting `ENABLE_BEGINNER_UI=false` fully disables new functionality
- No database schema changes required
- No migrations needed
- Safe to toggle at runtime (application restart required)

## Support

For issues or questions:
1. Check browser console for JavaScript errors
2. Review server logs for Python exceptions
3. Verify all new files are present and properly linked
4. Test both enabled and disabled states
5. Review test suite: `python tests/test_milestone_system.py`
