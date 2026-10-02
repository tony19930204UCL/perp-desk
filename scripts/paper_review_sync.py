"""Profile-scoped deterministic scheduler entry. No LLM or trading actions."""
from pathlib import Path
import sys
PROFILE=Path('/home/chihcheng/.hermes/profiles/perp-desk')
sys.path.insert(0,str(PROFILE/'repo_sync'))
from review_sync import main
if __name__=='__main__':
    raise SystemExit(main(['--profile',str(PROFILE),'--repository',str(PROFILE/'repo_sync/mirror'),
                          '--remote','https://github.com/tony19930204UCL/perp-desk.git',
                          '--owner-repo','tony19930204UCL/perp-desk',
                          '--visibility','public','--authorize-public-repo','tony19930204UCL/perp-desk']))
