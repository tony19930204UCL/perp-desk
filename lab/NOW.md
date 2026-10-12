# NOW (generated 2026-10-12 12:44 UTC, do not edit)

## Needs a decision now (wake gate)
VERIFY C-0056 handle=finished E2-U2 PR54 r1修正過期fixture已確認送出，監看交付並獨立驗收（不部署） auto_accept=missing
round 165908

## Ledger: items not finished
C-0003 DISPATCHED [C-0003 已派工 cron:93f4f9882e43 驗收:manual 到期:11-01 10:18] | handle=alive | H2 與 F1H 四臂分小時錯誤率檢查
C-0004 DISPATCHED [C-0004 已派工 cron:652bfba428d0 驗收:manual 到期:11-10 10:19] | handle=alive | 承諾帳本外掛、監控排程與 SKILL 已建好並安裝
C-0026 DISPATCHED [C-0026 已派工 cron:9dc79f48761f 驗收:manual 到期:11-10 16:43] | handle=alive | 新狀況警報：每小時跑記分卡，只在出現新觸發（狀態檔停更、虧損過半、窗口到期、來回數達門檻）時通知
C-0027 DISPATCHED [C-0027 已派工 cron:fa25edbf3834 驗收:manual 到期:11-10 16:43] | handle=alive | 每日自我檢討：每晚 9 點讀記分卡與日誌，對照事前預測，跑一個新實驗並寫入日誌（結果只存本機）
C-0028 DISPATCHED [C-0028 已派工 cron:213f30fc8f7c 驗收:manual 到期:11-10 16:43] | handle=alive | 每週檢討：每週日晚 8 點對使用者回報預測對錯、教訓、下週實驗
C-0041 DISPATCHED [C-0041 已派工 cron:6997c01c6d9e 驗收:manual 到期:10-26 09:32] | handle=alive | 工程監督迴圈：每 30 分鐘檢查帳本，有監看結束待驗收、阻擋解除待開工、延後到期的項目就自動叫醒我處理（沒有項目時不花算力）
C-0048 DISPATCHED [C-0048 已派工 cron:d08c2da3ae25 驗收:manual 到期:10-26 11:39] | handle=alive | 確定性自動驗收 cron：交付後 5 分鐘內不靠模型先跑 accept_unit.py，監督迴圈醒來時直接讀結果
C-0050 OPEN       [C-0050 未派工:等 item:C-0056] | handle=- | 看板v2 D2-U2 reader/server及D2-U3 UI工程派工（D2-U1已驗收，單一通道由PR54 E2-U2 r1 C-00
C-0051 OPEN       [C-0051 未派工:我選擇延後（不是阻擋）重評:10-15 13:12] | handle=- | 監督迴圈回放測試集：用假帳本與假 job 目錄當固定情境，檢查它醒來後的動作對不對，換模型或改 prompt 前先跑，才有依據判斷便宜模型行
C-0052 OPEN       [C-0052 未派工:我選擇延後（不是阻擋）重評:10-14 13:12] | handle=- | 派工一條指令化 dispatch_next.py：預檢、送出、登記監看三步合成一個命令，預檢失敗就不會往下
C-0053 OPEN       [C-0053 未派工:我選擇延後（不是阻擋）重評:10-17 13:12] | handle=- | 監督迴圈事後稽核：每週比對監督迴圈的驗收結論與 auto_accept 結果，不一致就標出來
C-0056 DISPATCHED [C-0056 已派工 pid:774773 驗收:manual 到期:10-12 17:10] | handle=finished | E2-U2 PR54 r1修正過期fixture已確認送出，監看交付並獨立驗收（不部署）
C-0057 DISPATCHED [C-0057 已派工 cron:93f4f9882e43 驗收:cmd 到期:10-14 14:27] | handle=alive | T1 四臂開窗驗證：四個服務 active、狀態檔產生、時鐘檢查通過、第一輪記分卡出現 T1 紙盤成績（需先重啟 WSL 載入新服務）
C-0058 OPEN       [C-0058 未派工:等 user:請你在 Windows PowerShell 執行 wsl --shutdown，再重新開啟 WSL（Tele] | handle=- | 重啟 WSL 載入 T1 四個新服務單元（我已寫好單元與連結，不能自己 daemon-reload，會弄斷 systemctl --user

