"""Deterministic one-way review export. Never deploys, trades, or reads credentials."""
from pathlib import Path
import re
import json
import hashlib
import os
import tempfile
import subprocess
import fcntl
import stat
import secrets
from contextlib import contextmanager


@contextmanager
def directory(path,create=False):
    """Pin each directory inode; never follow a pathname symlink."""
    path=Path(os.path.abspath(path))
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:os.mkdir(part,mode=0o700,dir_fd=fd)
                except FileExistsError:pass
            try:child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            except FileNotFoundError:raise
            except OSError as error:raise Blocked('unsafe directory') from error
            os.close(fd);fd=child
        yield fd
    finally:os.close(fd)


def read_bytes(path,missing=False):
    path=Path(path)
    try:
        with directory(path.parent) as parent:
            try:fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            except FileNotFoundError:
                if missing:return None
                raise
            except OSError as error:raise Blocked('unsafe file: '+path.name) from error
            with os.fdopen(fd,'rb') as stream:
                before=os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:raise Blocked('non-regular or hardlinked file: '+path.name)
                if before.st_size>2*1024*1024:raise Blocked('oversize export: '+path.name)
                raw=stream.read(2*1024*1024+1);after=os.fstat(stream.fileno())
                if len(raw)>2*1024*1024 or (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    raise Blocked('file changed during read')
                return raw
    except FileNotFoundError:
        if missing:return None
        raise


def safe_unlink(path):
    path=Path(path)
    with directory(path.parent) as parent:
        try:
            info=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):raise Blocked('unsafe deletion')
            os.unlink(path.name,dir_fd=parent);os.fsync(parent)
        except FileNotFoundError:pass


def tick(state_path,collector,publisher,now,force=False):
    state_path=Path(state_path)
    with directory(state_path.parent,create=True) as parent:
        try:fd=os.open(state_path.name+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=parent)
        except OSError as error:raise Blocked('unsafe status lock') from error
        with os.fdopen(fd,'r+') as lock:
            info=os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise Blocked('non-regular or hardlinked status lock')
            fcntl.flock(lock,fcntl.LOCK_EX)
            prior=json.loads(read_bytes(state_path,missing=True) or b'{}')
            result=dict(prior,checked_at=now)
            try:
                files=collector();digest=fingerprint(files)
                if digest!=prior.get('pending_fingerprint'):
                    result.update(pending_fingerprint=digest,pending_since=now)
                if not force and now-result['pending_since']<15:
                    result.update(state='settling')
                else:
                    result.update(publisher(files),last_success_at=now)
                result.pop('error',None)
            except Exception as error:
                result.update(state='blocked',error=str(error) if isinstance(error,Blocked) else type(error).__name__)
            atomic_write(state_path,(json.dumps(result,indent=2)+'\n').encode())
            return result


def controlled_env():
    # No inherited Git discovery/config/transport overrides, loader or Python injection.
    env={k:v for k,v in os.environ.items() if not k.startswith(('GIT_','GITLEAKS','LD_','DYLD_','PYTHON'))}
    env.update(GIT_TERMINAL_PROMPT='0',GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_SYSTEM='/dev/null',GIT_CONFIG_GLOBAL='/dev/null',
               GIT_ATTR_NOSYSTEM='1',GIT_NO_REPLACE_OBJECTS='1')
    return env


def run(args,cwd=None,check=True):
    def execute(directory_fd=None):
        return subprocess.run(args,cwd=('/proc/self/fd/'+str(directory_fd)) if directory_fd is not None else None,
                              pass_fds=(directory_fd,) if directory_fd is not None else (),
                              capture_output=True,text=True,timeout=90,env=controlled_env(),umask=0o077)
    if cwd is None:result=execute()
    else:
        with directory(cwd) as fd:result=execute(fd)
    if check and result.returncode:
        raise Blocked('command failed: '+Path(args[0]).name+' '+args[1]+' (exit '+str(result.returncode)+')')
    return result


class _TransientGitTreeChange(RuntimeError):
    """A Git-generated path disappeared while the private tree was being inspected."""


