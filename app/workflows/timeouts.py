"""Activity limits the workflows share."""
from datetime import timedelta

# One Aura turn. Her reply deadline is 10 minutes, kept open while one of her Claude turns works
# (app.coding.direct), so this bounds a turn that keeps Claude busy.
AURA_TURN = timedelta(hours=4)
