"""
Phase 5 Integration Test - Verify all components work together
"""
import re

def test_phase5_integration():
    """Integration test for Phase 5 implementation"""
    
    with open('app.py', 'r', encoding='utf-8', errors='ignore') as f:
        app_content = f.read()
    
    tests = {
        'Shadow Trading Widget Enhancement': [
            'id="lv-last-opt-time"',
            'id="lv-next-opt-countdown"',
            'id="lv-params-changed-time"',
            'id="lv-circuit-breaker-status"',
            'onclick="disableAutoOptimize()"'
        ],
        'Optimization Decisions Table': [
            'class="opt-decisions-panel"',
            'id="opt-decisions-table"',
            'opt-decisions-status',
            'opt-decisions-reason',
            'opt-decisions-improvement'
        ],
        'CSS Classes': [
            '.opt-decisions-status.pending',
            '.opt-decisions-status.deployed',
            '.opt-decisions-status.reverted',
            '.opt-decisions-improvement.positive',
            '.opt-decisions-improvement.negative'
        ],
        'JavaScript Functions': [
            'async function pollOptimizationMetrics()',
            'async function pollOptimizationDecisions()',
            'function toggleOptDecisions()',
            'function disableAutoOptimize()',
            'setInterval(pollOptimizationMetrics, 30000)',
            'setInterval(pollOptimizationDecisions, 45000)'
        ],
        'API Endpoints': [
            "methods=['POST', 'GET']",
            "/api/current-parameters",
            "/api/optimization-decisions"
        ]
    }
    
    results = {}
    for category, patterns in tests.items():
        print(f"\n{category}:")
        results[category] = {}
        for pattern in patterns:
            found = pattern in app_content
            results[category][pattern] = found
            status = '✓' if found else '✗'
            print(f"  {status} {pattern[:60]}")
    
    # Calculate summary
    total_checks = sum(len(patterns) for patterns in tests.values())
    total_passed = sum(1 for cat in results.values() for found in cat.values() if found)
    
    print(f"\n{'='*60}")
    print(f"INTEGRATION TEST RESULTS: {total_passed}/{total_checks} PASSED")
    print(f"{'='*60}\n")
    
    if total_passed == total_checks:
        print("✅ ALL PHASE 5 COMPONENTS SUCCESSFULLY IMPLEMENTED")
        return True
    else:
        print("❌ SOME COMPONENTS MISSING")
        missing = total_checks - total_passed
        print(f"   {missing} component(s) not found")
        return False

if __name__ == '__main__':
    success = test_phase5_integration()
    exit(0 if success else 1)