def _check_private_tree_once(path):
    """Require one complete permission/type pass over a stable directory snapshot."""
    with directory(path) as fd:
        before=os.fstat(fd)
        if before.st_uid!=os.getuid() or before.st_mode&0o022:
            raise Blocked('unsafe Git directory ownership/permissions')
        names=os.listdir(fd)
        for name in names:
            try:
                entry=os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError as error:
                raise _TransientGitTreeChange(name) from error
            if entry.st_uid!=os.getuid() or entry.st_mode&0o022:
                raise Blocked('unsafe Git entry permissions')
            if stat.S_ISDIR(entry.st_mode):
                try:_check_private_tree_once(Path(path)/name)
                except FileNotFoundError as error:raise _TransientGitTreeChange(name) from error
            elif not stat.S_ISREG(entry.st_mode) or entry.st_nlink!=1:
                raise Blocked('unsafe Git entry')
        after=os.fstat(fd)
        if (before.st_ino,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_mtime_ns,after.st_ctime_ns):
            raise _TransientGitTreeChange(Path(path).name)
        if read_bytes(Path(path)/'objects/info/alternates',missing=True):
            raise Blocked('Git alternate object directories rejected')


def check_private_tree(path):
    """Fail closed on unsafe entries; tolerate only a bounded transient Git-tree race."""
    # Git may create and remove maintenance.lock or other internal lock files between
    # listdir() and stat(). Never ignore the missing entry: restart the entire
    # inspection and require a stable pass. Persistent churn is an explicit block.
    for attempt in range(3):
        try:
            _check_private_tree_once(path)
            return
        except _TransientGitTreeChange:
            if attempt==2:
                raise Blocked('Git directory changed during inspection') from None


def git(repository,*args,check=True):
    repository=Path(repository)
    with directory(repository):pass
    if (repository/'.git').exists() or (repository/'.git').is_symlink():
        check_private_tree(repository/'.git')
        validate_git_config(repository)
    return run(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
                '-c','tag.gpgsign=false','-c','core.fsmonitor=false',
                '-c','core.attributesFile=/dev/null','-c','credential.helper=',
                '-c','credential.https://github.com.helper=!'+str(Path.home()/'.local/bin/gh')+' auth git-credential',
                '-c','init.templateDir=','-c','core.autocrlf=false',
                '-c','core.safecrlf=false','-c','core.filemode=false',*args],repository,check)


def validate_git_config(repository):
    config=Path(repository)/'.git/config'
    raw=read_bytes(config)
    with tempfile.TemporaryDirectory(prefix='review-config-') as td:
        pinned=Path(td)/'config';atomic_write(pinned,raw)
        result=run(['git','config','--file',str(pinned),'--no-includes','--null','--list'],cwd=td)
    allowed={'core.repositoryformatversion':{'0'},'core.filemode':{'true','false'},
             'core.bare':{'false'},'core.logallrefupdates':{'true'},
             'core.hookspath':{'/dev/null'},'commit.gpgsign':{'false'},
             'remote.origin.fetch':{'+refs/heads/*:refs/remotes/origin/*'},
             'branch.main.remote':{'origin'},'branch.main.merge':{'refs/heads/main'}}
    for entry in result.stdout.split('\0'):
        if not entry:continue
        key,_,value=entry.partition('\n')
        if key in ('user.name','user.email','remote.origin.url','remote.origin.pushurl'):continue
        if key not in allowed or value not in allowed[key]:
            raise Blocked('unsupported local Git configuration: '+key)


