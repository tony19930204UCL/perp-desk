import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

class ReviewSyncTests(unittest.TestCase):
    def test_public_mode_requires_exact_explicit_authorization_and_private_default(self):
        import review_sync as m
        import inspect
        self.assertIn('visibility',inspect.signature(m.publish).parameters,'explicit public opt-in missing')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,remote,pub=self.local_fixture(root)
            public=lambda name:{'private':False,'full_name':name}
            with self.assertRaises(m.Blocked):pub({'lab/a.py':b'pass\n'},lookup=public)
            for authorization in (None,'other/repo'):
                with self.subTest(authorization=authorization),self.assertRaises(m.Blocked):
                    pub({'lab/a.py':b'pass\n'},lookup=public,visibility='public',public_authorization=authorization)
            result=pub({'lab/a.py':b'print(2)\n'},lookup=public,visibility='public',public_authorization='owner/repo')
            self.assertEqual(result['state'],'pushed')
            self.assertEqual(m.git(repo,'ls-remote','--heads','origin','main').stdout.split()[0],result['sha'])
            with self.assertRaises(m.Blocked):
                pub({'lab/a.py':b'pass\n'},visibility='public',public_authorization='owner/repo')

    def test_public_metadata_is_strict_and_identity_precedes_side_effects(self):
        import review_sync as m
        from unittest.mock import Mock
        owner='tony19930204UCL/perp-desk';url='https://github.com/'+owner+'.git'
        invalid=[None,[],{}, {'private':False,'full_name':'other/perp-desk'}]
        invalid += [{'private':v,'full_name':owner} for v in (None,0,1,'false','true',True)]
        for metadata in invalid:
            with self.subTest(metadata=metadata),tempfile.TemporaryDirectory() as td:
                repo=Path(td)/'repo';scanner=Mock()
                with self.assertRaises(m.Blocked):
                    m.publish({'a.py':b'pass\n'},repo,url,owner,lambda n:metadata,scanner,
                              visibility='public',public_authorization=owner)
                scanner.assert_not_called();self.assertFalse(repo.exists())
        for bad in ('https://github.com/other/perp-desk.git',url+'?x=1','http://github.com/'+owner+'.git'):
            query=Mock()
            with tempfile.TemporaryDirectory() as td,self.assertRaises(m.Blocked):
                m.publish({},Path(td)/'repo',bad,owner,query,Mock(),visibility='public',public_authorization=owner)
            query.assert_not_called()
        for mode in ('PUBLIC','auto',None):
            with tempfile.TemporaryDirectory() as td,self.assertRaises(m.Blocked):
                m.publish({},Path(td)/'repo',url,owner,Mock(),Mock(),visibility=mode,public_authorization=owner)

    def test_public_cli_and_scheduler_require_exact_opt_in(self):
        import review_sync as m
        import subprocess,contextlib,io,json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'lab').mkdir();(root/'lab/a.py').write_text('pass\n')
            remote=root/'remote.git';subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
            gh=root/'gh';gh.write_text('#!/usr/bin/env python3\nprint(\'{"private":false,"full_name":"owner/repo"}\')\n');gh.chmod(0o700)
            scanner=root/'scanner';scanner.write_text('#!/bin/sh\nexit 0\n');scanner.chmod(0o700)
            base=['--profile',str(root),'--repository',str(root/'mirror'),'--remote',str(remote),'--owner-repo','owner/repo','--gh',str(gh),'--gitleaks',str(scanner),'--force']
            original=m.publish
            def local(*a,**kw):return original(*a,**kw,_test_remote=remote)
            with patch.object(m,'publish',local),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(base),1,'private default must reject public remote')
                try:result=m.main(base+['--visibility','public','--authorize-public-repo','owner/repo'])
                except SystemExit as error:result=error.code
                self.assertEqual(result,0,'CLI lacks authorized public mode')
                self.assertEqual(m.main(base+['--visibility','public']),1)
                self.assertEqual(m.main(base+['--visibility','public','--authorize-public-repo','other/repo']),1)
                scanner.write_text('#!/bin/sh\nexit 1\n')
                self.assertEqual(m.main(base+['--visibility','public','--authorize-public-repo','owner/repo']),1)
            self.assertEqual(json.loads((root/'repo_sync/status.json').read_text())['state'],'blocked')
        wrapper=(Path(__file__).resolve().parents[2]/'scripts/paper_review_sync.py').read_text()
        self.assertIn("'--visibility','private'",wrapper)
        self.assertNotIn("'--authorize-public-repo'",wrapper)

    def test_public_fetches_all_remote_refs_before_security_scan(self):
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,remote,pub=self.local_fixture(root)
            branch=root/'reviewer';m.run(['git','-c','init.templateDir=','clone','--no-hardlinks','--branch','main',str(remote),str(branch)])
            m.git(branch,'config','user.name','reviewer');m.git(branch,'config','user.email','review@example.invalid')
            m.git(branch,'checkout','-b','separate-review');(branch/'review.md').write_text('other remote history')
            m.git(branch,'add','review.md');m.git(branch,'commit','-m','independent branch');sha=m.git(branch,'rev-parse','HEAD').stdout.strip()
            m.git(branch,'push','origin','separate-review')
            def scanner(path):
                self.assertIn(sha,m.git(path,'rev-list','--all').stdout.splitlines(),'remote non-main history omitted from scan')
            pub({'lab/a.py':b'print(2)\n'},lookup=lambda n:{'private':False,'full_name':n},
                visibility='public',public_authorization='owner/repo',scan=scanner)

    def test_public_collection_omits_personal_account_context_but_keeps_review_rules(self):
        import review_sync as m
        import inspect
        self.assertIn('visibility',inspect.signature(m.collect).parameters,'public privacy policy missing')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            agents='## Review rules\nPAPER only\n## 已知事實\nprivate account metadata\n## Engineering\nDo not deploy\n'
            (root/'AGENTS.md').write_text(agents)
            for name in ('SOUL.md','SPEC.md'):(root/name).write_text('private observer identity\n')
            (root/'lab').mkdir();(root/'lab/a.py').write_text('pass\n')
            raw=m.collect(root);public=m.collect(root,visibility='public')
            self.assertEqual(raw['context/AGENTS.md'],agents.encode())
            self.assertNotIn(b'private account metadata',public['context/AGENTS.md'])
            self.assertIn(b'PAPER only',public['context/AGENTS.md']);self.assertIn(b'Do not deploy',public['context/AGENTS.md'])
            for name in ('SOUL.md','SPEC.md'):self.assertNotIn('context/'+name,public)
            self.assertEqual(public['lab/a.py'],raw['lab/a.py'])
            self.assertEqual(set(public),set(raw)-{'context/SOUL.md','context/SPEC.md'})
            with self.assertRaises(m.Blocked):m.collect(root,visibility='auto')

    def test_scheduler_wrapper_is_profile_bound_and_script_only(self):
        p=Path(__file__).resolve().parents[2]/'scripts/paper_review_sync.py'
        self.assertTrue(p.exists(),'deployed no-agent scheduler entry missing')
        source=p.read_text()
        self.assertIn('tony19930204UCL/perp-desk',source)
        self.assertIn('/home/chihcheng/.hermes/profiles/perp-desk',source)
        self.assertNotIn('subprocess',source)
        self.assertIn('main(',source)
        import review_sync as m
        self.assertTrue('scripts/paper_review_sync.py' in m.collect(Path(__file__).resolve().parents[2]),'scheduler entry absent from export')

    def test_git_uses_only_fixed_official_github_credential_helper(self):
        import review_sync as m
        from unittest.mock import patch
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            with patch.object(m,'run',return_value=subprocess.CompletedProcess([],0,'','')) as command:
                m.git(Path(td),'version')
            args=command.call_args.args[0]
            self.assertIn('credential.helper=',args)
            trusted='credential.https://github.com.helper=!'+str(Path.home()/'.local/bin/gh')+' auth git-credential'
            self.assertIn(trusted,args)
            self.assertLess(args.index('credential.helper='),args.index(trusted))

    def local_fixture(self,root):
        import subprocess
        from functools import partial
        import review_sync as m
        remote=root/'remote.git';repo=root/'repo'
        subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
        pub=partial(m.publish,repository=repo,remote_url=str(remote),owner_repo='owner/repo',
                    lookup=lambda name:{'private':True,'full_name':name},scan=lambda path:None,_test_remote=remote)
        pub({'lab/a.py':b'pass\n'})
        return repo,remote,pub

    def test_tick_rejects_preexisting_symlinks_before_outside_io(self):
        import review_sync as m
        import builtins,os
        from unittest.mock import patch
        for target in ('profile','status','lock'):
            with self.subTest(target=target),tempfile.TemporaryDirectory() as td:
                root=Path(td);outside=root/'outside';outside.mkdir()
                profile=root/'profile';profile.mkdir();sync=profile/'repo_sync';sync.mkdir()
                state=sync/'status.json';victim=outside/'status.json';victim.write_text('{}')
                outside_lock=outside/'status.json.lock';reads=[];writes=[]
                if target=='profile':
                    sync.rmdir();profile.rmdir();profile.symlink_to(outside,target_is_directory=True)
                    (outside/'repo_sync').mkdir();victim=outside/'repo_sync/status.json';victim.write_text('{}')
                    outside_lock=outside/'repo_sync/status.json.lock'
                elif target=='status':state.symlink_to(victim)
                else:Path(str(state)+'.lock').symlink_to(outside_lock)
                real_read=Path.read_text;real_open=builtins.open;real_os_open=os.open
                def descriptor_open(path,flags,*args,**kwargs):
                    fd=real_os_open(path,flags,*args,**kwargs)
                    if Path('/proc/self/fd/'+str(fd)).resolve().is_relative_to(outside):
                        (writes if flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT) else reads).append(str(path))
                    return fd
                def read(path,*args,**kwargs):
                    if path.resolve().is_relative_to(outside):reads.append(str(path))
                    return real_read(path,*args,**kwargs)
                def open_file(path,*args,**kwargs):
                    if Path(path).resolve().is_relative_to(outside):writes.append(str(path))
                    return real_open(path,*args,**kwargs)
                with patch.object(Path,'read_text',read),patch.object(builtins,'open',open_file),patch.object(os,'open',descriptor_open):
                    with self.assertRaises(m.Blocked):
                        m.tick(state,lambda:m.collect(profile),lambda files:{'state':'pushed'},100,force=True)
                self.assertEqual(reads,[],'outside status read before rejection')
                self.assertEqual(writes,[],'outside lock opened before rejection')
                self.assertFalse(outside_lock.exists(),'outside lock created before rejection')
                self.assertEqual(victim.read_text(),'{}')

    def test_git_directory_symlinks_rejected_before_config_read(self):
        import review_sync as m
        from unittest.mock import patch
        for target in ('.git','.git/config','.git/refs/heads'):
            with tempfile.TemporaryDirectory() as td:
                root=Path(td);repo,remote,pub=self.local_fixture(root)
                path=repo/target;backup=root/'original';path.rename(backup);path.symlink_to(backup,target_is_directory=backup.is_dir())
                with patch.object(m,'validate_git_config',side_effect=AssertionError('unsafe config read')):
                    with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(2)\n'})

    def test_mirror_parent_swap_before_read_or_delete_never_follows_symlink(self):
        import review_sync as m
        from unittest.mock import patch
        for action in ('read','delete'):
            with tempfile.TemporaryDirectory() as td:
                root=Path(td);repo=root/'repo';outside=root/'outside';outside.mkdir()
                m.mirror({'lab/a.py':b'pass\n'},repo)
                victim=outside/'a.py';victim.write_bytes(b'private')
                real=Path.is_symlink;triggered=[]
                def swap(path):
                    result=real(path)
                    if path==repo/'lab' and not triggered:
                        triggered.append(True);path.rename(repo/'moved');path.symlink_to(outside,target_is_directory=True)
                        return False
                    return result
                with patch.object(Path,'is_symlink',swap):
                    with self.assertRaises(m.Blocked):m.mirror({'lab/a.py':b'print(2)\n'} if action=='read' else {},repo)
                self.assertEqual(victim.read_bytes(),b'private')

    def test_transient_git_lock_disappearance_restarts_complete_inspection(self):
        import review_sync as m
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,remote,pub=self.local_fixture(root)
            lock=repo/'.git/maintenance.lock';lock.write_text('transient')
            real_stat=os.stat;events=[]
            def disappearing(path,*args,**kwargs):
                if path=='maintenance.lock' and kwargs.get('dir_fd') is not None and not events:
                    events.append('removed');lock.unlink()
                    raise FileNotFoundError(2,'synthetic transient disappearance','maintenance.lock')
                return real_stat(path,*args,**kwargs)
            with patch.object(os,'stat',disappearing):
                m.check_private_tree(repo/'.git')
            self.assertEqual(events,['removed'])
            # A full retry must still enforce security, not bless the tree after ENOENT.
            unsafe=repo/'.git/index';unsafe.chmod(unsafe.stat().st_mode|0o020)
            try:
                with self.assertRaisesRegex(m.Blocked,'permissions'):m.check_private_tree(repo/'.git')
            finally:unsafe.chmod(0o600)

    def test_persistent_git_tree_churn_is_bounded_and_blocked(self):
        import review_sync as m
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            repo,remote,pub=self.local_fixture(Path(td));calls=[]
            real=m._check_private_tree_once
            def changing(path):
                calls.append(Path(path).name)
                raise m._TransientGitTreeChange('maintenance.lock')
            with patch.object(m,'_check_private_tree_once',changing):
                with self.assertRaisesRegex(m.Blocked,'changed during inspection'):
                    m.check_private_tree(repo/'.git')
            self.assertEqual(len(calls),3,'transient handling must remain strictly bounded')
            # Control: implementation still delegates to the real strict pass afterwards.
            m._check_private_tree_once=real

    def test_group_writable_git_entries_fail_before_scanner_or_hooks(self):
        import review_sync as m
        from unittest.mock import Mock
        for target in ('.git','.git/index'):
            with self.subTest(target=target),tempfile.TemporaryDirectory() as td:
                root=Path(td);repo,remote,pub=self.local_fixture(root)
                before=m.git(repo,'rev-parse','HEAD').stdout.strip()
                unsafe=repo/target;unsafe.chmod(unsafe.stat().st_mode|0o020);scanner=Mock()
                with self.assertRaisesRegex(m.Blocked,'permissions'):
                    pub({'lab/a.py':b'print(2)\n'},scan=scanner)
                scanner.assert_not_called();unsafe.chmod(0o700 if unsafe.is_dir() else 0o600)
                self.assertEqual(m.git(repo,'rev-parse','HEAD').stdout.strip(),before)

    def test_existing_hook_files_never_execute(self):
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,remote,pub=self.local_fixture(root)
            m.git(repo,'config','--unset','core.hooksPath')
            marker=root/'executed';hook=repo/'.git/hooks/pre-commit';hook.parent.mkdir(mode=0o700,exist_ok=True)
            hook.parent.chmod(0o700)  # Match the owner-private Git tree contract, independent of umask.
            hook.write_text('#!/bin/sh\ntouch '+str(marker)+'\n');hook.chmod(0o700)
            pub({'lab/a.py':b'print(2)\n'})
            self.assertFalse(marker.exists())

    def test_cli_scanner_policy_cannot_be_overridden_by_export_or_environment(self):
        import review_sync as m
        import os,json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,remote,pub=self.local_fixture(root)
            (root/'lab').mkdir();(root/'lab/a.py').write_text('pass\n')
            gh=root/'gh';gh.write_text('#!/usr/bin/env python3\nprint(\'{"private":true,"full_name":"owner/repo"}\')\n');gh.chmod(0o755)
            log=root/'scanner-log';scanner=root/'scanner'
            scanner.write_text('#!/usr/bin/env python3\nimport sys,json,os,pathlib\na=sys.argv[1:]\nr={"args":a,"cwd":os.getcwd(),"env":{k:v for k,v in os.environ.items() if k.startswith("GITLEAKS")}}\nif "--config" in a:r["config"]=pathlib.Path(a[a.index("--config")+1]).read_text()\nwith open('+repr(str(log))+',"a") as f:f.write(json.dumps(r)+"\\n")\n');scanner.chmod(0o755)
            args=['--profile',str(root),'--repository',str(repo),'--remote',str(remote),'--owner-repo','owner/repo','--gh',str(gh),'--gitleaks',str(scanner),'--force']
            original=m.publish;real_tree=m._check_private_tree_once;transient=[]
            def local(*a,**kw):return original(*a,**kw,_test_remote=remote)
            def once(path):
                if not transient:
                    transient.append('maintenance.lock')
                    raise m._TransientGitTreeChange('maintenance.lock')
                return real_tree(path)
            with patch.object(m,'publish',local),patch.object(m,'_check_private_tree_once',once),patch.dict(os.environ,{'GITLEAKS_CONFIG':'/evil','GITLEAKS_CONFIG_TOML':'[allowlist]\npaths=[".*"]'}):
                self.assertEqual(m.main(args),0)
            self.assertEqual(transient,['maintenance.lock'])
            records=[json.loads(line) for line in log.read_text().splitlines()]
            self.assertTrue(records)
            history=[record for record in records if record['args'][0]=='git']
            self.assertTrue(history)
            for record in history:self.assertIn('--log-opts=--all --reflog',record['args'])
            for record in records:
                self.assertIn('--config',record['args']);self.assertIn('--gitleaks-ignore-path',record['args'])
                self.assertEqual(record['args'][record['args'].index('--gitleaks-ignore-path')+1],'/dev/null')
                self.assertNotIn('--ignore-path',record['args'])
                self.assertEqual(record['env'],{});self.assertEqual(record['config'],'[extend]\nuseDefault = true\n')
                self.assertNotEqual(record['cwd'],str(repo))

    def test_crash_after_commit_recovers_only_exact_transaction_commit(self):
        import review_sync as m
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            repo,remote,pub=self.local_fixture(Path(td));real_git=m.git
            def crash(repository,*args,**kwargs):
                result=real_git(repository,*args,**kwargs)
                if args[0]=='commit':raise m.Blocked('process interrupted after commit')
                return result
            with patch.object(m,'git',crash):
                with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(2)\n'})
            committed=m.git(repo,'rev-parse','HEAD').stdout.strip()
            result=pub({'lab/a.py':b'print(2)\n'})
            self.assertEqual(result['sha'],committed);self.assertEqual(result['state'],'pushed')

    def test_exporter_owned_staged_commit_failure_retries_but_unknown_stage_blocks(self):
        import review_sync as m
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            repo,remote,pub=self.local_fixture(Path(td));real_git=m.git
            def fail_commit(repository,*args,**kwargs):
                if args[0]=='commit':raise m.Blocked('transient commit failure')
                return real_git(repository,*args,**kwargs)
            with patch.object(m,'git',fail_commit):
                with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(2)\n'})
            self.assertTrue(m.git(repo,'diff','--cached','--name-only').stdout)
            self.assertEqual(pub({'lab/a.py':b'print(3)\n'})['state'],'pushed')
            self.assertFalse(m.git(repo,'diff','--cached','--name-only').stdout)
            with patch.object(m,'git',fail_commit):
                with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(4)\n'})
            (repo/'unknown.txt').write_text('unowned staged data');m.git(repo,'add','unknown.txt')
            with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(4)\n'})
            self.assertIn('unknown.txt',m.git(repo,'diff','--cached','--name-only').stdout)

    def test_scanner_failure_new_unstaged_file_removed_next_snapshot_converges(self):
        import review_sync as m
        import json,subprocess
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,remote,pub=self.local_fixture(root)
            before=m.git(repo,'rev-parse','HEAD').stdout.strip()
            ledger=root/'.repo.review-sync/state.json';failed=[]
            def fail(path):
                failed.append(path)
                self.assertTrue((path/'lab/transient.py').is_file())
                self.assertNotIn('lab/transient.py',m.git(path,'ls-files','-z').stdout.split('\0'))
                raise m.Blocked('scanner transient failure')
            with self.assertRaisesRegex(m.Blocked,'scanner transient failure'):
                pub({'lab/a.py':b'print(2)\n','lab/transient.py':b'transient\n'},scan=fail)
            self.assertEqual(failed,[repo])
            self.assertIn('lab/transient.py',json.loads(ledger.read_text())['pending_owned'])
            self.assertEqual(m.git(repo,'rev-parse','HEAD').stdout.strip(),before)
            read_remote=lambda:subprocess.run(['git','--git-dir',str(remote),'rev-parse','refs/heads/main'],check=True,capture_output=True,text=True).stdout.strip()
            self.assertEqual(read_remote(),before)
            result=pub({'lab/a.py':b'print(2)\n'})
            self.assertEqual(result['state'],'pushed');self.assertEqual(read_remote(),result['sha'])
            self.assertFalse((repo/'lab/transient.py').exists())
            self.assertEqual(set(m.git(repo,'ls-tree','-r','--name-only','HEAD').stdout.splitlines()),{'lab/a.py','source_manifest.json'})
            self.assertEqual(json.loads(ledger.read_text())['owned_paths'],['lab/a.py'])
            self.assertEqual(pub({'lab/a.py':b'print(2)\n'})['state'],'unchanged')

    def test_scanner_failure_preserves_deletions_for_retry(self):
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            repo,remote,pub=self.local_fixture(Path(td))
            pub({'lab/a.py':b'pass\n','lab/old.py':b'old\n'})
            def fail(path):raise m.Blocked('scanner transient failure')
            with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(2)\n'},scan=fail)
            self.assertFalse((repo/'lab/old.py').exists())
            result=pub({'lab/a.py':b'print(2)\n'})
            self.assertEqual(result['state'],'pushed')
            self.assertNotIn('lab/old.py',m.git(repo,'ls-tree','-r','--name-only','HEAD').stdout.splitlines())
            self.assertFalse(any('review-sync' in p for p in m.git(repo,'ls-tree','-r','--name-only','HEAD').stdout.splitlines()))

    def test_entire_outgoing_tree_must_match_snapshot_bytes(self):
        import review_sync as m
        for mutation in ('owned','unowned'):
            with tempfile.TemporaryDirectory() as td:
                repo,remote,pub=self.local_fixture(Path(td));before=m.git(repo,'rev-parse','HEAD').stdout.strip()
                def scanner(path):
                    if mutation=='owned':(path/'lab/a.py').write_text('not the source bytes')
                    else:
                        (path/'private.txt').write_text('not owned');m.git(path,'add','private.txt')
                with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(2)\n'},scan=scanner)
                self.assertEqual(m.git(repo,'rev-parse','HEAD').stdout.strip(),before)

    def test_unknown_descendant_history_is_never_published(self):
        import subprocess
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            repo,remote,pub=self.local_fixture(Path(td))
            before=m.git(repo,'rev-parse','HEAD').stdout.strip()
            p=repo/'private.txt';p.write_text('out of scope')
            m.git(repo,'add','private.txt');m.git(repo,'commit','-m','unowned intermediate')
            m.git(repo,'rm','private.txt');m.git(repo,'commit','-m','hide unowned intermediate')
            local=m.git(repo,'rev-parse','HEAD').stdout.strip()
            with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(2)\n'})
            self.assertEqual(m.git(repo,'rev-parse','HEAD').stdout.strip(),local)
            remote_head=subprocess.run(['git','--git-dir',str(remote),'rev-parse','refs/heads/main'],check=True,capture_output=True,text=True).stdout.strip()
            self.assertEqual(remote_head,before)

    def test_pathspec_magic_is_staged_literally(self):
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,remote,pub=self.local_fixture(root)
            (repo/'private-note.txt').write_text('not exporter owned')
            pub({'lab/a.py':b'pass\n',':(glob)**':b'literal file'})
            paths=m.git(repo,'ls-tree','-r','--name-only','HEAD').stdout.splitlines()
            self.assertIn(':(glob)**',paths);self.assertNotIn('private-note.txt',paths)

    def test_erroneous_python_is_exported_as_raw_review_evidence(self):
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'lab').mkdir();p=root/'lab/broken.py';p.write_bytes(b'def broken(')
            self.assertEqual(m.collect(root)['lab/broken.py'],b'def broken(')

    def test_exact_reviewed_lab_failure_evidence_is_exported(self):
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            names=('freshness_actual_failure.json','v2_timestamp_failure_actual.json','unreviewed.json')
            for name in names:
                p=root/'lab/evidence'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{"failure":true}')
            files=m.collect(root)
            self.assertEqual(set(files),{'lab/evidence/'+name for name in names[:2]})
            self.assertEqual(files['lab/evidence/freshness_actual_failure.json'],b'{"failure":true}')

    def test_flattened_evidence_collisions_fail_closed(self):
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for name in ('first/result.txt','second/result.txt'):
                p=root/'repo_sync/evidence'/name;p.parent.mkdir(parents=True);p.write_text(name)
            with self.assertRaises(m.Blocked):m.collect(root)

    def test_docs_reject_nonallowlisted_names_before_read(self):
        import review_sync as m
        from unittest.mock import patch
        for name in ('.env','credentials.txt','source_manifest.json','.gitleaks.toml',
                     'lab/a.py','automation/review_sync.py','evidence/sync/test.txt','docs/random.pem'):
            with tempfile.TemporaryDirectory() as td:
                root=Path(td);p=root/'repo_sync/docs'/name;p.parent.mkdir(parents=True);p.write_text('credential material')
                with patch.object(m,'safe_read',side_effect=AssertionError('must reject before read')):
                    with self.assertRaises(m.Blocked):m.collect(root)

    def test_filesystem_roots_and_manifest_symlinks_fail_before_io(self):
        import review_sync as m
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);outside=root/'outside';outside.mkdir()
            link=root/'link';link.symlink_to(outside,target_is_directory=True)
            for repo in (link,link/'nested'):
                with self.assertRaises(m.Blocked):m.mirror({'a.py':b'pass\n'},repo)
            self.assertEqual(list(outside.iterdir()),[])
            (outside/'lab').mkdir();(outside/'lab/a.py').write_text('pass\n')
            with self.assertRaises(m.Blocked):m.collect(link)
            repo=root/'repo';repo.mkdir();manifest=repo/'source_manifest.json'
            private=root/'private';private.write_text('{"files":{}}')
            manifest.symlink_to(private)
            with patch.object(Path,'read_text',side_effect=AssertionError('unsafe read')):
                with self.assertRaises(m.Blocked):m.mirror({'a.py':b'pass\n'},repo)

    def test_inherited_git_environment_cannot_redirect_or_execute(self):
        import os,subprocess
        import review_sync as m
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,remote,pub=self.local_fixture(root)
            marker=root/'executed';global_config=root/'global'
            global_config.write_text('[core]\n hooksPath = '+str(root/'hooks')+'\n')
            hooks=root/'hooks';hooks.mkdir();hook=hooks/'pre-commit'
            hook.write_text('#!/bin/sh\ntouch '+str(marker)+'\n');hook.chmod(0o755)
            env={'GIT_DIR':str(root/'wrong.git'),'GIT_WORK_TREE':str(root/'wrong'),
                 'GIT_INDEX_FILE':str(root/'wrong-index'),'GIT_CONFIG_GLOBAL':str(global_config),
                 'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'core.hooksPath','GIT_CONFIG_VALUE_0':str(hooks),
                 'GIT_CONFIG_PARAMETERS':"'core.hooksPath='+str(hooks)+'\"",'GIT_TEMPLATE_DIR':str(hooks)}
            with patch.dict(os.environ,env):
                result=pub({'lab/a.py':b'print(2)\n'})
            self.assertEqual(result['state'],'pushed');self.assertFalse(marker.exists())
            self.assertFalse((root/'wrong-index').exists())

    def test_existing_git_execution_config_is_rejected_before_scan(self):
        import subprocess
        import review_sync as m
        from unittest.mock import Mock
        for key,value in (('core.hooksPath','/tmp/evil'),('filter.evil.clean','touch /tmp/evil'),
                          ('include.path','/tmp/evil'),('core.worktree','/tmp/evil'),
                          ('credential.helper','!touch /tmp/evil'),('url.file:///tmp/.insteadOf','https://github.com/')):
            with tempfile.TemporaryDirectory() as td:
                repo,remote,pub=self.local_fixture(Path(td))
                subprocess.run(['git','config',key,value],cwd=repo,check=True)
                scanner=Mock()
                with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(2)\n'},scan=scanner)
                scanner.assert_not_called()

    def test_secondary_push_url_is_rejected(self):
        import subprocess
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);repo,remote,pub=self.local_fixture(root)
            secondary=root/'secondary.git'
            subprocess.run(['git','init','--bare',str(secondary)],check=True,capture_output=True)
            for url in (remote,secondary):
                subprocess.run(['git','config','--add','remote.origin.pushurl',str(url)],cwd=repo,check=True)
            with self.assertRaises(m.Blocked):pub({'lab/a.py':b'print(2)\n'})
            result=subprocess.run(['git','--git-dir',str(secondary),'show-ref'],capture_output=True)
            self.assertNotEqual(result.returncode,0)

    def test_production_url_is_bound_before_metadata_or_network(self):
        import review_sync as m
        from unittest.mock import Mock
        for url in ('https://github.com/other/repo.git', 'http://github.com/owner/repo.git',
                    'https://github.com/owner/repo.git?x=1', 'https://user@github.com/owner/repo.git',
                    '/local/remote', 'https://github.com/owner/repo'):
            lookup=Mock(return_value={'private':True,'full_name':'owner/repo'})
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(m.Blocked):
                    m.publish({'a.py':b'pass\n'},Path(td)/'repo',url,'owner/repo',lookup,lambda p:None)
                lookup.assert_not_called()

    def test_cli_runs_real_private_metadata_and_secret_scanner(self):
        import review_sync as m
        self.assertTrue(hasattr(m,'main'),'scheduled production CLI missing')
        import json,os
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'lab').mkdir();(root/'lab/a.py').write_text('pass\n')
            remote=root/'remote.git'
            import subprocess
            subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
            gh=root/'gh';gh.write_text('#!/usr/bin/env python3\nimport json\nprint(json.dumps({"private":True,"full_name":"owner/repo"}))\n');gh.chmod(0o755)
            scanner=root/'gitleaks';scanner.write_text('#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n');scanner.chmod(0o755)
            args=['--profile',str(root),'--repository',str(root/'mirror'),'--remote',str(remote),'--owner-repo','owner/repo','--gh',str(gh),'--gitleaks',str(scanner),'--force']
            from unittest.mock import patch
            from functools import partial
            publish=m.publish
            with patch.object(m,'publish',partial(publish,_test_remote=remote)):
                self.assertEqual(m.main(args),0)
                state=json.loads((root/'repo_sync/status.json').read_text());self.assertEqual(state['state'],'pushed')
                self.assertEqual(m.main(args),0)
                scanner.write_text('#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n')
                self.assertEqual(m.main(args),1)
            self.assertEqual(json.loads((root/'repo_sync/status.json').read_text())['state'],'blocked')

    def test_framework_docs_automation_and_evidence_are_exported_without_status(self):
        import review_sync as m
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for name in ('repo_sync/review_sync.py','repo_sync/tests/test_sync.py','repo_sync/docs/README.md',
                         'repo_sync/docs/.github/workflows/ci.yml','repo_sync/docs/docs/HEALTH_V3_OPERATOR.md',
                         'repo_sync/docs/docs/STORAGE_PROTECTION_V3.md',
                         'repo_sync/docs/docs/STARTUP_RECOVERY_V3.md',
                         'repo_sync/docs/docs/SOURCE_TIMING_EVIDENCE.md',
                         'repo_sync/docs/docs/MULTI_LEDGER_DASHBOARD.md',
                         'repo_sync/docs/docs/OPERATOR_EVENT_GATE.md',
                         'repo_sync/docs/docs/OWNER_RESUMPTION_HANDOFF.md',
                         'lab/operator_event_gate_config.example.json',
                         'scripts/paper_startup_supervisor.py','scripts/public_source_timing_probe.py',
                         'scripts/paper_operator_event_gate.py',
                         'repo_sync/evidence/green1.txt','repo_sync/status.json'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('pass\n' if p.suffix=='.py' else '{}')
            files=m.collect(root)
            self.assertIn('automation/review_sync.py',files)
            self.assertIn('README.md',files)
            self.assertIn('.github/workflows/ci.yml',files)
            self.assertIn('docs/HEALTH_V3_OPERATOR.md',files,'operator health contract must survive normal mirror export')
            self.assertIn('docs/STORAGE_PROTECTION_V3.md',files,'storage protection contract must survive normal mirror export')
            self.assertIn('docs/STARTUP_RECOVERY_V3.md',files,'startup recovery contract must survive normal mirror export')
            self.assertIn('scripts/paper_startup_supervisor.py',files,'startup supervisor entrypoint must survive normal mirror export')
            self.assertIn('docs/SOURCE_TIMING_EVIDENCE.md',files,'timing evidence contract must survive normal mirror export')
            self.assertIn('docs/MULTI_LEDGER_DASHBOARD.md',files,'multi-ledger dashboard contract must survive normal mirror export')
            self.assertIn('docs/OPERATOR_EVENT_GATE.md',files,'operator event-gate contract must survive normal mirror export')
            self.assertIn('docs/OWNER_RESUMPTION_HANDOFF.md',files,'owner resumption contract must survive normal mirror export')
            self.assertIn('lab/operator_event_gate_config.example.json',files,'disabled event-gate config template must survive export')
            self.assertIn('scripts/paper_operator_event_gate.py',files,'Hermes event-gate precheck wrapper must survive export')
            self.assertIn('scripts/public_source_timing_probe.py',files,'public timing probe must survive normal mirror export')
            self.assertIn('evidence/sync/green1.txt',files)
            self.assertNotIn('automation/status.json',files)

    def test_scheduled_tick_debounces_records_failures_and_recovers(self):
        import review_sync as m
        self.assertTrue(hasattr(m,'tick'),'persistent scheduled tick missing')
        with tempfile.TemporaryDirectory() as td:
            state=Path(td)/'state.json';calls=[]
            files={'a.py':b'pass\n'}
            def pub(f):calls.append(f);return {'state':'pushed','sha':'actual-test-sha'}
            self.assertEqual(m.tick(state,lambda:files,pub,100)['state'],'settling')
            self.assertEqual(m.tick(state,lambda:files,pub,105)['state'],'settling')
            self.assertEqual(m.tick(state,lambda:files,pub,120)['state'],'pushed')
            self.assertEqual(len(calls),1)
            def bad(f):raise m.Blocked('network failed')
            result=m.tick(state,lambda:files,bad,150)
            self.assertEqual(result['state'],'blocked');self.assertEqual(result['last_success_at'],120)
            self.assertEqual(m.tick(state,lambda:files,pub,180)['state'],'pushed')
            self.assertEqual(m.tick(state,lambda:files,pub,180,force=True)['state'],'pushed')

    def test_real_git_push_no_change_and_remote_divergence_fail_closed(self):
        import review_sync as m
        self.assertTrue(hasattr(m,'publish'),'deterministic git publication missing')
        import subprocess
        def git(*args,cwd=None):
            p=subprocess.run(['git',*args],cwd=cwd,capture_output=True,text=True);self.assertEqual(p.returncode,0,p.stderr);return p.stdout.strip()
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);remote=base/'remote.git';git('init','--bare',str(remote));repo=base/'repo'
            query=lambda name:{'private':True,'full_name':name}
            scan=lambda path:None
            from functools import partial
            publish=partial(m.publish,_test_remote=remote)
            a=publish({'lab/a.py':b'print(1)\n'},repo,str(remote),'owner/repo',query,scan)
            self.assertEqual(a['state'],'pushed');self.assertEqual(git('--git-dir',str(remote),'rev-parse','refs/heads/main'),a['sha'])
            b=publish({'lab/a.py':b'print(1)\n'},repo,str(remote),'owner/repo',query,scan)
            self.assertEqual(b['state'],'unchanged');self.assertEqual(a['sha'],b['sha'])
            c=publish({'lab/a.py':b'print(2)\n'},repo,str(remote),'owner/repo',query,scan);self.assertNotEqual(a['sha'],c['sha'])
            with self.assertRaises(m.Blocked):publish({'lab/a.py':b'print(3)\n'},repo,str(remote),'owner/repo',lambda name:{'private':False,'full_name':name},scan)
            self.assertEqual(git('--git-dir',str(remote),'rev-parse','refs/heads/main'),c['sha'])
            other=base/'other';git('clone','--branch','main',str(remote),str(other));git('config','user.name','reviewer',cwd=other);git('config','user.email','review@example.invalid',cwd=other)
            (other/'review.md').write_text('independent review');git('add','review.md',cwd=other);git('commit','-m','review',cwd=other);git('push',cwd=other);ahead=git('rev-parse','HEAD',cwd=other)
            with self.assertRaises(m.Blocked):publish({'lab/a.py':b'print(4)\n'},repo,str(remote),'owner/repo',query,scan)
            self.assertEqual(git('--git-dir',str(remote),'rev-parse','refs/heads/main'),ahead)

    def test_atomic_mirror_provenance_deletes_only_owned_files(self):
        import review_sync as m
        self.assertTrue(hasattr(m,'mirror'),'provenance mirror missing')
        import json,hashlib
        with tempfile.TemporaryDirectory() as td:
            repo=Path(td)/'repo';repo.mkdir();(repo/'personal-note.md').write_text('preserve')
            first={'lab/a.py':b'print(1)\n','docs/old.md':b'old'}
            m.mirror(first,repo)
            manifest=json.loads((repo/'source_manifest.json').read_text())
            self.assertEqual(manifest['files']['lab/a.py'],hashlib.sha256(first['lab/a.py']).hexdigest())
            self.assertNotIn('captured_at',manifest)
            m.mirror({'lab/a.py':b'print(2)\n'},repo)
            self.assertFalse((repo/'docs/old.md').exists());self.assertEqual((repo/'personal-note.md').read_text(),'preserve')
            before=(repo/'lab/a.py').read_bytes()
            for path in ('../escape','.git/config','/absolute'):
                with self.assertRaises(m.Blocked):m.mirror({path:b'bad'},repo)
            self.assertEqual((repo/'lab/a.py').read_bytes(),before)

    def test_export_rejects_symlinks_secrets_binary_and_oversize(self):
        import review_sync as m
        self.assertTrue(hasattr(m,'Blocked'),'fail-closed export guard missing')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'lab').mkdir();outside=root/'outside';outside.write_text('outside data')
            p=root/'lab/a.py';p.symlink_to(outside)
            with self.assertRaises(m.Blocked):m.collect(root)
            p.unlink();p.write_bytes(b'print(1)\x00')
            with self.assertRaises(m.Blocked):m.collect(root)
            p.write_text('token = '+repr('ghp_'+'a'*36))
            with self.assertRaises(m.Blocked):m.collect(root)
            p.write_bytes(b'#'+b'a'*(2*1024*1024))
            with self.assertRaises(m.Blocked):m.collect(root)
            p.write_text('def broken(')
            self.assertEqual(m.collect(root)['lab/a.py'],b'def broken(')
            p.unlink();(root/'lab/tests').symlink_to(outside.parent,target_is_directory=True)
            with self.assertRaises(m.Blocked):m.collect(root)

    def test_allowlisted_framework_only_no_runtime_or_credentials(self):
        try:
            import review_sync as m
        except ModuleNotFoundError:
            m=None
        self.assertIsNotNone(m,'review export implementation missing')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for path,text in {'lab/a.py':'print(1)\n','lab/tests/test_a.py':'pass\n','lab/paper_config_v2.json':'{}','lab/paper_config_v3.json':'{}','AGENTS.md':'project rules','lab/shared/status.json':'private runtime','lab/data/broker.sqlite3':'database','config.yaml':'private gateway','lab/.env':'private credential','lab/staging/x/probe-data/raw.json':'raw','lab/staging/x/a.py':'pass\n'}.items():
                p=root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
            files=m.collect(root)
            self.assertEqual(set(files),{'lab/a.py','lab/tests/test_a.py','lab/paper_config_v2.json','lab/paper_config_v3.json','context/AGENTS.md','lab/staging/x/a.py'})
            self.assertEqual(files['lab/a.py'],b'print(1)\n')

if __name__=='__main__':unittest.main()
