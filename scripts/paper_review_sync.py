"""Profile-scoped deterministic scheduler entry. No LLM or trading actions."""
from pathlib import Path
import sys
PROFILE=Path('/home/chihcheng/.hermes/profiles/perp-desk')
sys.path.insert(0,str(PROFILE/'repo_sync'))
from review_sync import main
import argparse


def run(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--visibility',choices=('private','public'),default='private')
    parser.add_argument('--authorize-public-repo')
    args=parser.parse_args(argv)
    owner='tony19930204UCL/perp-desk'
    if args.visibility=='public' and args.authorize_public_repo!=owner:
        parser.error('public sync requires exact repository authorization')
    if args.visibility=='private' and args.authorize_public_repo is not None:
        parser.error('public authorization requires explicit public visibility')
    command=['--profile',str(PROFILE),'--repository',str(PROFILE/'repo_sync/mirror'),
             '--remote','https://github.com/tony19930204UCL/perp-desk.git',
             '--owner-repo',owner,'--visibility',args.visibility]
    if args.visibility=='public':
        command += ['--authorize-public-repo',owner]
    return main(command)


if __name__=='__main__':
    raise SystemExit(run())
