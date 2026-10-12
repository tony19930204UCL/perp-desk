# NOW (generated 2026-10-12 10:55 UTC, do not edit)

## Needs a decision now (wake gate)
nothing

## Ledger: items not finished
C-0003 DISPATCHED [C-0003 已派工 cron:93f4f9882e43 驗收:manual 到期:11-01 10:18] | handle=alive | H2 與 F1H 四臂分小時錯誤率檢查
C-0004 DISPATCHED [C-0004 已派工 cron:652bfba428d0 驗收:manual 到期:11-10 10:19] | handle=alive | 承諾帳本外掛、監控排程與 SKILL 已建好並安裝
C-0026 DISPATCHED [C-0026 已派工 cron:9dc79f48761f 驗收:manual 到期:11-10 16:43] | handle=alive | 新狀況警報：每小時跑記分卡，只在出現新觸發（狀態檔停更、虧損過半、窗口到期、來回數達門檻）時通知
C-0027 DISPATCHED [C-0027 已派工 cron:fa25edbf3834 驗收:manual 到期:11-10 16:43] | handle=alive | 每日自我檢討：每晚 9 點讀記分卡與日誌，對照事前預測，跑一個新實驗並寫入日誌（結果只存本機）
C-0028 DISPATCHED [C-0028 已派工 cron:213f30fc8f7c 驗收:manual 到期:11-10 16:43] | handle=alive | 每週檢討：每週日晚 8 點對使用者回報預測對錯、教訓、下週實驗
C-0037 OPEN       [C-0037 未派工:等 item:C-0049] | handle=- | E2 轉接層狀態持久化的工程派工（E1 已知缺口：重啟後訂單狀態恢復），單一工程通道被 T1-U2B1 佔用
C-0041 DISPATCHED [C-0041 已派工 cron:6997c01c6d9e 驗收:manual 到期:10-26 09:32] | handle=alive | 工程監督迴圈：每 30 分鐘檢查帳本，有監看結束待驗收、阻擋解除待開工、延後到期的項目就自動叫醒我處理（沒有項目時不花算力）
C-0048 DISPATCHED [C-0048 已派工 cron:d08c2da3ae25 驗收:manual 到期:10-26 11:39] | handle=alive | 確定性自動驗收 cron：交付後 5 分鐘內不靠模型先跑 accept_unit.py，監督迴圈醒來時直接讀結果
C-0049 DISPATCHED [C-0049 已派工 pid:722930 驗收:manual 到期:10-12 15:46] | handle=alive | E2-U1持久化store（PR53）r0已確認送出，監看交付與獨立驗收（E2 adapter整合尚未完成）
C-0050 OPEN       [C-0050 未派工:等 item:C-0049] | handle=- | 看板v2 D2-U2 reader/server及D2-U3 UI工程派工（D2-U1模型已驗收，單一通道由PR53 E2-U1佔用）

## Roadmap: active and ready
PROBLEMS:
 - G2-e is planned and all its dependencies are done: start it
ACTIVE / READY:
 - G1-a H2、F1H、W1 共 8 臂紙盤運轉 | next: 到 11/1（W1 到 11/1 13:56 UTC）收窗口；每日記分卡與警報盯著
 - READY G2-e T1 四臂以 systemd 服務開窗（180 天） | next: U2B2 驗收後部署、先過 clock_check
 - READY G2-f 量測掛單成交率，判斷 1 分鐘暴衝能否用掛單做 | next: 寫量測腳本，對 4 幣記錄掛單 60 秒內成交比例與逆選擇
 - READY G2-g 空頭擁擠做多假說（資金費率極端 5%） | next: 等 W1 與 T1 前瞻資料，重評日 10-18
 - G3-b E2 轉接層狀態持久化（重啟後訂單狀態恢復） | next: PR53 E2-U1持久化store已確認送出，C-0049監看120步；後續adapter整合由C-0037等待同一通道。E2未完成、不部署。
 - G4-a 看板 v2（規格 lab/DASHBOARD_V2_SPEC.md） | next: PR52 D2-U1模型與分數天數修復已獨立驗收；reader/server/UI尚未派工，C-0050等PR53 C-0049通道。

## Project numbers (arm_scorecard.py)
SCORECARD 2026-10-12T10:55:26Z UTC
F1H-BTCUSDT-PAPER      第1.1天(剩19.9) 信號4 擋1 成交5 來回2 淨損益0.32 手續費0.21 最大回撤0.33 持倉1 輪詢錯誤9
F1H-SOLUSDT-PAPER      第1.1天(剩19.9) 信號1 擋0 成交1 來回0 淨損益-0.02 手續費0.04 最大回撤0.44 持倉1 輪詢錯誤3
F1H-XRPUSDT-PAPER      第1.1天(剩19.9) 信號0 擋0 成交0 來回0 淨損益0.00 手續費0.00 最大回撤0.00 持倉0 輪詢錯誤5
H2-PAPER-004           第1.1天(剩19.9) 信號2 擋0 成交3 來回1 淨損益0.38 手續費0.13 最大回撤0.89 持倉1 輪詢錯誤10
W1-BTCUSDT-PAPER       第0.87天(剩20.13) 信號7 擋4 成交5 來回2 淨損益0.59 手續費0.42 最大回撤0.66 持倉1 輪詢錯誤9
W1-ETHUSDT-PAPER       第0.87天(剩20.13) 信號5 擋3 成交3 來回1 淨損益0.72 手續費0.26 最大回撤1.74 持倉1 輪詢錯誤5
W1-SOLUSDT-PAPER       第0.87天(剩20.13) 信號5 擋2 成交6 來回3 淨損益-3.01 手續費0.46 最大回撤3.21 持倉0 輪詢錯誤10
W1-XRPUSDT-PAPER       第0.87天(剩20.13) 信號6 擋4 成交3 來回1 淨損益-0.44 手續費0.20 最大回撤1.77 持倉1 輪詢錯誤8
T1 影子（規則狀態，不是紙盤成績）：BTC flat | ETH flat | SOL flat | XRP flat
TRIGGERS: none

## Last 6 journal entries
J-0033 2026-10-12 [decision] C-0047獨立驗收與E2小單元備妥 | 
J-0034 2026-10-12 [mistake] 本輪驗收輔助命令工作目錄錯誤已修正 | 
J-0035 2026-10-12 [decision] 回答使用者：監督迴圈職責；學習加速但加實質內容閘門 | 不降頻；以機械閘門擋空話
J-0036 2026-10-12 [mistake] D2-U1驗收verdict錯把範圍外待辦列入本job | 
J-0037 2026-10-12 [decision] C-0047驗收接受D2-U1，E2接工程通道 | 
J-0038 2026-10-12 [decision] E2-U1 PR53已實際續派並由C-0049監看 |

## Engineering job env files
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/e1-env.txt
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/t1-env.txt
