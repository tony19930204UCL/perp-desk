# NOW (generated 2026-10-12 09:03 UTC, do not edit)

## Needs a decision now (wake gate)
VERIFY C-0040 handle=finished T1-U2B2 修復 r2（只改一個測試的假市場資料）已確認送出，監看程式等待交付；交付後驗收（乾淨複製、CI、快照三情境）
round 165907

## Ledger: items not finished
C-0003 DISPATCHED [C-0003 已派工 cron:93f4f9882e43 驗收:manual 到期:11-01 10:18] | handle=alive | H2 與 F1H 四臂分小時錯誤率檢查
C-0004 DISPATCHED [C-0004 已派工 cron:652bfba428d0 驗收:manual 到期:11-10 10:19] | handle=alive | 承諾帳本外掛、監控排程與 SKILL 已建好並安裝
C-0026 DISPATCHED [C-0026 已派工 cron:9dc79f48761f 驗收:manual 到期:11-10 16:43] | handle=alive | 新狀況警報：每小時跑記分卡，只在出現新觸發（狀態檔停更、虧損過半、窗口到期、來回數達門檻）時通知
C-0027 DISPATCHED [C-0027 已派工 cron:fa25edbf3834 驗收:manual 到期:11-10 16:43] | handle=alive | 每日自我檢討：每晚 9 點讀記分卡與日誌，對照事前預測，跑一個新實驗並寫入日誌（結果只存本機）
C-0028 DISPATCHED [C-0028 已派工 cron:213f30fc8f7c 驗收:manual 到期:11-10 16:43] | handle=alive | 每週檢討：每週日晚 8 點對使用者回報預測對錯、教訓、下週實驗
C-0033 OPEN       [C-0033 未派工:等 item:C-0040] | handle=- | 看板v2新分支與PR工程派工，承接C-0031，規格lab/DASHBOARD_V2_SPEC.md
C-0037 OPEN       [C-0037 未派工:等 item:C-0040] | handle=- | E2 轉接層狀態持久化的工程派工（E1 已知缺口：重啟後訂單狀態恢復），單一工程通道被 T1-U2B1 佔用
C-0040 DISPATCHED [C-0040 已派工 pid:632139 驗收:manual 到期:10-12 13:31] | handle=finished | T1-U2B2 修復 r2（只改一個測試的假市場資料）已確認送出，監看程式等待交付；交付後驗收（乾淨複製、CI、快照三情境）
C-0041 DISPATCHED [C-0041 已派工 cron:6997c01c6d9e 驗收:manual 到期:10-26 09:32] | handle=alive | 工程監督迴圈：每 30 分鐘檢查帳本，有監看結束待驗收、阻擋解除待開工、延後到期的項目就自動叫醒我處理（沒有項目時不花算力）
C-0042 DISPATCHED [C-0042 已派工 cron:6997c01c6d9e 驗收:cmd 到期:10-12 12:09] | handle=alive | 驗證監督迴圈第一次實戰：C-0040（r2 已交付）要在沒有人叫它的情況下被驗收結案（done 或 drop）；做不到就是迴圈失敗
C-0043 OPEN       [C-0043 未派工:我選擇延後（不是阻擋）重評:10-14 10:44] | handle=- | 驗收腳本化 accept_unit.py：乾淨複製、跑 CI 同指令、白名單範圍檢查、位元組比對一次做完，不再每次現寫
C-0044 OPEN       [C-0044 未派工:我選擇延後（不是阻擋）重評:10-14 10:44] | handle=- | 派工前檢查腳本 dispatch_preflight.py：聊天推理等級是 High、任務授權時效、送出記錄狀態、單元大小警告

## Roadmap: active and ready
no problems
ACTIVE / READY:
 - G1-a H2、F1H、W1 共 8 臂紙盤運轉 | next: 到 11/1（W1 到 11/1 13:56 UTC）收窗口；每日記分卡與警報盯著
 - G2-d T1 下單執行 U2B2 | next: r2（一個測試夾具）等交付；驗收後部署 T1 四臂
 - READY G2-f 量測掛單成交率，判斷 1 分鐘暴衝能否用掛單做 | next: 寫量測腳本，對 4 幣記錄掛單 60 秒內成交比例與逆選擇
 - READY G2-g 空頭擁擠做多假說（資金費率極端 5%） | next: 等 W1 與 T1 前瞻資料，重評日 10-18
 - G5-c 工程監督迴圈：帳本有已結束的監看項目就自動醒來驗收與派下一單 | next: 已建並啟用，尚未證明：要看它是否在下一次監看結束時自己醒來驗收並派工（證據看 journal 與 cron 輸出）

