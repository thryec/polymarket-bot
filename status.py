"""Live status dashboard — run `python status.py` to see current state."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "bot.db"


def main():
    if not DB_PATH.exists():
        print("No database found. Bot hasn't run yet.")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Latest snapshot
    snap = conn.execute("SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()

    print("=" * 70)
    print("  POLYMARKET AI TRADING BOT — STATUS")
    print("=" * 70)

    if snap:
        snap = dict(snap)
        print(f"  Last update:    {snap['ts']}")
        print(f"  Bankroll:       ${snap['bankroll']:>10.2f}")
        print(f"  Exposure:       ${snap['exposure']:>10.2f}")
        print(f"  Unrealized P&L: ${snap['unrealized_pnl']:>+10.2f}")
        print(f"  Realized P&L:   ${snap['realized_pnl']:>+10.2f}")
        print(f"  Total P&L:      ${snap['unrealized_pnl'] + snap['realized_pnl']:>+10.2f}")
    else:
        print("  No snapshots yet.")

    # Win/Loss record
    wins = conn.execute("SELECT COUNT(*) as n, COALESCE(SUM(pnl),0) as total FROM trades WHERE result = 'WIN'").fetchone()
    losses = conn.execute("SELECT COUNT(*) as n, COALESCE(SUM(pnl),0) as total FROM trades WHERE result = 'LOSS'").fetchone()
    pending = conn.execute("SELECT COUNT(*) as n, COALESCE(SUM(size_usdc),0) as total FROM trades WHERE result IS NULL").fetchone()

    total_resolved = wins['n'] + losses['n']
    total_pnl = wins['total'] + losses['total']

    print(f"\n{'─' * 70}")
    print(f"  TRADE RESULTS")
    print(f"{'─' * 70}")
    print(f"  Wins:           {wins['n']:>4}    (${wins['total']:>+.2f})")
    print(f"  Losses:         {losses['n']:>4}    (${losses['total']:>+.2f})")
    print(f"  Pending:        {pending['n']:>4}    (${pending['total']:>.2f} at risk)")
    if total_resolved > 0:
        win_rate = wins['n'] / total_resolved * 100
        print(f"  Win rate:       {win_rate:>5.1f}%   ({wins['n']}/{total_resolved})")
        print(f"  Net P&L:        ${total_pnl:>+.2f}")
        if wins['n'] > 0 and losses['n'] > 0:
            avg_win = wins['total'] / wins['n']
            avg_loss = losses['total'] / losses['n']
            print(f"  Avg win:        ${avg_win:>+.2f}")
            print(f"  Avg loss:       ${avg_loss:>+.2f}")
    else:
        print(f"  Win rate:        n/a (no resolved trades yet)")

    # Resolved trades detail
    resolved = conn.execute(
        "SELECT * FROM trades WHERE result IS NOT NULL ORDER BY ts DESC LIMIT 10"
    ).fetchall()
    if resolved:
        print(f"\n{'─' * 70}")
        print(f"  RESOLVED TRADES")
        print(f"{'─' * 70}")
        print(f"  {'Result':<6} {'P&L':>9} {'Side':>4} {'Paid':>8} {'Price':>6}  Question")
        print(f"  {'─'*6} {'─'*9} {'─'*4} {'─'*8} {'─'*6}  {'─'*25}")
        for t in resolved:
            t = dict(t)
            pnl_str = f"${t['pnl']:>+.2f}" if t['pnl'] is not None else "     n/a"
            question = (t['question'] or '')[:30]
            print(f"  {t['result']:<6} {pnl_str:>9} {t['side']:>4} ${t['size_usdc']:>7.2f} {t['price']:>6.3f}  {question}")

    # Open trades
    open_trades = conn.execute(
        "SELECT * FROM trades WHERE result IS NULL ORDER BY ts DESC LIMIT 15"
    ).fetchall()
    print(f"\n{'─' * 70}")
    print(f"  OPEN TRADES ({len(open_trades)})")
    print(f"{'─' * 70}")
    if open_trades:
        print(f"  {'Time':<20} {'Side':>4} {'Paid':>8} {'Price':>6} {'Edge':>6}  Question")
        print(f"  {'─'*19} {'─'*4} {'─'*8} {'─'*6} {'─'*6}  {'─'*25}")
        for t in open_trades:
            t = dict(t)
            edge_str = f"{t['edge']:.3f}" if t['edge'] else "  n/a"
            question = (t['question'] or '')[:30]
            print(f"  {t['ts']:<20} {t['side']:>4} ${t['size_usdc']:>7.2f} {t['price']:>6.3f} {edge_str:>6}  {question}")
    else:
        print("  No open trades.")

    # Analysis stats
    total = conn.execute("SELECT COUNT(*) as n FROM analyses").fetchone()['n']
    signals = conn.execute(
        "SELECT COUNT(*) as n FROM analyses WHERE recommendation != 'SKIP' AND edge >= 0.05 AND confidence >= 5"
    ).fetchone()['n']

    print(f"\n{'─' * 70}")
    print(f"  ANALYSIS STATS")
    print(f"{'─' * 70}")
    print(f"  Total analyzed:  {total}")
    print(f"  Signals found:   {signals}")
    if total > 0:
        print(f"  Signal rate:     {signals / total * 100:.1f}%")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    main()
