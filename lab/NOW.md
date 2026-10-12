# NOW (generated 2026-10-12 15:44 UTC, do not edit)

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
C-0051 OPEN       [C-0051 未派工:我選擇延後（不是阻擋）重評:10-15 13:12] | handle=- | 監督迴圈回放測試集：用假帳本與假 job 目錄當固定情境，檢查它醒來後的動作對不對，換模型或改 prompt 前先跑，才有依據判斷便宜模型行
C-0052 OPEN       [C-0052 未派工:我選擇延後（不是阻擋）重評:10-14 13:12] | handle=- | 派工一條指令化 dispatch_next.py：預檢、送出、登記監看三步合成一個命令，預檢失敗就不會往下
C-0053 OPEN       [C-0053 未派工:我選擇延後（不是阻擋）重評:10-17 13:12] | handle=- | 監督迴圈事後稽核：每週比對監督迴圈的驗收結論與 auto_accept 結果，不一致就標出來
C-0057 DISPATCHED [C-0057 已派工 cron:93f4f9882e43 驗收:cmd 到期:10-14 14:27] | handle=alive | T1 四臂開窗驗證：四個服務 active、狀態檔產生、時鐘檢查通過、第一輪記分卡出現 T1 紙盤成績（需先重啟 WSL 載入新服務）
C-0058 OPEN       [C-0058 未派工:等 user:請你在 Windows PowerShell 執行 wsl --shutdown，再重新開啟 WSL（Tele] | handle=- | 重啟 WSL 載入 T1 四個新服務單元（我已寫好單元與連結，不能自己 daemon-reload，會弄斷 systemctl --user
C-0062 DISPATCHED [C-0062 已派工 cron:fa25edbf3834 驗收:manual 到期:10-22 16:30] | handle=alive | 入場邏輯研究（G2-h）：由每日負責人迴圈每天最多兩個實驗往下做，結果進研究日誌
C-0065 DISPATCHED [C-0065 已派工 pid:902746 驗收:manual 到期:10-12 20:32] | handle=finished | D2-U3B PR58固定靜態資產serving seam，高推理已確認送出，120步監看與獨立端到端驗收（不部署）

## Roadmap: active and ready
no problems
ACTIVE / READY:
 - G1-a H2、F1H、W1 共 8 臂紙盤運轉 | next: 到 11/1（W1 到 11/1 13:56 UTC）收窗口；每日記分卡與警報盯著
 - G2-e T1 四臂以 systemd 服務開窗（180 天） | next: 已部署檔案與單元（accepted head b1c5d92），等使用者 wsl --shutdown 載入服務；載入後四臂自動啟動，驗證項目會叫醒監督迴圈
 - READY G2-f 量測掛單成交率，判斷 1 分鐘暴衝能否用掛單做 | next: 寫量測腳本，對 4 幣記錄掛單 60 秒內成交比例與逆選擇
 - READY G2-g 空頭擁擠做多假說（資金費率極端 5%） | next: 等 W1 與 T1 前瞻資料，重評日 10-18
 - G4-a 看板 v2（規格 lab/DASHBOARD_V2_SPEC.md） | next: PR57 r1靜態UI已獨立驗收通過。PR58 D2-U3B固定資產serving seam已High三次確認並實送D2-U3B-r0，由C-0065監看與獨立端到端驗收。沒有部署，T1/行情/曲線與獨立健康來源仍未提供。
 - G2-h 入場邏輯研究：從 1 小時掃描的空結果往外找（4h 到日線、掛單成本、資金費率） | next: 每次迴圈挑一個：(a) 4h 與日線特徵掃描用非重疊樣本與區塊自助法 (b) 抓幣安資金費率歷史測極端值 (c) 配合 G2-f 掛單成交率重算成本門檻

## Project numbers (arm_scorecard.py)
SCORECARD 2026-10-12T15:44:13Z UTC
F1H-BTCUSDT-PAPER      第1.3天(剩19.7) 信號6 擋2 成交7 來回3 淨損益-0.55 手續費0.29 最大回撤1.30 持倉1 輪詢錯誤9
F1H-SOLUSDT-PAPER      第1.3天(剩19.7) 信號3 擋1 成交3 來回1 淨損益-0.96 手續費0.11 最大回撤1.23 持倉1 輪詢錯誤3
F1H-XRPUSDT-PAPER      第1.3天(剩19.7) 信號2 擋0 成交3 來回1 淨損益-0.91 手續費0.10 最大回撤1.25 持倉1 輪詢錯誤5
H2-PAPER-004           第1.3天(剩19.7) 信號5 擋2 成交5 來回2 淨損益-0.89 手續費0.22 最大回撤1.90 持倉1 輪詢錯誤10
W1-BTCUSDT-PAPER       第1.07天(剩19.93) 信號9 擋5 成交7 來回3 淨損益-1.02 手續費0.63 最大回撤2.78 持倉1 輪詢錯誤9
W1-ETHUSDT-PAPER       第1.07天(剩19.93) 信號9 擋6 成交5 來回2 淨損益-1.79 手續費0.44 最大回撤3.80 持倉1 輪詢錯誤5
W1-SOLUSDT-PAPER       第1.07天(剩19.93) 信號8 擋3 成交9 來回4 淨損益-4.80 手續費0.69 最大回撤5.25 持倉1 輪詢錯誤10
W1-XRPUSDT-PAPER       第1.07天(剩19.93) 信號9 擋6 成交5 來回2 淨損益-2.24 手續費0.33 最大回撤2.97 持倉1 輪詢錯誤8
T1 影子（規則狀態，不是紙盤成績）：BTC flat | ETH flat | SOL flat | XRP flat
TRIGGERS: none

## Last 6 journal entries
J-0082 2026-10-12 [experiment] C-0063 小幣領先落後結果重現與資料驗收 | 證據不足：不支持BTC急動後以分鐘級追小幣能覆蓋成本，因此不開這個交易臂。不是嚴格統計上不存在延遲效應，事件重疊、未區塊自助法及多重檢定，14bp是研究假設不是實際成交成本。
J-0083 2026-10-12 [mistake] 本回合研究日誌初次寫入被驗證器拒絕，已補記 | 
J-0084 2026-10-12 [mistake] C-0064探針初次使用錯誤工作目錄 | 
J-0085 2026-10-12 [mistake] C-0064驗收工具限制及淺clone診斷 | 
J-0086 2026-10-12 [decision] C-0064 PR57 r1静態UI獨立驗收通過，轉D2-U3B | 
J-0087 2026-10-12 [decision] D2-U3B PR58已實送，高推理120步監看C-0065承接 |

## Engineering job env files
none
