import pytest
from wallet_scanner import score_wallet, rank_wallets, WalletStats

def make_stats(wins, losses, avg_roi, roi_std, recency_weight):
    return WalletStats(
        address="ADDR1",
        wins=wins,
        losses=losses,
        avg_roi=avg_roi,
        roi_std=roi_std,
        recency_weight=recency_weight,
    )

def test_score_wallet_perfect():
    stats = make_stats(wins=90, losses=10, avg_roi=0.50, roi_std=0.05, recency_weight=1.0)
    score = score_wallet(stats)
    assert 0.0 <= score <= 1.0
    assert score > 0.7

def test_score_wallet_terrible():
    stats = make_stats(wins=10, losses=90, avg_roi=-0.20, roi_std=0.80, recency_weight=0.2)
    score = score_wallet(stats)
    assert score < 0.3

def test_score_wallet_zero_trades():
    stats = make_stats(wins=0, losses=0, avg_roi=0.0, roi_std=0.0, recency_weight=0.0)
    score = score_wallet(stats)
    assert score == 0.0

def test_rank_wallets_returns_top_pct():
    stats_list = [
        make_stats(90, 10, 0.5, 0.05, 1.0),
        make_stats(50, 50, 0.1, 0.3, 0.5),
        make_stats(10, 90, -0.2, 0.8, 0.2),
        make_stats(80, 20, 0.4, 0.1, 0.9),
        make_stats(20, 80, -0.1, 0.6, 0.3),
    ]
    top = rank_wallets(stats_list, top_pct=0.4)
    assert len(top) == 2
    assert top[0].score >= top[1].score
