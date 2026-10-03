"""Bounded PAPER storage protection. Observes capacity; never deletes or rewrites evidence."""
from dataclasses import dataclass
from pathlib import Path
import json
import shutil


class StorageProtectionHalt(RuntimeError):
    """Continuing could add risk or require a durable write without configured headroom."""


@dataclass(frozen=True)
class StoragePolicy:
    warning_bytes: int
    new_risk_limit_bytes: int
    exit_reserve_bytes: int
    min_free_bytes: int
    max_exit_cycle_bytes: int

    @property
    def hard_limit_bytes(self):
        return self.new_risk_limit_bytes + self.exit_reserve_bytes


def load_policy(path):
    data=json.loads(Path(path).read_text())
    if not isinstance(data,dict) or data.get('schema_version')!=1:
        raise ValueError('invalid storage policy schema')
    names=('warning_bytes','new_risk_limit_bytes','exit_reserve_bytes','min_free_bytes','max_exit_cycle_bytes')
    if any(type(data.get(name)) is not int or data[name]<=0 for name in names):
        raise ValueError('storage policy requires positive integer byte limits')
    policy=StoragePolicy(**{name:data[name] for name in names})
    if not policy.warning_bytes < policy.new_risk_limit_bytes:
        raise ValueError('warning must be below new-risk limit')
    if policy.max_exit_cycle_bytes > policy.exit_reserve_bytes:
        raise ValueError('exit-cycle bound cannot exceed exit reserve')
    return policy


def filesystem_probe(state_dir):
    """Bounded metadata-only observation; never opens database contents."""
    root=Path(state_dir).resolve()
    files={}
    total=0
    for entry in root.iterdir():
        if entry.is_file():
            size=entry.stat().st_size
            files[entry.name]=size
            total+=size
    disk=shutil.disk_usage(root)
    return dict(used_bytes=total,free_bytes=disk.free,total_bytes=disk.total,files=files)


class StorageGuard:
    def __init__(self,state_dir,policy,*,probe=None):
        self.state_dir=Path(state_dir).resolve()
        self.policy=policy
        self.probe=probe or filesystem_probe

    def observe(self):
        raw=self.probe(self.state_dir)
        used=raw.get('used_bytes');free=raw.get('free_bytes')
        if type(used) is not int or used<0 or type(free) is not int or free<0:
            raise ValueError('invalid storage observation')
        p=self.policy
        warning=used>=p.warning_bytes
        namespace_hard=used>=p.hard_limit_bytes
        free_for_exit=free-p.max_exit_cycle_bytes>=p.min_free_bytes
        namespace_for_exit=used+p.max_exit_cycle_bytes<=p.hard_limit_bytes
        exit_cycle_allowed=free_for_exit and namespace_for_exit
        new_risk_allowed=(used<p.new_risk_limit_bytes
                          and free-p.max_exit_cycle_bytes>=p.min_free_bytes)
        reasons=[]
        if warning: reasons.append('namespace_warning')
        if used>=p.new_risk_limit_bytes: reasons.append('new_risk_namespace_limit')
        if not free_for_exit: reasons.append('insufficient_free_space_for_exit_reserve')
        if not namespace_for_exit: reasons.append('exit_namespace_reserve_exhausted')
        level=('halt' if not exit_cycle_allowed
               else 'protect' if not new_risk_allowed
               else 'warning' if warning else 'normal')
        return dict(schema_version=1,level=level,reasons=reasons,
                    used_bytes=used,free_bytes=free,
                    warning_bytes=p.warning_bytes,
                    new_risk_limit_bytes=p.new_risk_limit_bytes,
                    hard_limit_bytes=p.hard_limit_bytes,
                    min_free_bytes=p.min_free_bytes,
                    max_exit_cycle_bytes=p.max_exit_cycle_bytes,
                    exit_reserve_bytes=p.exit_reserve_bytes,
                    new_risk_allowed=new_risk_allowed,
                    exit_accounting_cycle_allowed=exit_cycle_allowed,
                    disk_full=(free==0),
                    files=raw.get('files',{}))

    def require_exit_cycle(self,context):
        state=self.observe()
        if not state['exit_accounting_cycle_allowed']:
            raise StorageProtectionHalt(
                context+': durable exit/accounting headroom unavailable; '+','.join(state['reasons']))
        return state
