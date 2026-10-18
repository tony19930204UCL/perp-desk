# Workflow pipeline 優先驗收規格

Owner 授權：2026-10-17，workflow/pipeline 優先於新增紙盤功能。三項成果：自主審視研究系統與規劃優化、所有任務持久追蹤含執行者與後續動作、異常巡查有實際處置而不是只有通知。

## 實作路線與範圍
延伸既有 commitments ledger / roadmap / mailbox / deterministic acceptance / wake gate。保留既有 C-ID 與歷史，禁止另建無法對帳的第二份待辦權威。SQLite 可作可重建投影或經明確遷移的唯一權威，不因有 SQL 就宣稱可靠。先接回已交付 PA1（C0311）與既有 PR65/PR62 候選，不重複派送、不重跑已驗相同 head。

只作用 perp-desk，不變更其他 profile、不做實盤、不放寬行情15秒/tick/cost/risk、不自動merge main、不繞過核准拒絕或未知送出。工程由外部coding agent完成，operator定義要求、獨立驗收與部署。模型/cron配置只用官方支援工具，禁止盲猜免費模型、費用承諾或自動新增付費來源。

## 必要資料契約
每個工作需 task_id、parent/root ID、種類（一次性/週期/研究/事件）、目標、可驗收成果、優先序、來源與建立時間、dependencies、狀態、owner角色、executor handle及process start/lease、attempt ID、exact instruction/repo/branch/source SHA（不適用時明示）、acceptance command/判斷、證據URI/hash、下一個具體動作、next_check_at、最近實質進度、blocker種類/解除證據/負責人、retry budget、terminal outcome。
事件 append-only：event_id、task_id、attempt_id、時間、actor、from/to state、action/reason、evidence/hash、下一個責任人。不能靠touch/updated洗成進度。hash標識內容，不證明完成。

## 執行與巡查
1. 所有可執行規劃經既有入口登記，未派工僅真實 blocker 或有日期的owned defer。輸出lint是輔助，需針對未持有工作、孤兒與ready依賴做機械掃描。
2. 單一任務/attempt僅一個有效executor lease，失聯檢查PID/start或遠端handback，未知送出先read-only reconciliation，避免重複送/重跑suite。
3. 巡查每輪輸出持久 evidence：observed condition、classified incident、選擇的處置、真正dispatch receipt/handle或精確blocked/defer；通知不是處置。
4. 看見handback即接acceptance，通過接授權deployment/readback與下一個合法動作，不通過附精確證據返工。沒有部署授權則保持accepted_not_deployed。
5. Recurring任務按due slot、dispatch、output、coverage判定。cron ok不等於成果；每週未到期不誤報。程式本身健康而產品長時間無進展仍告警。
6. 模型判斷worker無法啟動/額度耗盡需獨立健康事件、保存pending工作、做有界可用性確認；不可把無模型檢查器當作有判斷者，不無限重試429，不改用未驗較弱模型自動接受工程。
7. 自主研究循環：讀最新紙盤錯誤/缺口/成本/成交證據，寫事實與可否證假說，自主選一個有界調查/優化，登記並派工或做已有readonly研究入口，結果及反證入journal，不只讀帳本報告。
8. 人工unblock需對應原incident/attempt做readback，不能一直留等使用者；已按卡不代表授權所有新卡或未知send重試。

## 驗收矩陣
隔離重播：worker死亡、supervisor rc0但零產品進展、READY依賴無holder、手動核准後續接、handback先於observer退出、錯instruction mailbox、CI pending、同head完成結果reuse、worker活躍跨soft budget、額度失效、missing artifact、每週未到期、健康cron有/無coverage、重複incident去重、兩writer搶lease、unknown send不得重送。

真正無人值守證明（不可由模型自報代替）：保存t0後無新使用者訊息區間，至少一個真實工程交付由巡查獨立發現、驗收/返工、正式記錄、執行已授權後續並讀回。另需真實紙盤異常被巡查發現並對應有dispatch handle或精確外部blocker。以事件時間、head/blob、process identity、結果hash對帳。manual rerun/啟用排程/一筆send/單元測試全部不等於此驗收通過。

## 交付狀態要求
分開：requirements、engineering_dispatched、candidate、isolated_verified、installed、unattended_proven。每輪保存current owner/handle/last evidence/next action。失敗保留原紀錄並明示仍未完成，不用新增協調任務替代真正修復。

## 2026-10-17 owner 擴充的整合完成門檻
使用者明確要求完整修好所有 workflow 缺口；不需例行按卡、轉貼、選模型。C-0324 是同一 operator 的整體續辦，不是新 observer。範圍包括既有 cron、launcher、技能/prompt、真入口與遺留承諾狀態/依賴；不允許只新增候選而保留舊壞路徑。
- 固定機械核卡和交付追蹤要有獨立可查 executor、真正檢查頻率與完整交易鎖；唯讀 GitHub watcher 不算 approval executor，deadline 不算 poll interval。接卡優先，保留首次可見/每次檢查/拒絕或transport失敗/人工unblock與交付時間；未知 send 不重送。
- 已部署入口、舊排程與舊技能/prompt 的 selectors/範圍/依賴需整合遷移並真讀回，保留原 authority IDs/history、驗證新舊路徑無靜默分叉，不能以 PLAN_ONLY 作已部署收據。
- 模型額度/可用性失效需有界的真實處置、pending保留、獨立健康及恢復驗收，不以持續通知或換未驗較弱判斷模型代替。
- 保留全部原安全gate/未知送出/拒絕邊界。執行者結束須有真實可查 successor 或精確不可自行解除授權boundary；等待無producer檔案與泛稱等父不合格。
- 必須以真正無人值守工程閉環及真實紙盤異常處置證據才完成；舊12項按各自scope逐項處置，非workflow業務不冒結。

## 既有12項處置範圍
C0053納入監督判定對照測試；C0071保留且本工作不做熱力圖；C0079/C0080/C0093/C0112保留依賴與歷史不冒結；C0122/C0136重用已交付驗收runner並查真正整合缺口；C0168/C0178保留精確授權邊界不擅重試；C0169保持研究門檻不啟實盤；C0180與PR62原修補對帳不重複造工程。此表是scope，不是已完成這12項。
