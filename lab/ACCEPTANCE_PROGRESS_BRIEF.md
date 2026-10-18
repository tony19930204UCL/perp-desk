# 進度感知的驗收執行器（C-0122 / C-0136 設計與驗收範圍）

## 2026-10-14 20:22 UTC 已部署盤點（非整項結案）

證據：`lab/evidence/operator-20261014T2020Z/installed-hashes-readback.json` 與 `lab/evidence/accept-progress-20261014T154830Z/deployment-20261014T175937Z/DEPLOYMENT-RESULT.json`。四個 installed helper hashes 均吻合已驗來源；39 installed fixtures 與原 source 673 項 full suite PASS（2 skips）屬既有證據，本次沒有重跑。同 PID/start identity 跨 840 秒 checkpoint 的 real suite 證據存在。TIMEOUT / STALL / CANCELLED 分類、JSONL lifecycle、單項耗時、stack/wait 診斷與 exact config 在同一 state-dir 的 completed-result reuse 已實作，不再標為全未實作。

尚未滿足／最小 bounded 補件需求（需求盤點，非派工或恢復排程授權）：

- **診斷待辦閉環缺口**：`accept_unit.py` 只轉抄 diagnostics；`auto_accept.py` 只啟動／看既有結果；兩者沒有以 commitment CLI 建立具體診斷待辦、可驗收 owner/holder、acceptance 與 evidence 的路徑。`ledger_wake.py` 只提醒原 watcher VERIFY，不等於建立診斷 owner。JSON diagnostics 不等於已派工待辦。最小修復範圍是現有 launcher／acceptance 邊界的 idempotent diagnostic registration，不重造 progress runner；驗收需要相同 head/suite/config 的 TIMEOUT/STALL 重讀不重複登記、明確 owner 或可檢查 blocker、失敗保留原證據且禁止 PASS/重跑/續派。
- **預設單項 budget 未落地**：CLI 支援 `--item-seconds` / `--stall-seconds`，但實際 launcher 沒傳，accept_unit 預設為 None。整套預設 2400 秒存在；不能宣稱 scheduler 已採預先校準的單項上限。需要以既有 lifecycle 耗時分布與合理長測試標示作可審查 budget profile，並由真正 launcher 傳遞。
- **去重範圍限制**：同一 state-dir 的 exact key reuse 已有實測；default state-dir 是 job-specific `.progress_runner`，沒有跨 job 完成結果索引。原 673-suite 為 verbose，scheduler nonverbose 是不同 config，不能冒稱同 key 閉環；不得為補證據重跑已完成同 head/suite/config。
- **真實 scheduler 候選閉環缺證**：既有 direct / builtin 試跑候選數為 0，屬真實 no-op。isolated fixture 非正式工程交付。缺少真實 REVIEW_REQUIRED 候選 → installed launcher → pinned 結果 → supervisor formal verdict／帳本獨立讀回的完整鏈，不可用 no-op 結案 C0122/C0136。
- **分類語義補充**：現有單項 budget 超限是 STALL，整套 hard deadline 超限是 TIMEOUT，diagnostics reason 可區分。基础設施／artifact 問題仍有 generic FAIL 路徑；是否另分類 INFRA_ERROR 屬規格確認，不把它冒称已重現產品 FAIL。

此盤點沒有產品工程碼、安裝、部署、排程或實盤變更；C0153 VERIFIED 僅覆蓋原正式 handback／部署證據，不等於 C0122/C0136 全需求完成。

14 分鐘是軟檢查點，不是停止線。40 分鐘為目前整套預設硬上限（可依實測重新登記，但不無限延長）。必須在子程序啟動時即採可監控的執行方式，不能先用 subprocess.run timeout=840 殺掉後再延長。

每項 test 記錄 ID、開始、完成、結果、耗時（monotonic）。完整 stdout/stderr 持續寫檔，避免buffer造成看似沒進度。快照記錄 pinned head、suite、最後完成項、目前項、進度時間、PID與程序啟動時間。

14分鐘檢查：
- 已退出：按真實exit與test結果分類。
- 持續有test完成：原程序續跑，以5分鐘一次checkpoint觀察，不重啟整套。
- 同一項耗時：對照同項歷史耗時與明示等待budget。CPU或I/O增量只能輔助，不是活性證明（busy loop亦可能高CPU）。
- 無法判定：標STALL_SUSPECTED，保存stack/目前test/等待原因；不假稱卡死，也不直接算程式FAIL。
- 已證明deadlock、超過單項預先定義上限、或整套40分鐘hard limit：有界終止所擁有process group、保存部分結果，分類STALL或TIMEOUT，排除同head同suite重複執行。

延長記錄原因、進度證據、舊deadline與新deadline。同一程序續跑，不能以新run清掉歷史時間。SKIP單獨列，pass以完整suite及逐ID結果核對，不能只看最後輸出。

工程測試至少覆蓋持續進展的慢suite、合理長單項、sleep/deadlock、busy loop、輸出buffer、無stdout但test lifecycle有變、子程序遺留、取消、重啟恢復與同head去重。fixture用短時鐘/短budget驗邏輯，不為測14分鐘真的等14分鐘。部署前用一套真實full lab確認checkpoint後延續同PID與正常收尾。
