# 進度感知的驗收執行器（C-0122 設計補充，未實作）

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