## Roadmap: active and ready
no problems
ACTIVE / READY:
 - G1-a H2、F1H、W1 共 8 臂紙盤運轉 | next: 到 11/1（W1 到 11/1 13:56 UTC）收窗口；每日記分卡與警報盯著
 - G2-e T1 四臂以 systemd 服務開窗（180 天） | next: 已部署檔案與單元（accepted head b1c5d92），等使用者 wsl --shutdown 載入服務；載入後四臂自動啟動，驗證項目會叫醒監督迴圈
 - READY G2-f 量測掛單成交率，判斷 1 分鐘暴衝能否用掛單做 | next: 寫量測腳本，對 4 幣記錄掛單 60 秒內成交比例與逆選擇
 - READY G2-g 空頭擁擠做多假說（資金費率極端 5%） | next: 等 W1 與 T1 前瞻資料，重評日 10-18
 - G3-b E2 轉接層狀態持久化（重啟後訂單狀態恢復） | next: E2-U1 PR53已驗收。E2-U2 PR54 r0因測試fixture過期ts拒收，r1 High確認送出，C-0056監看120步。adapter未驗收、不部署。
 - G4-a 看板 v2（規格 lab/DASHBOARD_V2_SPEC.md） | next: D2-U1已驗收，reader/server與UI尚未派工，C-0050單一工程通道等C-0056（PR54 E2-U2 r1）。

## Project numbers (arm_scorecard.py)
SCORECARD 2026-10-12T12:44:18Z UTC
F1H-BTCUSDT-PAPER      第1.18天(剩19.82) 信號4 擋1 成交6 來回3 淨損益-0.49 手續費0.25 最大回撤0.92 持倉0 輪詢錯誤9
F1H-SOLUSDT-PAPER      第1.18天(剩19.82) 信號1 擋0 成交2 來回1 淨損益-1.09 手續費0.08 最大回撤1.18 持倉0 輪詢錯誤3
F1H-XRPUSDT-PAPER      第1.18天(剩19.82) 信號0 擋0 成交0 來回0 淨損益0.00 手續費0.00 最大回撤0.00 持倉0 輪詢錯誤5
H2-PAPER-004           第1.18天(剩19.82) 信號3 擋1 成交4 來回2 淨損益-0.85 手續費0.18 最大回撤1.36 持倉0 輪詢錯誤10
W1-BTCUSDT-PAPER       第0.95天(剩20.05) 信號7 擋4 成交6 來回3 淨損益-0.82 手續費0.50 最大回撤1.62 持倉0 輪詢錯誤9
W1-ETHUSDT-PAPER       第0.95天(剩20.05) 信號7 擋5 成交4 來回2 淨損益-1.69 手續費0.35 最大回撤2.68 持倉0 輪詢錯誤5
W1-SOLUSDT-PAPER       第0.95天(剩20.05) 信號6 擋2 成交7 來回3 淨損益-4.11 手續費0.54 最大回撤4.55 持倉1 輪詢錯誤10
W1-XRPUSDT-PAPER       第0.95天(剩20.05) 信號7 擋5 成交3 來回1 淨損益-1.88 手續費0.20 最大回撤2.54 持倉1 輪詢錯誤8
T1 影子（規則狀態，不是紙盤成績）：BTC flat | ETH flat | SOL flat | XRP flat
TRIGGERS: none

## Last 6 journal entries
J-0048 2026-10-12 [decision] 同步帳本與路線圖，C-0050改等C-0055 | 
J-0049 2026-10-12 [decision] C-0055 PR54 r0拒收：保護出場測試使用過期決策時間 | 
J-0050 2026-10-12 [decision] PR54 E2-U2 r1已續派，C-0056接管單一工程通道 | 
J-0051 2026-10-12 [decision] 部署 T1 四臂檔案與服務單元（未啟動），等 WSL 重啟 | G2-e 在 U2B2 驗收後閒置約 2 小時無人持有，是路線圖 lint 抓到的缺口
J-0052 2026-10-12 [mistake] T1 部署在驗收後閒置約 3 小時無人持有 | 已修：喚醒閘門加入路線圖 lint 問題（輸出 START roadmap: …），新增測試，共 10 項通過。屬 autonomous-project-owner 的設計缺口，非
J-0053 2026-10-12 [decision] ledger 與 owner 兩個 skill 維持分開，安裝合成一條指令 | 分開比較好；使用者要的是只交付一個，由安裝腳本達成

## Engineering job env files
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/t1-env.txt
