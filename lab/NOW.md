# NOW (generated 2026-10-12 11:54 UTC, do not edit)

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
C-0050 OPEN       [C-0050 未派工:等 item:C-0055] | handle=- | 看板v2 D2-U2 reader/server及D2-U3 UI工程派工（D2-U1已驗收，單一通道由PR54 E2-U2 C-0055佔
C-0051 OPEN       [C-0051 未派工:我選擇延後（不是阻擋）重評:10-15 13:12] | handle=- | 監督迴圈回放測試集：用假帳本與假 job 目錄當固定情境，檢查它醒來後的動作對不對，換模型或改 prompt 前先跑，才有依據判斷便宜模型行
C-0052 OPEN       [C-0052 未派工:我選擇延後（不是阻擋）重評:10-14 13:12] | handle=- | 派工一條指令化 dispatch_next.py：預檢、送出、登記監看三步合成一個命令，預檢失敗就不會往下
C-0053 OPEN       [C-0053 未派工:我選擇延後（不是阻擋）重評:10-17 13:12] | handle=- | 監督迴圈事後稽核：每週比對監督迴圈的驗收結論與 auto_accept 結果，不一致就標出來
C-0055 DISPATCHED [C-0055 已派工 pid:757870 驗收:manual 到期:10-12 16:42] | handle=finished | E2-U2 PR54持久化adapter重啟恢復已確認送出，監看交付並獨立驗收（不部署）

## Roadmap: active and ready
PROBLEMS:
 - G2-e is planned and all its dependencies are done: start it
ACTIVE / READY:
 - G1-a H2、F1H、W1 共 8 臂紙盤運轉 | next: 到 11/1（W1 到 11/1 13:56 UTC）收窗口；每日記分卡與警報盯著
 - READY G2-e T1 四臂以 systemd 服務開窗（180 天） | next: U2B2 驗收後部署、先過 clock_check
 - READY G2-f 量測掛單成交率，判斷 1 分鐘暴衝能否用掛單做 | next: 寫量測腳本，對 4 幣記錄掛單 60 秒內成交比例與逆選擇
 - READY G2-g 空頭擁擠做多假說（資金費率極端 5%） | next: 等 W1 與 T1 前瞻資料，重評日 10-18
 - G3-b E2 轉接層狀態持久化（重啟後訂單狀態恢復） | next: E2-U1 PR53已獨立驗收；E2-U2 PR54 r0 High確認送出，C-0055監看120步。adapter未驗收，不部署。
 - G4-a 看板 v2（規格 lab/DASHBOARD_V2_SPEC.md） | next: D2-U1已驗收，reader/server與UI尚未派工，C-0050等PR54 E2-U2 C-0055通道。

## Project numbers (arm_scorecard.py)
SCORECARD 2026-10-12T11:54:14Z UTC
F1H-BTCUSDT-PAPER      第1.14天(剩19.86) 信號4 擋1 成交5 來回2 淨損益0.20 手續費0.21 最大回撤0.35 持倉1 輪詢錯誤9
F1H-SOLUSDT-PAPER      第1.14天(剩19.86) 信號1 擋0 成交1 來回0 淨損益-0.56 手續費0.04 最大回撤0.88 持倉1 輪詢錯誤3
F1H-XRPUSDT-PAPER      第1.14天(剩19.86) 信號0 擋0 成交0 來回0 淨損益0.00 手續費0.00 最大回撤0.00 持倉0 輪詢錯誤5
H2-PAPER-004           第1.14天(剩19.86) 信號2 擋0 成交3 來回1 淨損益0.03 手續費0.13 最大回撤0.89 持倉1 輪詢錯誤10
W1-BTCUSDT-PAPER       第0.91天(剩20.09) 信號7 擋4 成交5 來回2 淨損益0.35 手續費0.42 最大回撤0.70 持倉1 輪詢錯誤9
W1-ETHUSDT-PAPER       第0.91天(剩20.09) 信號6 擋4 成交3 來回1 淨損益0.03 手續費0.26 最大回撤1.74 持倉1 輪詢錯誤5
W1-SOLUSDT-PAPER       第0.91天(剩20.09) 信號5 擋2 成交6 來回3 淨損益-3.01 手續費0.46 最大回撤3.21 持倉0 輪詢錯誤10
W1-XRPUSDT-PAPER       第0.91天(剩20.09) 信號6 擋4 成交3 來回1 淨損益-1.07 手續費0.20 最大回撤1.85 持倉1 輪詢錯誤8
T1 影子（規則狀態，不是紙盤成績）：BTC flat | ETH flat | SOL flat | XRP flat
TRIGGERS: none

## Last 6 journal entries
J-0043 2026-10-12 [decision] E2-U1 r1已續派，C-0054持有單一工程通道 | 
J-0044 2026-10-12 [observation] 技能被背景程序修改：來源查明並修正兩處分歧 | 已同步副本並更正說明；紀錄在 skills/.curator_ledger.jsonl
J-0045 2026-10-12 [mistake] 監督迴圈 execute_code 被 cron 核准政策拒絕 | 
J-0046 2026-10-12 [decision] C-0054 E2-U1 r1獨立驗收通過，僅限持久化 store | 
J-0047 2026-10-12 [decision] C-0037已續派E2-U2 PR54，C-0055持有單一工程通道 | 
J-0048 2026-10-12 [decision] 同步帳本與路線圖，C-0050改等C-0055 |

## Engineering job env files
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/e1-env.txt
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/t1-env.txt
