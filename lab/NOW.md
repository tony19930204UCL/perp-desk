# NOW (generated 2026-10-12 13:48 UTC, do not edit)

## Needs a decision now (wake gate)
nothing

## Ledger: items not finished
C-0003 DISPATCHED [C-0003 已派工 cron:93f4f9882e43 驗收:manual 到期:11-01 10:18] | handle=alive | H2 與 F1H 四臂分小時錯誤率檢查
C-0004 DISPATCHED [C-0004 已派工 cron:652bfba428d0 驗收:manual 到期:11-10 10:19] | handle=alive | 承諾帳本外掛、監控排程與 SKILL 已建好並安裝
C-0026 DISPATCHED [C-0026 已派工 cron:9dc79f48761f 驗收:manual 到期:11-10 16:43] | handle=alive | 新狀況警報：每小時跑記分卡，只在出現新觸發（狀態檔停更、虧損過半、窗口到期、來回數達門檻）時通知
C-0027 DISPATCHED [C-0027 已派工 cron:fa25edbf3834 驗收:manual 到期:11-10 16:43] | handle=alive | 每日自我檢討：每晚 9 點讀記分卡與日誌，對照事前預測，跑一個新實驗並寫入日誌（結果只存本機）
C-0028 DISPATCHED [C-0028 已派工 cron:213f30fc8f7c 驗收:manual 到期:11-10 16:43] | handle=alive | 每週檢討：每週日晚 8 點對使用者回報預測對錯、教訓、下週實驗
C-0041 DISPATCHED [C-0041 已派工 cron:6997c01c6d9e 驗收:manual 到期:10-26 09:32] | handle=alive | 工程監督迴圈：每 30 分鐘檢查帳本，有監看結束待驗收、阻擋解除待開工、延後到期的項目就自動叫醒我處理（沒有項目時不花算力）
C-0048 DISPATCHED [C-0048 已派工 cron:d08c2da3ae25 驗收:manual 到期:10-26 11:39] | handle=alive | 確定性自動驗收 cron：交付後 5 分鐘內不靠模型先跑 accept_unit.py，監督迴圈醒來時直接讀結果
C-0050 OPEN       [C-0050 未派工:等 item:C-0060] | handle=- | 看板v2剩餘D2-U3 UI（D2-U2B HTTP server PR56由C-0060佔用單一通道）
C-0051 OPEN       [C-0051 未派工:我選擇延後（不是阻擋）重評:10-15 13:12] | handle=- | 監督迴圈回放測試集：用假帳本與假 job 目錄當固定情境，檢查它醒來後的動作對不對，換模型或改 prompt 前先跑，才有依據判斷便宜模型行
C-0052 OPEN       [C-0052 未派工:我選擇延後（不是阻擋）重評:10-14 13:12] | handle=- | 派工一條指令化 dispatch_next.py：預檢、送出、登記監看三步合成一個命令，預檢失敗就不會往下
C-0053 OPEN       [C-0053 未派工:我選擇延後（不是阻擋）重評:10-17 13:12] | handle=- | 監督迴圈事後稽核：每週比對監督迴圈的驗收結論與 auto_accept 結果，不一致就標出來
C-0057 DISPATCHED [C-0057 已派工 cron:93f4f9882e43 驗收:cmd 到期:10-14 14:27] | handle=alive | T1 四臂開窗驗證：四個服務 active、狀態檔產生、時鐘檢查通過、第一輪記分卡出現 T1 紙盤成績（需先重啟 WSL 載入新服務）
C-0058 OPEN       [C-0058 未派工:等 user:請你在 Windows PowerShell 執行 wsl --shutdown，再重新開啟 WSL（Tele] | handle=- | 重啟 WSL 載入 T1 四個新服務單元（我已寫好單元與連結，不能自己 daemon-reload，會弄斷 systemctl --user
C-0060 DISPATCHED [C-0060 已派工 pid:826810 驗收:manual 到期:10-12 18:36] | handle=finished | D2-U2B PR56 loopback HTTP JSON server High已確認送出，120步監看與獨立驗收（不部署）