## Paper arms (latest scorecard)
SCORECARD 2026-10-12T09:03:14Z UTC
F1H-BTCUSDT-PAPER      第1.03天(剩19.97) 信號4 擋1 成交5 來回2 淨損益0.40 手續費0.21 最大回撤0.23 持倉1 輪詢錯誤9
F1H-SOLUSDT-PAPER      第1.03天(剩19.97) 信號1 擋0 成交1 來回0 淨損益0.06 手續費0.04 最大回撤0.05 持倉1 輪詢錯誤3
F1H-XRPUSDT-PAPER      第1.03天(剩19.97) 信號0 擋0 成交0 來回0 淨損益0.00 手續費0.00 最大回撤0.00 持倉0 輪詢錯誤5
H2-PAPER-004           第1.03天(剩19.97) 信號2 擋0 成交3 來回1 淨損益0.34 手續費0.13 最大回撤0.89 持倉1 輪詢錯誤10
W1-BTCUSDT-PAPER       第0.8天(剩20.2) 信號7 擋4 成交5 來回2 淨損益0.75 手續費0.42 最大回撤0.36 持倉1 輪詢錯誤9
W1-ETHUSDT-PAPER       第0.8天(剩20.2) 信號5 擋3 成交3 來回1 淨損益0.66 手續費0.26 最大回撤1.74 持倉1 輪詢錯誤5
W1-SOLUSDT-PAPER       第0.8天(剩20.2) 信號5 擋2 成交5 來回2 淨損益-2.52 手續費0.39 最大回撤2.96 持倉1 輪詢錯誤10
W1-XRPUSDT-PAPER       第0.8天(剩20.2) 信號6 擋4 成交3 來回1 淨損益-0.15 手續費0.20 最大回撤1.77 持倉1 輪詢錯誤8
T1 影子（規則狀態，不是紙盤成績）：BTC flat | ETH flat | SOL flat | XRP flat
TRIGGERS: none

## Last 6 journal entries
J-0017 2026-10-11 [observation] 每日自我檢討2026-10-11：開始成交，仍零完整來回，八臂未失效 | 八臂均未到期。H2/F1H期限11/1 08:26:51 UTC，W1期限11/1 13:56:53 UTC。各臂error/(成功decision after candle wo
J-0018 2026-10-11 [mistake] 監看程式把『工程端已放棄』當成『還在做』空轉近兩小時 | 單元太大（兩次整包皆失敗）。拆成 U2B1 骨架與 U2B2 執行。監看程式新增偵測：連續 8 次未完整交付且聊天閒置就停下並標示 ENGINEER_IDLE_NO_HANDBAC
J-0019 2026-10-11 [decision] T1-U2B1 驗收通過，派 T1-U2B2 | 兩次失敗的根因是工程端聊天停在 Instant 模式加上單元過大，不是策略或引擎設計問題。派工前把聊天模型調到 High
J-0020 2026-10-11 [experiment] T1 引擎端到端探針（冷啟動窗口 vs 黃金歷史） | 下單邏輯與黃金歷史一致。第一版連續 7 年探針的不一致是 180 天研究窗口到期，是探針設計錯誤，不是引擎缺陷
J-0021 2026-10-12 [mistake] r1 交付後 10 小時沒人驗收，CI 失敗無人知 | 把『帳本有待驗收或可開工項目』接成會叫醒我的排程（工程監督迴圈）。授權時效改成續期須明確記錄
J-0022 2026-10-12 [decision] 監督迴圈上線與驗證設計 | 第一次實戰驗證：C-0040（r2 於 10:33 交付）須在無人叫喚下被結案，帳本項目 C-0042 以指令自動檢查，12:09 到期，未過會被每小時檢查程式警報

## Engineering job env files
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/e1-env.txt
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/t1-env.txt
/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch/w1-env.txt
