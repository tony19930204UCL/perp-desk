"""Script-only public scheduler entry, authorized for one literal repository."""
from paper_review_sync import run


if __name__=='__main__':
    raise SystemExit(run(['--visibility','public','--authorize-public-repo',
                          'tony19930204UCL/perp-desk']))
