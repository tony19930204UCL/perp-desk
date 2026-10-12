# NOW (generated 2026-10-12 10:08 UTC, do not edit)

## Needs a decision now (wake gate)
VERIFY C-0046 handle=finished 看板v2 D2-U1資料模型（PR52）已確認送出，監看交付與獨立驗收
round 165907

## Ledger: items not finished
C-0003 DISPATCHED [C-0003 已派工 cron:93f4f9882e43 驗收:manual 到期:11-01 10:18] | handle=alive | H2 與 F1H 四臂分小時錯誤率檢查
C-0004 DISPATCHED [C-0004 已派工 cron:652bfba428d0 驗收:manual 到期:11-10 10:19] | handle=alive | 承諾帳本外掛、監控排程與 SKILL 已建好並安裝
C-0026 DISPATCHED [C-0026 已派工 cron:9dc79f48761f 驗收:manual 到期:11-10 16:43] | handle=alive | 新狀況警報：每小時跑記分卡，只在出現新觸發（狀態檔停更、虧損過半、窗口到期、來回數達門檻）時通知
C-0027 DISPATCHED [C-0027 已派工 cron:fa25edbf3834 驗收:manual 到期:11-10 16:43] | handle=alive | 每日自我檢討：每晚 9 點讀記分卡與日誌，對照事前預測，跑一個新實驗並寫入日誌（結果只存本機）
C-0028 DISPATCHED [C-0028 已派工 cron:213f30fc8f7c 驗收:manual 到期:11-10 16:43] | handle=alive | 每週檢討：每週日晚 8 點對使用者回報預測對錯、教訓、下週實驗
C-0037 OPEN       [C-0037 未派工:等 item:C-0046] | handle=- | E2 轉接層狀態持久化的工程派工（E1 已知缺口：重啟後訂單狀態恢復），單一工程通道被 T1-U2B1 佔用
C-0041 DISPATCHED [C-0041 已派工 cron:6997c01c6d9e 驗收:manual 到期:10-26 09:32] | handle=alive | 工程監督迴圈：每 30 分鐘檢查帳本，有監看結束待驗收、阻擋解除待開工、延後到期的項目就自動叫醒我處理（沒有項目時不花算力）
C-0043 OPEN       [C-0043 未派工:我選擇延後（不是阻擋）重評:10-14 10:44] | handle=- | 驗收腳本化 accept_unit.py：乾淨複製、跑 CI 同指令、白名單範圍檢查、位元組比對一次做完，不再每次現寫
C-0044 OPEN       [C-0044 未派工:我選擇延後（不是阻擋）重評:10-14 10:44] | handle=- | 派工前檢查腳本 dispatch_preflight.py：聊天推理等級是 High、任務授權時效、送出記錄狀態、單元大小警告
C-0046 DISPATCHED [C-0046 已派工 pid:672790 驗收:manual 到期:10-12 14:34] | handle=finished | 看板v2 D2-U1資料模型（PR52）已確認送出，監看交付與獨立驗收

## Roadmap: active and ready
PROBLEMS:
 - G2-e is planned and all its dependencies are done: start it
 - G5-d is planned and all its dependencies are done: start it
 - G5-e is planned and all its dependencies are done: start it
ACTIVE / READY:
 - G1-a H2、F1H、W1 共 8 臂紙盤運轉 | next: 到 11/1（W1 到 11/1 13:56 UTC）收窗口；每日記分卡與警報盯著
 - READY G2-e T1 四臂以 systemd 服務開窗（180 天） | next: U2B2 驗收後部署、先過 clock_check
 - READY G2-f 量測掛單成交率，判斷 1 分鐘暴衝能否用掛單做 | next: 寫量測腳本，對 4 幣記錄掛單 60 秒內成交比例與逆選擇
 - READY G2-g 空頭擁擠做多假說（資金費率極端 5%） | next: 等 W1 與 T1 前瞻資料，重評日 10-18
 - G4-a 看板 v2（規格 lab/DASHBOARD_V2_SPEC.md） | next: PR52 D2-U1純資料模型已派且C-0046監看中，交付後獨立驗收；reader/server與UI未完成
 - READY G5-d 驗收腳本化 accept_unit.py | next: 看監督迴圈實戰後決定涵蓋範圍
 - READY G5-e 派工前檢查 dispatch_preflight.py | next: 同上

## Paper arms (latest scorecard)
SCORECARD 2026-10-12T10:08:13Z UTC
F1H-BTCUSDT-PAPER      第1.07天(剩19.93) 信號4 擋1 成交5 來回2 淨損益0.28 手續費0.21 最大回撤0.33 持倉1 輪詢錯誤9
F1H-SOLUSDT-PAPER      第1.07天(剩19.93) 信號1 擋0 成交1 來回0 淨損益-0.12 手續費0.04 最大回撤0.44 持倉1 輪詢錯誤3
F1H-XRPUSDT-PAPER      第1.07天(剩19.93) 信號0 擋0 成交0 來回0 淨損益0.00 手續費0.00 最大回撤0.00 持倉0 輪詢錯誤5
H2-PAPER-004           第1.07天(剩19.93) 信號2 擋0 成交3 來回1 淨損益0.42 手續費0.13 最大回撤0.89 持倉1 輪詢錯誤10
W1-BTCUSDT-PAPER       第0.84天(剩20.16) 信號7 擋4 成交5 來回2 淨損益0.51 手續費0.42 最大回撤0.66 持倉1 輪詢錯誤9
W1-ETHUSDT-PAPER       第0.84天(剩20.16) 信號5 擋3 成交3 來回1 淨損益0.77 手續費0.26 最大回撤1.74 持倉1 輪詢錯誤5
W1-SOLUSDT-PAPER       第0.84天(剩20.16) 信號5 擋2 成交6 來回3 淨損益-3.01 手續費0.46 最大回撤3.21 持倉0 輪詢錯誤10
W1-XRPUSDT-PAPER       第0.84天(剩20.16) 信號6 擋4 成交3 來回1 淨損益-0.53 手續費0.20 最大回撤1.77 持倉1 輪詢錯誤8
T1 影子（規則狀態，不是紙盤成績）：BTC flat | ETH flat | SOL flat | XRP flat
TRIGGERS: none

## Last 6 journal entries
J-0021 2026-10-12 [mistake] r1 交付後 10 小時沒人驗收，CI 失敗無人知 | 把『帳本有待驗收或可開工項目』接成會叫醒我的排程（工程監督迴圈）。授權時效改成續期須明確記錄
J-0022 2026-10-12 [decision] 監督迴圈上線與驗證設計 | 第一次實戰驗證：C-0040（r2 於 10:33 交付）須在無人叫喚下被結案，帳本項目 C-0042 以指令自動檢查，12:09 到期，未過會被每小時檢查程式警報
J-0023 2026-10-12 [mistake] C-0040驗收前景工具420秒上限中止 | 
J-0024 2026-10-12 [decision] 監督迴圈首輪實戰通過，T1-U2B2 r2工程驗收完成 | 
J-0025 2026-10-12 [decision] C-0033看板v2第一單元已派，C-0037改等C-0046 | 
J-0026 2026-10-12 [mistake] 派工前斷言失敗後shell仍續派 |

## Engineering job env files
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/e1-env.txt
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/t1-env.txt
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/w1-env.txt