def publish(files,repository,remote_url,owner_repo,lookup,scan,*,visibility='private',public_authorization=None,_test_remote=None):
    # Public publication is an explicit authorization for this exact identity only.
    if visibility not in ('private','public'):
        raise Blocked('invalid repository visibility mode')
    if visibility=='public' and public_authorization!=owner_repo:
        raise Blocked('public export requires exact owner/repository authorization')
    # Deliberately not exposed by the CLI: tests may use one explicit local bare repo.
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+',owner_repo):
        raise Blocked('invalid authorized owner/repository')
    expected='https://github.com/'+owner_repo+'.git'
    if _test_remote is not None:
        test_path=Path(_test_remote)
        if not test_path.is_absolute() or test_path.is_symlink() or not (test_path/'HEAD').is_file() or not (test_path/'objects').is_dir():
            raise Blocked('test seam requires an explicit local bare repository')
        expected=str(test_path)
    if remote_url!=expected:
        raise Blocked('remote URL is not bound to the authorized repository')
    metadata=lookup(owner_repo)
    if not isinstance(metadata,dict) or metadata.get('private') is not (visibility=='private') or metadata.get('full_name')!=owner_repo:
        raise Blocked('remote must be the exact authorized '+visibility+' repository')
    repository=Path(os.path.abspath(repository))
    with directory(repository,create=True):pass
    state_dir=repository.parent/('.'+repository.name+'.review-sync')
    with directory(state_dir,create=True) as parent:
        fd=os.open('lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=parent)
        with os.fdopen(fd,'r+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return publish_locked(files,repository,remote_url,scan,state_dir/'state.json')


def manifest_bytes(files):
    manifest=dict(schema_version=1,purpose='read-only review snapshot; not a deployment or test acceptance',
                  files={p:hashlib.sha256(b).hexdigest() for p,b in sorted(files.items())},fingerprint=fingerprint(files))
    return (json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()


def verify_tree(repository,tree,files):
    expected=dict(files)
    expected['source_manifest.json']=manifest_bytes(files)
    blobs={name:'100644 blob '+hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
           for name,raw in expected.items()}
    observed={}
    for entry in git(repository,'ls-tree','-rz','--full-tree',tree).stdout.split('\0'):
        if entry:
            metadata,name=entry.split('\t',1);observed[name]=metadata
    if observed!=blobs:raise Blocked('entire outgoing tree differs from owned snapshot')


def publish_locked(files,repository,remote_url,scan,state_path):
    ledger=json.loads(read_bytes(state_path,missing=True) or b'{}')
    if ledger and ledger.get('remote')!=remote_url:raise Blocked('ownership state remote mismatch')
    if not (repository/'.git').exists():
        repository.mkdir(parents=True,exist_ok=True)
        git(repository,'init','--initial-branch=main')
        git(repository,'config','user.name','Perp Desk Auto Sync')
        git(repository,'config','user.email','perp-desk-sync@users.noreply.github.com')
        git(repository,'config','commit.gpgsign','false')
        git(repository,'config','core.hooksPath','/dev/null')
        git(repository,'remote','add','origin',remote_url)
    check_private_tree(repository/'.git')
    validate_git_config(repository)
    for args in [('remote','get-url','--all','origin'),('remote','get-url','--push','--all','origin')]:
        if git(repository,*args).stdout.splitlines()!=[remote_url]:raise Blocked('unexpected fetch/push remote URL')
    if git(repository,'branch','--show-current').stdout.strip()!='main':raise Blocked('unexpected mirror branch')
    staged=git(repository,'diff','--cached','--name-only','-z').stdout
    if staged and (not ledger.get('pending_tree') or git(repository,'write-tree').stdout.strip()!=ledger['pending_tree']):
        raise Blocked('pre-existing staged changes require manual review')
    head_result=git(repository,'rev-parse','--verify','HEAD',check=False)
    head=head_result.stdout.strip() if head_result.returncode==0 else None
    if head!=ledger.get('owned_head'):
        parents=git(repository,'rev-list','--parents','-n','1',head).stdout.split() if head else []
        expected_parents=[head]+([ledger['owned_head']] if ledger.get('owned_head') else [])
        if not ledger.get('pending_tree') or parents!=expected_parents or git(repository,'rev-parse',head+'^{tree}').stdout.strip()!=ledger['pending_tree'] or git(repository,'log','-1','--format=%B',head).stdout.strip()!=ledger.get('pending_message'):
            raise Blocked('unowned local history; manual review required')
        ledger.update(owned_head=head,owned_paths=ledger['pending_paths'])
        atomic_write(state_path,(json.dumps(ledger,sort_keys=True)+'\n').encode())
    ref=git(repository,'ls-remote','--heads','origin','refs/heads/main').stdout.split()
    remote_head=ref[0] if ref else None
    # Fetch every advertised ref into a scanner-only namespace, never local main.
    # Non-main branches/tags must not escape the mandatory history scan.
    git(repository,'fetch','origin','+refs/*:refs/review-sync-remote/*')
    if remote_head:
        if not head or git(repository,'merge-base','--is-ancestor',remote_head,head,check=False).returncode:
            raise Blocked('remote advanced/diverged; no pull, overwrite, reset or force-push')
    prior_paths=set(ledger.get('pending_owned',ledger.get('owned_paths',[])))
    ledger.update(remote=remote_url,pending_owned=sorted(prior_paths|set(files)))
    atomic_write(state_path,(json.dumps(ledger,sort_keys=True)+'\n').encode())
    owned=mirror(files,repository,_prior=ledger['pending_owned'])
    scan(repository)  # Both working-tree and history secret scans are mandatory in production.
    tracked=set(git(repository,'ls-files','-z').stdout.split('\0'))
    # A failed pre-stage scan may own paths never added to the index.
    # Keep the durable ledger, but only stage extant files or tracked deletions.
    stage={name for name in owned if name in tracked or read_bytes(repository/relative_path(name),missing=True) is not None}
    git(repository,'--literal-pathspecs','add','--all','--',*sorted(stage))
    tree=git(repository,'write-tree').stdout.strip()
    verify_tree(repository,tree,files)
    ledger.update(pending_tree=tree,pending_paths=sorted(files),
                  pending_message='chore(snapshot): '+fingerprint(files)[:12]+' transaction '+secrets.token_hex(16))
    atomic_write(state_path,(json.dumps(ledger,sort_keys=True)+'\n').encode())
    if git(repository,'diff','--cached','--quiet',check=False).returncode:
        git(repository,'commit','-m',ledger['pending_message'])
    sha=git(repository,'rev-parse','HEAD').stdout.strip()
    atomic_write(state_path,(json.dumps(dict(remote=remote_url,owned_head=sha,owned_paths=sorted(files)),sort_keys=True)+'\n').encode())
    scan(repository)
    verify_tree(repository,sha,files)
    if git(repository,'rev-parse','HEAD').stdout.strip()!=sha:raise Blocked('HEAD changed during scan')
    if sha!=remote_head:
        git(repository,'push','origin','HEAD:refs/heads/main')
        observed=git(repository,'ls-remote','--heads','origin','refs/heads/main').stdout.split()
        if not observed or observed[0]!=sha:raise Blocked('remote SHA readback did not match')
        state='pushed'
    else:state='unchanged'
    return dict(state=state,sha=sha,files=len(files),fingerprint=fingerprint(files))


def fingerprint(files):
    return hashlib.sha256(json.dumps({p:hashlib.sha256(b).hexdigest() for p,b in sorted(files.items())},sort_keys=True).encode()).hexdigest()


def relative_path(name):
    p=Path(name)
    if p.is_absolute() or '..' in p.parts or not p.parts or '.git' in p.parts:
        raise Blocked('unsafe repository path')
    return p


def atomic_write(path,raw):
    path=Path(path)
    with directory(path.parent,create=True) as parent:
        try:
            info=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise Blocked('unsafe write target')
        except FileNotFoundError:pass
        tmp='.'+path.name+'.'+secrets.token_hex(12)
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(tmp,path.name,src_dir_fd=parent,dst_dir_fd=parent);os.fsync(parent)
        finally:
            try:os.unlink(tmp,dir_fd=parent)
            except FileNotFoundError:pass


def mirror(files,repository,*,_prior=None):
    repository=Path(repository)
    with directory(repository,create=True):pass
    paths={name:relative_path(name) for name in files}
    manifest_path=repository/'source_manifest.json'
    previous=json.loads(read_bytes(manifest_path,missing=True) or b'{"files":{}}')
    prior={name:relative_path(name) for name in (previous['files'] if _prior is None else _prior)}
    for rel in [*paths.values(),*prior.values(),Path('source_manifest.json')]:
        target=repository/rel
        if target.is_symlink() or any(p.is_symlink() for p in target.parents if p!=repository and repository in p.parents):raise Blocked('repository symlink rejected')
    for name,raw in files.items():
        p=repository/paths[name]
        if read_bytes(p,missing=True)!=raw:atomic_write(p,raw)
    for name in prior.keys()-files.keys():
        safe_unlink(repository/prior[name])
    atomic_write(manifest_path,manifest_bytes(files))
    return set(prior)|set(files)|{'source_manifest.json'}

class Blocked(RuntimeError):
    """Unsafe or inconsistent publication. No best-effort bypass."""


def safe_read(path, profile):
    path=Path(os.path.abspath(path));profile=Path(os.path.abspath(profile))
    with directory(profile):pass
    if not path.is_relative_to(profile):raise Blocked('outside profile')
    raw=read_bytes(path)
    if b'\0' in raw:raise Blocked('binary export rejected: '+path.name)
    try:text=raw.decode('utf-8')
    except UnicodeError:raise Blocked('non-UTF8 export rejected: '+path.name)
    if re.search(r'gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY',text):
        raise Blocked('credential pattern detected; value not logged: '+path.name)
    return raw

DOC_FILES={'README.md','AGENTS.md','.gitignore','.github/workflows/ci.yml',
           'docs/DEPLOYMENT_AND_GAPS.md','docs/AUTO_SYNC.md','docs/ARCHITECTURE.md',
           'docs/AI_REVIEW_GUIDE.md','docs/VERIFICATION.md','docs/HEALTH_V3_OPERATOR.md',
           'docs/STORAGE_PROTECTION_V3.md','docs/STARTUP_RECOVERY_V3.md',
           'docs/SOURCE_TIMING_EVIDENCE.md','docs/MULTI_LEDGER_DASHBOARD.md',
           'docs/OPERATOR_EVENT_GATE.md','docs/OWNER_RESUMPTION_HANDOFF.md','docs/ETH_DISCOVERY_LAB.md','docs/OBSERVER_CONSOLE.md'}

SKIP_DIRS={'.git','__pycache__','data','shared','evidence','probe-data','probe-shared','cache','logs','sessions','node_modules'}


def collect(profile,*,visibility='private'):
    if visibility not in ('private','public'):raise Blocked('invalid collection visibility mode')
    profile=Path(profile)
    with directory(profile):pass
    for name in ('lab','scripts','repo_sync'):
        if (profile/name).exists() or (profile/name).is_symlink():
            with directory(profile/name):pass
    files={}
    for p in (profile/'lab').rglob('*'):
        rel=p.relative_to(profile/'lab')
        if any(x in SKIP_DIRS for x in rel.parts):continue
        if p.is_symlink():raise Blocked('symlink in export scope: '+rel.as_posix())
        if not p.is_file():continue
        allowed=p.suffix in ('.py','.md','.html','.patch','.diff')
        if p.suffix=='.json':
            allowed=rel.as_posix() in ('paper_config.json','paper_config_v2.json','paper_config_v3.json','operator_event_gate_config.example.json','discovery_config_v1.json','research_dashboard_config.example.json') or 'fixtures' in rel.parts or (rel.parts[0]=='staging' and p.name in ('paper_config.json','paper_config_v2.json','paper_config_v3.json'))
        if allowed:files['lab/'+rel.as_posix()]=safe_read(p,profile)
    for name in ('freshness_actual_failure.json','v2_timestamp_failure_actual.json'):
        p=profile/'lab/evidence'/name
        if p.exists() or p.is_symlink():files['lab/evidence/'+name]=safe_read(p,profile)
    for name in ('AGENTS.md','SOUL.md','versions.md','SPEC.md','OPERATING_AGREEMENT.md','open_questions.md'):
        # Personal identity and obsolete private mandate are not needed for public review.
        if visibility=='public' and name in ('SOUL.md','SPEC.md'):continue
        p=profile/name
        if p.is_file():
            raw=safe_read(p,profile)
            if visibility=='public' and name=='AGENTS.md':
                raw=re.sub(r'(?ms)^## 已知事實\n.*?(?=^## |\Z)',
                           '## Public context omission\n\nPersonal/account/environment metadata omitted for public review.\n\n',raw.decode()).encode()
            files['context/'+name]=raw
    scripts=list((profile/'scripts').glob('paper_health_*.py'))
    scripts += list((profile/'scripts').glob('paper_startup_*.py'))
    scripts += list((profile/'scripts').glob('public_source_timing_probe.py'))
    scripts += list((profile/'scripts').glob('paper_operator_event_gate.py'))
    # Literal scheduler entries only; never export a generic scripts directory/glob.
    for name in ('paper_review_sync.py','paper_review_sync_public.py'):
        sync_entry=profile/'scripts'/name
        if sync_entry.is_file():scripts.append(sync_entry)
    for p in scripts:
        if p.is_file():files['scripts/'+p.name]=safe_read(p,profile)
    sync=profile/'repo_sync'
    for p in sync.rglob('*'):
        rel=p.relative_to(sync)
        if '__pycache__' in rel.parts or not p.is_file():continue
        dest=None
        if rel.as_posix()=='review_sync.py' or (rel.parts[0]=='tests' and p.suffix=='.py'):dest='automation/'+rel.as_posix()
        elif rel.parts[0]=='docs':
            dest=Path(*rel.parts[1:]).as_posix()
            if dest not in DOC_FILES:raise Blocked('nonallowlisted documentation destination: '+dest)
        elif rel.parts[0]=='evidence' and p.suffix in ('.txt','.json','.md'):dest='evidence/sync/'+p.name
        if dest:
            if dest in files:raise Blocked('export destination collision: '+dest)
            files[dest]=safe_read(p,profile)
    return files


def main(argv=None):
    import argparse,time
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--repository',type=Path,required=True)
    parser.add_argument('--remote',required=True)
    parser.add_argument('--owner-repo',required=True)
    parser.add_argument('--visibility',choices=('private','public'),default='private')
    parser.add_argument('--authorize-public-repo',help='Explicit public export authorization; must equal --owner-repo')
    parser.add_argument('--gh',default=str(Path.home()/'.local/bin/gh'))
    parser.add_argument('--gitleaks',default=str(Path.home()/'.local/bin/gitleaks'))
    parser.add_argument('--force',action='store_true')
    args=parser.parse_args(argv)
    def lookup(name):
        return json.loads(run([args.gh,'api','repos/'+name]).stdout)
    def scan(repository):
        # Explicit default-rule extension defeats repository config and ignore files.
        with tempfile.TemporaryDirectory(prefix='review-scanner-') as td:
            policy=Path(td)/'policy.toml';atomic_write(policy,b'[extend]\nuseDefault = true\n')
            flags=['--config',str(policy),'--gitleaks-ignore-path','/dev/null','--redact','--no-banner','--ignore-gitleaks-allow']
            run([args.gitleaks,'dir',str(repository),*flags],cwd=td)
            if git(repository,'rev-parse','--verify','HEAD',check=False).returncode==0:
                run([args.gitleaks,'git',str(repository),*flags,'--log-opts=--all --reflog'],cwd=td)
    result=tick(args.profile/'repo_sync/status.json',lambda:collect(args.profile,visibility=args.visibility),
                lambda files:publish(files,args.repository,args.remote,args.owner_repo,lookup,scan,
                                     visibility=args.visibility,public_authorization=args.authorize_public_repo),time.time(),args.force)
    print(json.dumps(result,sort_keys=True))
    return 1 if result['state']=='blocked' else 0


if __name__=='__main__':raise SystemExit(main())
