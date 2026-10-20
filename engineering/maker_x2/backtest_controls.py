"""Synthetic integration gate used by CLI --self-test, no market files."""
from decimal import Decimal as D
from account_lifecycle import OfflineAccount
from account_ledger import replay_account
from liquidation import liquidation_proxy
from matched_signals import select_matched
from maker_exit_lab import X2Error


def synthetic_account():
    return OfflineAccount("1000","50","5","5",2,"24",".0145",".0005")


def fictional_bar(index, high="100.27005", low="99", close="100"):
    return {"ts":index*60000,"open":"100","high":high,
            "low":low,"close":close,"closed":True}


def self_test():
    """Deterministic monetary expectations independent of account summary."""
    a=synthetic_account()
    status=a.submit("FICTION","synthetic",0,"100","100","1",
                    ".25",2,"2","X2")
    assert status=="PENDING"
    a.bar(fictional_bar(0),entry_qty="2")
    ledger=replay_account("1000",a.events)
    assert ledger["cash"]==D("999.95990")
    assert ledger["fees"]==D(".04010")
    assert ledger["qty"]==D("2")
    assert ledger["reserved"]==D("50")
    assert a.snapshot()["unrealized"]==D(".50")
    assert a.snapshot()["equity"]==D("1000.45990")
    assert a.snapshot()["estimated_close_fee_not_paid"]>D("0")
    proxy=liquidation_proxy("100","102","1","2","50",
                            ".0145",".0005")
    assert proxy["margin_writeoff"]==D("25")
    assert proxy["fee"]==D(".05")
    sample=select_matched([{"framework_minutes":1,"signal_time":i*60000}
                           for i in range(10)],1,0,600000,count=10)
    assert sample["selected_timestamps"]==[i*60000 for i in range(10)]
    assert sample["status"]=="READY"
    return True
