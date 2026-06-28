paused

# Worker control flag. Curator-owned.
#
# First line is the state: `active` or `paused`.
# - active : the worker runs its loop and picks work from the board.
# - paused : the worker quiesces at its next checkpoint (no new work started).
#
# Seeded as `paused` so the worker stays quiet until a board is set up and the
# user/curator deliberately switches it to `active`.
