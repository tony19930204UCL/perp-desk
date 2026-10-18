# NOW (generated 2026-10-18 19:03 UTC, do not edit)

## Needs a decision now (wake gate)
START C-0376 (blocker C-0374 is VERIFIED) 承接C0324/C0330原完整workflow產品驗收：r7真工程交付後核exactCI/actualentrypoint安裝/舊laun
START roadmap: G3-c untouched for more than 3 days
round 165958

## Ledger: items not finished
C-0027 DISPATCHED [C-0027 已派工 cron:fa25edbf3834 驗收:manual 到期:11-10 16:43] | handle=alive | 每日自我檢討：每晚 9 點讀記分卡與日誌，對照事前預測，跑一個新實驗並寫入日誌（結果只存本機）
C-0028 DISPATCHED [C-0028 已派工 cron:213f30fc8f7c 驗收:manual 到期:11-10 16:43] | handle=alive | 每週檢討：每週日晚 8 點對使用者回報預測對錯、教訓、下週實驗
C-0041 DISPATCHED [C-0041 已派工 cron:6997c01c6d9e 驗收:manual 到期:10-26 09:32] | handle=alive | 工程監督迴圈：每 30 分鐘檢查帳本，有監看結束待驗收、阻擋解除待開工、延後到期的項目就自動叫醒我處理（沒有項目時不花算力）
C-0062 DISPATCHED [C-0062 已派工 cron:fa25edbf3834 驗收:manual 到期:10-22 16:30] | handle=alive | 入場邏輯研究（G2-h）：由每日負責人迴圈每天最多兩個實驗往下做，結果進研究日誌
C-0069 DISPATCHED [C-0069 已派工 cron:fa25edbf3834 驗收:manual 到期:10-27 00:00] | handle=alive | 點子池（G2-i）：每日負責人迴圈至少一個實驗取自 lab/IDEAS.md，測完標結果
C-0071 OPEN       [C-0071 未派工:我選擇延後（不是阻擋）重評:10-27 00:43] | handle=- | Coinglass 熱力圖定期截圖歸檔：找出不靠模型就能自動截圖的做法（目前獨立腳本連不上瀏覽器，Windows Chrome 的除錯埠從 
C-0095 DISPATCHED [C-0095 已派工 cron:f571faf89f8d 驗收:manual 到期:10-21 00:22] | handle=alive | 巡檢迴圈 desk_patrol 上線觀察：第一週看它實際叫醒幾次、有幾次是真異常、有幾次只回 SILENT、有沒有自己修好過，然後調整檢查
C-0122 OPEN       [C-0122 未派工:等 item:C-0168] | handle=- | 驗收執行器合理化工程：TIMEOUT與FAIL分開，保存進度與分項耗時，整套與單項逾時分開，依實測耗時訂budget，不重複跑同head且已
C-0136 OPEN       [C-0136 未派工:等 item:C-0122] | handle=- | 進度感知验收：14分鐘軟檢查點、有進度同程序續跑、卡住保存診斷、單項與40分鐘硬上限，規格lab/ACCEPTANCE_PROGRESS_B
C-0168 OPEN       [C-0168 未派工:等 user:是否授權僅對PR61同分支同白名單AP3-diagnostics-r0做一次有界重試（原SEND_UNKNOW] | handle=- | AP3-diagnostics-r0送出不確定：保留原收據與草稿，未重送。需明確單次重試授權或原instruction唯讀交付證據解除
C-0169 OPEN       [C-0169 未派工:等 item:C-0062] | handle=- | G3-c實盤試點門檻重評：需先有一個正期望證據策略，未啟動實盤也未索取新認證
C-0178 OPEN       [C-0178 未派工:等 user:AGY file-only r2連ViewFile/read_file遭headless auto-denie] | handle=- | C0171候選修補驗收阻擋：AGY r2 ViewFile/read_file被拒且空response零修改；保留7FAIL/1import
C-0180 OPEN       [C-0180 未派工:等 item:C-0168] | handle=- | 折疊核准卡正式工程：既有診斷與安全驗收規格已保存，ChatGPT通道AP3送出不明解除前不派送，不冒稱修復
C-0191 OPEN       [C-0191 未派工:等 user:是否人工檢視PR62原專用聊天室當前 engineering/relay/approval_transacti] | handle=- | PR62 CA1-r3現卡既有policy雙拒絕：9行diff內35字元診斷字串誤判；另有bridge逾時，未交付未部署
C-0267 OPEN       [C-0267 未派工:等 file:/home/chihcheng/.hermes/profiles/perp-desk/lab/evidence] | handle=- | PR63 OAUX1終態UX工程續辦：原卡重驗與原policy接卡，真新head/CI/成果與scope部署，現未交付未驗收未部署
C-0270 OPEN       [C-0270 未派工:等 file:/home/chihcheng/.hermes/profiles/perp-desk/lab/evidence] | handle=- | PR63 OAUX1原卡及候選驗收目前未派工：WSL跨界啟動可重複實測門檻未通過，禁止三次成功檔案觸發接卡
C-0303 OPEN       [C-0303 未派工:等 file:/home/chihcheng/.hermes/profiles/perp-desk/lab/evidence] | handle=- | PR68後續正式放行受阻：T1短時4個有效cycle後raw depth105.0020/tick0.0100再拒，hash因果已驗；需合格
C-0363 OPEN       [C-0363 未派工:等 user:目前明確禁止恢復paper或新研究窗口，是否另行授權原C0080的試點恢復與live長時驗收？未授權不啟動。] | handle=- | 原C0080保留未授權部分：paper重啟試點/逐臂恢復與1h24h live寫入驗收未執行；12維持停止disabled
C-0376 BLOCKED    [C-0376 已派工 ?: 驗收:manual] | handle=- | 承接C0324/C0330原完整workflow產品驗收：r7真工程交付後核exactCI/actualentrypoint安裝/舊laun
C-0447 OPEN       [C-0447 未派工:等 item:C-0363] | handle=- | 紙盤重啟時恢復暫停的perp-desk-paper-fleet-watchdog(93f4f9882e43)與perp-desk-score
C-0544 OPEN       [C-0544 未派工:等 item:C-0363] | handle=- | 紙盤重啟時恢復暫停的perp-desk-daily-brief(c270ea65cb38)：10/17起因12臂停用每日簡報腳本exit1，
C-0545 DISPATCHED [C-0545 已派工 cron:6997c01c6d9e 驗收:manual 到期:10-19 10:38] | handle=alive | B方案觀察期：以既有每10分鐘監督(C-0398)連續24小時驗收失效執行者對帳，標準=DISPATCHED/BLOCKED皆有活holde
C-0559 OPEN       [C-0559 未派工:等 item:C-0447] | handle=- | H2 與 F1H 四臂分小時錯誤率檢查
C-0560 OPEN       [C-0560 未派工:等 item:C-0447] | handle=- | 新狀況警報：每小時跑記分卡，只在出現新觸發（狀態檔停更、虧損過半、窗口到期、來回數達門檻）時通知
C-0572 OPEN       [C-0572 未派工:等 item:C-0363] | handle=- | PR66 SA1-r2候選4193fbc2部署(paper_runtime_v2/v5、測試與文件)：僅在使用者授權紙盤重啟時隨12臂差分與
C-0585 OPEN       [C-0585 未派工:等 cmd:python3 -c "import pathlib,hashlib,sys;p=pathlib.Path('/] | handle=- | C0053/C0479 週期coverage保留缺口，原task不變。本輪真executor已完成兩個未覆蓋suite取證，SO1-r0歷史
C-0614 OPEN       [C-0614 未派工:等 item:C-0363] | handle=- | 原task C0222/G2-f真實maker排隊與逆選擇研究，承接C-0312未完成scope。12paper保持inactive/dis
C-0615 OPEN       [C-0615 未派工:等 item:C-0363] | handle=- | 原task C0170/W1 database is locked，撤銷C0272過期延後。本輪唯讀核127373至172755發現新增鎖錯
C-0659 OPEN       [C-0659 未派工:等 user:是否人工檢視C0104原專用聊天室當前lab/tests/test_collapsed_approval_tr] | handle=- | C0104 r3 原卡明確決策邊界：兩既有helper已拒絕，45行diff新增35字元test_C29_cleanup_retry_and
C-0666 DISPATCHED [C-0666 已派工 pid:1069422 驗收:manual 到期:10-19 17:21] | handle=alive | 多臂唯讀看板(PR58已驗收head c97eb2f6)在本機127.0.0.1:18771常駐，只讀loopback，不碰紙盤
C-0689 OPEN       [C-0689 未派工:等 user:是否授權改派AGY做C0112看板UX1並由其寫入倉庫新分支] | handle=- | C0112看板UX1被OpenAI安全檢查連擋兩次(同一update_file)：不再重試、不繞過；等使用者回覆是否改派AGY本機執行器做同

## Roadmap: active and ready
PROBLEMS:
 - G3-c untouched for more than 3 days
ACTIVE / READY:

## Project numbers (arm_scorecard.py)
SCORECARD 2026-10-18T19:03:25Z UTC
F1H-BTCUSDT-PAPER      第4.88天(剩16.12) 信號3 擋0 成交2 來回1 淨損益-0.74 手續費0.08 最大回撤1.04 持倉0 輪詢錯誤329
F1H-SOLUSDT-PAPER      第4.88天(剩16.12) 信號3 擋2 成交2 來回1 淨損益-0.66 手續費0.08 最大回撤0.71 持倉0 輪詢錯誤711
F1H-XRPUSDT-PAPER      第4.88天(剩16.12) 信號5 擋4 成交2 來回1 淨損益-1.03 手續費0.06 最大回撤1.39 持倉0 輪詢錯誤250
T1-BTCUSDT-PAPER       第4.88天(剩175.12) 信號0 擋0 成交0 來回0 淨損益0.00 手續費0.00 最大回撤0.00 持倉0 輪詢錯誤95
T1-ETHUSDT-PAPER       第4.88天(剩175.12) 信號0 擋0 成交0 來回0 淨損益0.00 手續費0.00 最大回撤0.00 持倉0 輪詢錯誤176
T1-SOLUSDT-PAPER       第4.88天(剩175.12) 信號0 擋0 成交0 來回0 淨損益0.00 手續費0.00 最大回撤0.00 持倉0 輪詢錯誤739
T1-XRPUSDT-PAPER       第4.88天(剩175.12) 信號0 擋0 成交0 來回0 淨損益0.00 手續費0.00 最大回撤0.00 持倉0 輪詢錯誤250
H2-PAPER-004           第4.88天(剩16.12) 信號5 擋3 成交4 來回2 淨損益-2.03 手續費0.18 最大回撤2.30 持倉0 輪詢錯誤132
W1-BTCUSDT-PAPER       第4.88天(剩16.12) 信號11 擋5 成交6 來回3 淨損益-3.37 手續費0.74 最大回撤5.10 持倉0 輪詢錯誤321
W1-ETHUSDT-PAPER       第4.88天(剩16.12) 信號12 擋3 成交12 來回6 淨損益-4.55 手續費1.08 最大回撤6.15 持倉0 輪詢錯誤182
W1-SOLUSDT-PAPER       第4.88天(剩16.12) 信號6 擋3 成交4 來回2 淨損益-0.13 手續費0.31 最大回撤1.59 持倉0 輪詢錯誤864

## Last 6 journal entries
J-0437 2026-10-16 [observation] 每日負責人檢討2026-10-16：20相關來回虧12.78，時段來源失敗升高，I08未過成本 | 尚無盈利優勢。不能因最新error清空而忽略80%單小時失敗，也不能因累計未滿5%宣稱來源健康。連三根不支持加碼或方向交易。
J-0438 2026-10-17 [observation] 每日負責人維護：12臂依令停止，路線圖撤下過期運轉與研究狀態 | 本輪完成合法狀態對帳，不冒稱workflow、coverage或研究成功。停更警報不構成解除暫停的理由。
J-0439 2026-10-18 [decision] 每週檢討2026-10-18：停盤無新樣本，C0053原文三對一分歧，完整workflow未通過 | 證據不足：沒有盈利優勢，整週監督coverage UNKNOWN，完整workflow與自主持續交付未證明，C0376不結案。
J-0440 2026-10-18 [decision] C0223方法反思重評完成，不以工程進度替代交易優勢 | 
J-0441 2026-10-18 [observation] 每日負責人2026-10-18：12臂維持停用，唯讀機械查核與到期反思歸位 | storage既有完成證據與full workflow未驗收分開。週期coverage UNKNOWN。無新紙盤資料或研究結論。無工程派送、瀏覽器操作、重送、核卡、策略變更或實盤動
J-0442 2026-10-18 [mistake] 每日迴圈與監督競態更正：撤銷C0690重複承接，C0223已完成 | 本輪有去重缺陷但CAS避免修改既有完成紀錄，已實際撤銷重複項。未新增工程派送或研究。

## Engineering job env files
none

## trade_review
# 覆盤摘要（自動產生，勿手改）

產生時間 2026-10-18 18:30 UTC，共 68 個完成來回。每筆的完整欄位在 lab/journal/trade_reviews.jsonl。

## 最近 24 小時（0 筆，淨 0.00 USDT）

## 依出場原因
| 類別 | 筆數 | 淨損益合計 | 平均淨 | 勝率 |
|---|---|---|---|---|
| stop | 32 | -51.83 | -1.620 | 0% |
| take_profit | 20 | +18.41 | +0.921 | 100% |
| data_gap | 15 | +1.12 | +0.075 | 53% |
| max_hold | 1 | -0.36 | -0.364 | 0% |

## 依日線趨勢（進場時）
| 類別 | 筆數 | 淨損益合計 | 平均淨 | 勝率 |
|---|---|---|---|---|
| down | 68 | -32.66 | -0.480 | 41% |

## 依標籤
| 類別 | 筆數 | 淨損益合計 | 平均淨 | 勝率 |
|---|---|---|---|---|
| saved | 20 | -22.28 | -1.114 | 20% |
| 無標籤 | 18 | -6.03 | -0.335 | 56% |
| gap_exit | 15 | +1.12 | +0.075 | 53% |
| knife | 14 | -22.17 | -1.584 | 0% |
| premature | 12 | -7.42 | -0.618 | 33% |
| left_money | 10 | +11.30 | +1.130 | 100% |
| noise_loss | 3 | -0.18 | -0.061 | 0% |

## 依臂
| 類別 | 筆數 | 淨損益合計 | 平均淨 | 勝率 |
|---|---|---|---|---|
| W1-ETH | 14 | -9.63 | -0.688 | 43% |
| W1-BTC | 10 | -6.14 | -0.614 | 40% |
| W1-SOL | 10 | -6.56 | -0.656 | 40% |
| W1-XRP | 10 | -1.78 | -0.178 | 50% |
| H2 | 8 | -4.02 | -0.502 | 38% |
| F1H-BTC | 6 | -1.08 | -0.180 | 50% |
| F1H-SOL | 5 | -1.44 | -0.288 | 40% |
| F1H-XRP | 5 | -2.02 | -0.403 | 20% |

標籤意義：knife=進場後先跌 1% 以上才有 0.3% 獲利（接刀）；left_money=停利後 4 小時又漲 0.5% 以上；premature=非停利出場而 4 小時後價格高於進場價；saved=非停利出場後 4 小時又跌 0.5% 以上；noise_loss=虧損小於兩倍手續費；gap_exit=資料中斷強制平倉。
