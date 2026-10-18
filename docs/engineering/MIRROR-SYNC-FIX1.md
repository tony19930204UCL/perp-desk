# MIRROR-SYNC-FIX1

Manual-only operation:

`python3 automation/review_sync.py --profile PROFILE --repository MIRROR --remote REMOTE --owner-repo OWNER/REPO --tighten-private-git-permissions`

The command exits without publication or scanning. It preflights the configured mirror's `.git` tree, rejects symlinks, non-regular entries, hardlinked files and foreign ownership, then removes group/other permissions (never adds permissions). Changed entries are printed using relative paths and octal modes. Normal scheduled/publish checks never invoke this operation. Stop concurrent Git activity before repair: this is not a transactional filesystem operation, and races can block after earlier changes. Directory link counts are not file hardlinks.
