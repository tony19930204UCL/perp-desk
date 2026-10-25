"""Deterministic count-based ledger linearity, no wall clock claims."""
import unittest
from account_lifecycle import OfflineAccount
from account_ledger import replay_account

def exercise(n):
    a=OfflineAccount("1000","50","5","5",2,"24",".0145",".0005")
    a._emit("reserve","reserve",0,amount="50",episode="trial")
    for i in range(n):
        a.ledger()
        if i % 25 == 0:
            a._emit("funding","fund-"+str(i),(i+1)*60000,
                    settlement_id="settle-"+str(i),mark="100",rate=".0001")
        a.ledger()
    state=dict(a.ledger().items())
    expected=replay_account(a.capital,a.events,rates=a.rates)
    assert state==expected
    return a._applied_events,a._full_replays,len(a.events)

class Control(unittest.TestCase):
    def test_linear_counts(self):
        small=exercise(1000)
        large=exercise(4000)
        self.assertEqual(large[0],4*(small[0]-1)+1)
        self.assertEqual(large[2],4*(small[2]-1)+1)
        self.assertEqual(small[1],0)
        self.assertEqual(large[1],0)

    def test_read_only_calls(self):
        a=OfflineAccount("1000","50","5","5",2,"24",".0145",".0005")
        a._emit("reserve","reserve",0,amount="50",episode="one")
        a.ledger()
        count=a._applied_events
        for _ in range(2000):
            value=a.ledger()
            self.assertEqual(value["available"],950)
        self.assertEqual(a._applied_events,count)
        self.assertEqual(a._full_replays,0)
        a.verify_ledger()
        self.assertEqual(a._full_replays,1)
