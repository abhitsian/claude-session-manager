#!/usr/bin/env python3
"""Daily cron job: archive Claude sessions before they expire.

Archives sessions older than 25 days (before Claude Code's ~30 day cleanup).
Runs independently of the dashboard. Safe to run multiple times.

Usage:
    python3 archive_cron.py          # archive expiring sessions
    python3 archive_cron.py --all    # archive everything (first run)
"""

import sys
import time
from pathlib import Path

# Add parent to path so imports work
sys.path.insert(0, str(Path(__file__).parent))

from claude_sessions.data.archive import SessionArchive
from claude_sessions.data.session_parser import SessionParser


def main():
    archive = SessionArchive()
    parser = SessionParser()

    archive_all = "--all" in sys.argv
    cutoff_days = 0 if archive_all else 25
    cutoff = time.time() - (cutoff_days * 86400)

    sessions = parser.get_all_sessions()
    archived = 0
    skipped = 0

    for session in sessions:
        if archive_all or session.start_time.timestamp() < cutoff:
            if archive.archive_session(session.session_id):
                archived += 1
                print(f"  Archived: {session.title or session.session_id[:8]}")
            else:
                skipped += 1

    # Mark gone sessions
    gone = archive._mark_gone_sessions()

    stats = archive.get_stats()
    print(f"\nDone. Archived: {archived}, Skipped (already saved): {skipped}, Gone: {gone}")
    print(f"Total in archive: {stats['total']} sessions, {stats['total_messages']} messages")


if __name__ == "__main__":
    main()