## Roadmap: active and ready
no problems
ACTIVE / READY:
 - G1-a H2、F1H、W1 共 8 臂紙盤運轉 | next: 到 11/1（W1 到 11/1 13:56 UTC）收窗口；每日記分卡與警報盯著
 - G2-e T1 四臂以 systemd 服務開窗（180 天） | next: 已部署檔案與單元（accepted head b1c5d92），等使用者 wsl --shutdown 載入服務；載入後四臂自動啟動，驗證項目會叫醒監督迴圈
 - READY G2-f 量測掛單成交率，判斷 1 分鐘暴衝能否用掛單做 | next: 寫量測腳本，對 4 幣記錄掛單 60 秒內成交比例與逆選擇
 - READY G2-g 空頭擁擠做多假說（資金費率極端 5%） | next: 等 W1 與 T1 前瞻資料，重評日 10-18
 - G4-a 看板 v2（規格 lab/DASHBOARD_V2_SPEC.md） | next: D2-U1與D2-U2A PR55已驗收。D2-U2B HTTP JSON server PR56 High確認送出，C-0060監看120步。D2-U3 UI由C-0050等C-0060，未派工、不部署。

## Project numbers (arm_scorecard.py)
SCORECARD 2026-10-12T13:48:18Z UTC
F1H-BTCUSDT-PAPER      第1.22天(剩19.78) 信號5 擋1 成交7 來回3 淨損益-0.54 手續費0.29 最大回撤1.23 持倉1 輪詢錯誤9
F1H-SOLUSDT-PAPER      第1.22天(剩19.78) 信號2 擋1 成交2 來回1 淨損益-1.09 手續費0.08 最大回撤1.18 持倉0 輪詢錯誤3
F1H-XRPUSDT-PAPER      第1.22天(剩19.78) 信號1 擋0 成交1 來回0 淨損益-0.17 手續費0.03 最大回撤0.45 持倉1 輪詢錯誤5
H2-PAPER-004           第1.22天(剩19.78) 信號4 擋1 成交5 來回2 淨損益-0.78 手續費0.22 最大回撤1.69 持倉1 輪詢錯誤10
W1-BTCUSDT-PAPER       第0.99天(剩20.01) 信號8 擋4 成交7 來回3 淨損益-0.99 手續費0.63 最大回撤2.60 持倉1 輪詢錯誤9
W1-ETHUSDT-PAPER       第0.99天(剩20.01) 信號8 擋5 成交5 來回2 淨損益-1.55 手續費0.44 最大回撤3.42 持倉1 輪詢錯誤5
W1-SOLUSDT-PAPER       第0.99天(剩20.01) 信號7 擋3 成交7 來回3 淨損益-4.19 手續費0.54 最大回撤4.92 持倉1 輪詢錯誤10
W1-XRPUSDT-PAPER       第0.99天(剩20.01) 信號8 擋6 成交4 來回2 淨損益-2.49 手續費0.26 最大回撤2.88 持倉0 輪詢錯誤8
T1 影子（規則狀態，不是紙盤成績）：BTC flat | ETH flat | SOL flat | XRP flat
TRIGGERS: none

## Last 6 journal entries
J-0056 2026-10-12 [decision] C-0050解除PR54阻擋後直接續派D2-U2A reader PR55 | 
J-0057 2026-10-12 [observation] 本輪main由獨立Auto Sync推進，非工程relay改寫 | 
J-0058 2026-10-12 [mistake] 本輪execute_code被無人值守權限阻擋，已改正常工具 | 
J-0059 2026-10-12 [decision] C-0059 PR55 reader獨立驗收通過 | 
J-0060 2026-10-12 [decision] C-0050釋放reader阻擋後直接續派D2-U2B PR56 | 
J-0061 2026-10-12 [decision] C-0050 UI依賴改指到C-0060，其餘延後與WSL阻擋不變 |

## Engineering job env files
none
