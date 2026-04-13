"""Process-level lock for serializing git fetch operations.

Worktrees share the same .git object store. Concurrent git fetch calls from
multiple sprint workers race to update refs/remotes/origin/<branch>, causing
"incorrect old value provided" errors on the losing threads. Importing
FETCH_LOCK from this module and wrapping fetch calls with it serializes
network fetches across all coordinator threads in the process.
"""

import threading

FETCH_LOCK = threading.Lock()
