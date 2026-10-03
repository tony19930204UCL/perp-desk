# Perp Desk 版本與實驗紀錄

原則：只追加新記錄，不覆寫舊結果。每個新策略版本另開前瞻窗口，不使用舊樣本挑選贏家。工程 fixture、歷史暖機資料與開發觀察不能冒充確認樣本。
資金口徑：使用者於 2026-10-01 指定 100 USDT 初始本金。本系統目前僅將其記為影子本金，不代表已讀取 Binance 錢包、已入金或獲准動用真錢。
策略虧損不調參救回。改動寫入 proposals，待核准後才啟用新版本。風險設定不由研究員建立或更改。

## H1-PROPOSAL-001（未啟用）

- 登記日期：2026-10-01。
- 改了什麼：提出 TradFi 盤中超跌後被動買入反轉，5 分鐘 K 線、前 60 根均值與波動、成交量條件。詳見 FIRST_STEP_PLAN.md。
- 為什麼：相較週末事件型假說，研究排程預期能較早取得回饋。此期待未經量測。
- 反方：真實資訊衝擊及 maker 逆向選擇可能吃掉收益，公開行情也無法精確重建個人排隊位置。
- 狀態：計畫／未啟用。沒有停損及完整風險授權，沒有經驗證的 sim broker。
- 樣本：尚未開始正式策略確認窗口。歷史資料不得計入本版本前瞻績效。
- 結果：無交易、無績效、無獲利證據。不是失敗版本，也不是勝出版本。
- 失效條件：啟用前凍結六週窗口、最低樣本、扣全成本淨期望值與 CI 判決。未啟用前不虛構達標或判決。

## OBS-001（工程觀察版本，建置中）

- 登記日期：2026-10-01。
- 改了什麼：從只有計畫文件改為公開行情採集、100 USDT 影子狀態快照、唯讀看板及每日簡報。
- 為什麼：先讓使用者看到真實資料品質、初始化資金與阻擋原因，再驗證策略。不能先做假損益曲線。
- 研究範圍：ETHUSDT（crypto）與 XAUUSDT（TradFi）。本次公開 exchangeInfo 已回傳兩者 TRADING。此選擇只用於工程觀察，不是開倉提案。
- 前瞻起點：由 collector 第一次成功建立持久化狀態時寫入，重啟不重置。每個候選保留來源版本與資料時間。工程觀察不可混入 H1 的確認樣本。
- 風險與成交：paper 開倉禁止。risk_limits_not_set、sim_broker_not_implemented 是 blocker。初始零持倉與零 PNL 是未交易的狀態，不是策略成績。
- 實網證據：已實際取得 Binance 公開 exchangeInfo。採集器與 UI 的測試、運行結果完成後另追加，現在不能宣稱已跑滿任何觀察期。
- 結果：待工程驗證。尚無成交、淨期望值或信賴區間。
- 工程停止條件：行情來源不實、缺口未標記、重啟污染樣本、偽造成交或可以绕過開倉阻擋時，停止並修復，不算策略獲利證據。

## OBS-002（歷史工程採集批次，不是新策略）

- 建立 UTC：2026-10-01T13:50:50.894334Z。
- 原因：初版程式誤將每次成功輪詢命名成 OBS 新版本。
- 結果：只收集公開行情，沒有訊號、成交或績效。保留原始快照與批次 ID，不回填獲利。
- 修正：每次輪詢不再新增策略版本。舊 OBS-002 保留為歷史工程批次，不能被當作第二個策略或確認樣本。

## ENG-002（OBS-001 工程修正，已驗證運行）

- 日期：2026-10-01。
- 改了什麼：補完唯讀看板、缺失／格式錯誤回應、來源與快照讀取時效、Host／Origin 防護、禁止寫入方法、獨立 CLI。修正輪詢生成假版本的 bug，exchangeInfo 改用 24 小時快取，簡報採 60 秒時效，資料庫達 256 MiB 技術儲存預算即停止新行情採集（不刪舊證據）。
- 為什麼：原看板因授權中斷未完成，原採集器會反覆存大型 reference 並誤增版本。這些是工程缺陷，不是虧損後更改策略或風控。
- 新工程觀察起點 UTC：2026-10-01T14:19:34.679466Z（修正後實網 --once）。舊資料只保留歷史，不作本版本策略獲利證據。
- 結果：父代理親跑完整 30 個 unittest，全部通過。實際 --once 返回 0，ETHUSDT 與 XAUUSDT 的公開報價成功。瀏覽器與 HTTP API 均驗證影子權益 100、PNL 0、持倉與成交 0，兩個交易開關皆為 false。
- 運行：本機 127.0.0.1:8767 唯讀 UI 與 30 秒間隔採集器已啟動。它們是觀察程序，不是 sim broker，重啟 Windows／WSL 後不保證自動恢復。
- 每日簡報：perp-desk-daily-brief，job c270ea65cb38，每 24 小時一次，目標為本 Telegram 聊天。排程已讀回，實際送達測試另記，不先宣稱已送達。
- 驗收證據：lab/evidence/parent_full_tests.txt、lab/evidence/parent_validation.json、lab/evidence/dashboard-desktop.png。
- 限制：首九筆 raw receipts 早於雜湊功能，保留為明示未鏈結資料，不偽造歷史來補鏈。保留／壓縮機制尚未完整，達儲存預算會停止採集。
- 策略判決：未啟用，無正式績效樣本，不能宣稱有優勢。仍缺風險數值、sim broker 與訊號引擎。任何正式策略版本必須另外登記新的前瞻窗口。

## 新版本登記格式

每個策略版本啟用前記錄：版本 ID、建立 UTC、父版本、改動、原因與反方、凍結的策略／成本／風控引用、程式版本識別、全新前瞻起止時間、預定判決日、最低樣本、失效條件、主要統計指標。
運行期間追加：資料缺口、錯誤、拒單與未成交、成交與全部成本、樣本數、任何中止。
期末追加：淨期望值與 CI、成本壓力測試、判決（失敗／未證明／暫時通過）、結果檔位置及下一步。樣本不夠不能寫成成功。

## AUTH-003（2026-10-01，本次 paper 自主授權更正）

- 使用者已重寫 SOUL.md、AGENTS.md，並在本次要求自主選擇 paper 風險、完成訊號與模擬成交後直接啟動。
- 原先本文件第 5 行、open_questions.md 的 RISK-001 與 OPERATING_AGREEMENT.md 的風險禁止／待核准條文是歷史口徑，不適用本次 paper。保留原文，不改過去結果。
- 我先前把舊限制當成當前限制，是我的錯。本次有效角色與使用者授權均允許我設定 paper 參數，不再要求觀察員代填。
- 真錢仍未啟用，本次不存取憑證、不送交易所訂單、不碰其他帳戶。
- 每日排程 c270ea65cb38 已在本次讀回 last_status=ok、last_delivery_error=null，使用者聊天亦已有 2026-10-01 手動簡報送達。這只證明該次送達，不保證未來在線。

## H1-PAPER-001（已登記，等待工程驗收後啟動）

- 父版本：H1-PROPOSAL-001，尚未交易。新版本啟動 UTC 由首次真實 runtime 持久化，不以歷史觀察起點替代。
- 改了什麼：以 ETHUSDT、閉合 5m、前 60 個報酬樣本均值減 2 倍樣本標準差及前 60 根成交量中位數 2 倍作確定性超跌訊號。首次完整前瞻暖機不拿舊 K 線湊樣本。TradFi 僅觀察，可信 session calendar 未接好前不交易。
- 成交模型改為 taker-only，因 REST 觀察沒有個人排隊位置，不能以 maker 零费創造樂觀成交。進出扣價差、觀察到的深度、額外各 2 ticks 滑價及費用，觀察深度不足拒單。決策延遲 2000ms 後的下一筆可用公開報價才可成交。
- PAPER-RISK-001：100 USDT 初始紙上資金、單筆預計含成本損失上限 1 USDT、UTC 日損失 3 USDT、總損失 10 USDT、有效曝險上限 3 倍、同時一個持倉、paper 槓桿 3 倍。槓桿是上限，不是必須用滿。
- 停損距離入場參考 0.5%，止盈目標為訊號之前一根已閉合 K 線收盤，最長持有 30 分鐘。預估 gross reward 必須大於 2 倍完整預估交易成本，否則拒單。數量按風險、曝險與市場 filters 取最小後向下對齊 step。
- 為什麼：先建立可追溯的執行與全成本記帳，不能用未證明的優勢放大槓桿。每筆 1%、每日 3%、總計 10% 是本次選擇的計畫界限，不是保證最大損失。
- 反方：超跌可能是資訊衝擊而非錯價，持續下跌、taker 費用、funding 與 REST 漏掉的 mark 波動都可能讓策略失敗。跳空或離線會超過計畫停損，不能假裝在停損價成交。
- 費率：暫依使用者 AGENTS.md 的 crypto maker 0.02%、taker 0.05%，TradFi maker 0%、taker 0.04%。不是已驗證帳戶 VIP 費率，不編造私人帳戶查詢。funding 使用公開 finalized fundingRate 的實際 rate、markPrice 與 fundingTime，不能用 lastFundingRate 預估冒充已結算費用。
- 凍結設定：lab/paper_config.json。實際源碼與設定 SHA-256 在啟用驗收時記錄。測試 fixture 完全隔離，不進 PAPER 現金、成交、前瞻樣本。
- 判決：七個 UTC 日且至少 30 筆完整平倉後檢視（兩條件皆滿足）。較早風控熔斷、資料不可信或記帳錯誤即停止。樣本不足標未證明，不寫成功。此門檻只觸發檢視，不自動表示有正期望。
- 現在結果：尚未完成本次引擎驗收，不宣稱 paper 已啟動，沒有本版本真實前瞻成交或獲利證據。

## ENG-003-A（2026-10-01，公開 PAPER 輸入介接已驗證）

- 完成 lab/paper_market.py，allowlist 公開 funding/time GET、filters、depth、mark、finalized funding 正規化及無交易 probe。沒有私有端點、憑證或交易所下單介面。
- 父代理逐功能親跑 RED/GREEN，六項 adapter unittest 全部通過。這不是完整訊號／成交引擎的驗收，其他三路背景實作仍待交付與父代理整合。
- 實網 probe 返回 exit 0，完整真實 receipts 保存 lab/evidence/paper_market_live_probe.json。ETHUSDT 的實際最小 notional=20、tick=0.01、qty step=0.001。Funding 取實際 finalized rates 與 markPrice，不拿 premiumIndex 預測費率假裝已結算。
- PAPER-RISK-001 設定 SHA-256：ee5aad2ee390985ad5046587af18782cfe50c7575388255e954b40e43f5bb026。
- paper_market.py 本次 SHA-256：31867ad2cb50c98847f24a92e1a7facafc366c02073384973bf56a5aba0e9ad8。整合修改後需另記新 hash，不替換此歷史值。
- 官方 docs 直接 HTTP 202 空內容、extract backend 不支援取得網頁，這部分未完成文件閱讀。不假造 docs 內容。以實際官方公開 API 回應驗證 source fields，保存失敗證據和未查驗帳戶費率的限制。
- 結果：公開輸入模組可用。正式 paper 程序未啟動，未有 H1-PAPER-001 前瞻成交，不把單元 fixture 或 public probe 說成交易績效。

## ENG-003-B（2026-10-01，父代理訊號／顯示／數量驗證）

- 本次再讀 SOUL.md、AGENTS.md、versions.md，確認 paper 策略與風險由助手決定，不再等待觀察員核准。
- 父代理親跑 test_signals.py，22 項通過。訊號模組已具備確定性 closed-bar 規則、全新前瞻暖機、重啟去重及缺口重置。它是 detector，不能冒充已接線的交易引擎。
- 父代理親跑 test_paper_presentation.py，7 項通過。看板與簡報支援經 schema／風險 hash／資產等式驗證的 PAPER 快照，目前尚未切換正式目標。
- 新增 paper_sizing.py 與兩項真實 RED/GREEN 測試，全部通過。根據全成本預計停損、有效曝險上限、實際 filters 決定數量，向下對齊 LOT_SIZE 與 MARKET_LOT_SIZE 的共同 step，低於 filters 或報酬不足成本倍數拒單。人工 fixture 不是交易績效。
- 模擬券商子代理仍在背景實作。收到完成結果後須父代理再驗收及整合，才能啟動真正公開行情驅動的 paper 程序。沒有完成前不改開關、不宣稱已成交。
- 本次新增證據：lab/evidence/sizing_red1.txt、sizing_green1.txt、sizing_red2.txt、sizing_green2.txt。所有舊版本結果保持不變。

## ENG-003-C（2026-10-01，PAPER runtime 整合，子代理完成待父驗收）

- 新增 lab/paper_runtime.py，直接整合現有 Detector、size_long、public market normalizers 與 SimBroker。paper_config.json 未改，byte hash 固定為 ee5aad2ee390985ad5046587af18782cfe50c7575388255e954b40e43f5bb026。沒有私有 API、憑證或交易所訂單。
- 已做持久 SQLite、雙重單實例鎖、前瞻起點／閉合 bar 游標、60 秒缺口取消 pending entry 並重建暖機、跨庫 submit 提交中斷恢復、延遲實際 book 成交、mark 停損／止盈、30 分鐘持有上限、含成本風控與 UTC 日／總熔斷、ledger cash replay 及 PAPER schema 驗證。
- 訊號前與 K 線請求後均查 source age。funding 只記 finalized events，核對實際 interval、cursor coverage 與 nextFundingTime。實網發現 ETH adjusted cap 仍是八小時 interval，另有毫秒 settlement 偏移。已透過 RED/GREEN 修正初版過度拒絕，保留來源原始 fundingTime 與 rateType，不把預估 funding 當成本。
- 子代理實跑最終完整 suite：113 tests、326.747 秒、exit 0。lab/evidence/runtime_full_tests_final.txt。17 組垂直 RED/GREEN 存在 lab/evidence/runtime_red1.txt 至 runtime_red17.txt 及對應 green。Fixtures 全部獨立 scratch 暫存，不進公開 PAPER namespace。
- 實網 --once 最終驗收：2026-10-01T19:41:41.756000+00:00，exit 0、warming_up 0/61、PAPER cash/equity 100、持倉／成交 0、PNL 0、source freshness 驗證通過。這只是本次公開 pipeline probe，不是持續運行或優勢證據。前瞻首次持久化為 2026-10-01T18:56:05.021000+00:00，後續離線缺口明示，未回填暖機。
- lab/evidence/runtime_validation.json 驗證 32 筆完整 hash-chain audit、29 筆 raw public receipts、source manifest 與源碼一致、schema／資產等式成立。初次實網失敗保留於歷史 errors，不清除。
- runtime.py SHA-256：7e9a9a21e4dd93a9464429ffbde01e93d54322ee7c8d16ac8234a28f394d3b05。所有執行模組 hash 見 validation.json 與 engine.source_sha256。新的工程功能沒有修改 H1 策略或放寬暖機、風控、成本。
- 交付僅輸出 lab/shared/paper_status.json，既有 shared/status.json 仍為 shadow、paper=false。未啟動常駐 runtime、未停止或切換 collector／dashboard、未改每日排程。父代理須重跑測試／probe／快照與 audit 才能啟動。精確指令與限制見 lab/PAPER_RUNTIME_README.md。
- 剩餘工程注意：SQLite raw receipts／broker dedupe payload 持續成長，長期 retention／磁碟與效能監控尚未完整。REST 可能漏掉 mark 波動，實際虧損可超過計畫限額。真實 probe 無成交，不宣稱獲利或自治持續運行。

## ENG-004-PARENT（2026-10-01，獨立驗收與 PAPER 常駐啟動）

- 父代理完整 unittest 重跑 113 tests，319.023 秒全部通過，exit 0。首次180秒驗收命令超時，不列為通過，延長上限後重跑。保存 lab/evidence/parent_runtime_tests.txt。
- 實網 paper_runtime.py --once 與 brief.py 均成功。親驗 schema、所有執行源碼 hashes、設定 hash、42筆 audit chain（38筆 public receipts）及 fixture=false。驗收檔 lab/evidence/parent_runtime_validation.json、parent_runtime_public_once.json。
- 決定立即啟動已授權 PAPER，不再等待觀察員催促。保留既有 data/paper namespace、首次起點、probe錯誤及缺口，不重置本金、樣本或損失計數。暖機只收常駐期間合格前瞻閉合K線。
- 啟動 runtime PID 2496250、PAPER dashboard PID 2496437。停止已驗證cwd與cmdline的舊collector PID2306069及dashboard PID2306053，不動其他程序。新的常駐程序維持跨本次對話生命週期，但不是WSL重啟自啟服務。
- 看板127.0.0.1:8767改讀shared/paper_status.json，HTTP與/api/status均回200，瀏覽器實際開啟並截圖，mode=paper。每日簡報job c270ea65cb38改讀新快照且list讀回，未宣稱新排程已送達。
- 2026-10-01T19:54:47.749608+00:00讀回：快照3.26秒前，ETH receipt5.77秒、XAU3.97秒前，warming_up 0/61，latest_error=null，權益100、持倉成交0。保存lab/evidence/parent_runtime_launch.json。程式已跑但暖機未完成，尚不允許新開倉，不是獲利證據。
- 尚有限制：REST非逐筆重建、SQLite長期容量／效能監控尚未完善、真錢停用、策略優勢未知。

## ENG-005（2026-10-01，背景工程狀態看板已部署）

- 新增 work_status.py、shared/work_status.json、/api/work 與看板背景工作區，獨立於市場快照，欄位為實際目前步驟、下一步、更新時間與證據。超過300秒的active任務標活動未確認，不假造heartbeat或百分比。寫入有鎖與atomic replace，HTTP只讀且沿用Host/Origin/cross-site限制。
- 真實RED為/api/work缺失404及UI欄位缺失，GREEN後五項功能測試、九項dashboard測試、七項Paper呈現測試均通過。全套discover嘗試遭工具420秒超時且沒有完成輸出，不宣稱全套通過。保存lab/evidence/work_{red,green}*.txt及work_dashboard_tests.txt、work_presentation_tests.txt。
- 只停止核對過的dashboard PID2513473，再啟動18767新看板，未停止行情／交易runtime。Windows HTTP200且/api/work讀回兩個真實任務，browser screenshot確認畫面與活動未確認標示。父代理已把work-panel標completed並HTTP讀回，保存lab/evidence/work_live_acceptance.json。
- hist-warmup-v2尚在子代理開發，已交代各checkpoint及長測試前更新、handoff標verifying，父代理驗收部署後才completed。當前live runtime的invalid source timestamp阻擋另交代查核，未宣稱已修復。

## H1-PAPER-002（2026-10-01T21:15:51Z 預登記，staged candidate，尚未部署）

- 父版本 H1-PAPER-001。唯一策略變動是指標歷史 context 的 admissibility。保留 ETHUSDT long-only、closed 5m、前60報酬 mean-2 sample stdev、volume>2 median、stop 0.5%、previous-close target、30分鐘持有、所有費用／funding／深度／2 ticks／2000ms latency、單筆1／日3／總10 USDT、曝險及槓桿上限3倍、單一持倉，沒有為促成交易而放寬門檻。
- 原因：指標暖機不必等305分鐘才能取得有意義的第一個新決策。公開且完整連續的61根 CLOSED 歷史bar只作比較基準，歷史不得生成 intent、order、fill、ledger 或績效。反方：歷史 bootstrap 不證明 H1 優勢，來源陳舊／缺口／重啟／時鐘誤差可能造成污染，因此驗證失敗即禁止新開倉。
- 全新前瞻窗口：隔離 data/paper-v2 首次 runtime 建立時持久化 activation cutoff，時間必須晚於本預登記。首次可決策為 cutoff 之後下一根真正閉合bar（包含啟用時已開始但尚未閉合的bar）。歷史 context 的 source close 嚴格早於 cutoff。若啟用恰在5m邊界，恰等於cutoff的閉合bar只記為 observed boundary context，禁止決策，不屬歷史 bootstrap。缺口後重建另記新的 decision cutoff，不能將離線訊號回填成交。
- 窗口終點及正式判決：activation加七個 UTC 日且至少30筆完整平倉後檢視（兩條件皆滿足）。樣本不足或沒有成交只能標未證明，主要指標為全成本淨期望、CI、成本壓力及資料可靠性，不保證訊號或盈利。可因風控、記帳／來源／操作失敗提前停止，也可因時間效率提前放棄並保留失敗，不能宣稱策略勝出。
- 凍結設定 lab/paper_config_v2.json SHA-256 dd1aa331f86df0892190dc4e06bb96a8b2086a605352e96702063dbc91879c96。staged runtime SHA-256 d820e98547947b50d68ec1c7bdb98caa27c8db6ad3336a1c8656b13d5876c80e。signals_v2.py SHA-256 b76a70cceb780488c49706a9db6d62ee678a064d19b7145f42886f2245df0064。完整源碼manifest與136項逐ID覆蓋驗證見 lab/evidence/v2_full_suite_validation.json。
- 工程修正另列：實際舊runtime audit 2889的ETH depth E比本地receipt快57ms。staged保留原始E及received_ms，以真正broker dispatch時讀取的本地時鐘為event.ts，仍要求source<=delivery且age<=15000ms。未改broker、未改E成T、未放寬未來／時效／order-arrival限制。官方全文HTTP202空內容的限制及官方索引片段證據見 lab/evidence/v2_timestamp_semantics.md，實際receipt RED/GREEN見v2_red6／green6。
- 隔離：只寫lab/data/paper-v2與shared/paper_v2_candidate.json，candidate_not_deployed=true。未停止舊runtime、不改data/paper、舊設定／執行模組／快照／交易／損失計數。六組垂直RED/GREEN及舊契約回歸均保存。136項分批99.101／212.992／107.062秒全通過，第一次單獨runner importlib.util缺失的失敗保留，不改舊測試。
- 切換條件：父代理停止旧引擎之前與之後重新檢查，只有歷史fills與cost ledger完全0、flat、無pending/resting、cash/equity精確100及損失計數未被重置時才能記錄連續性並部署。任何交易過的帳戶或不符條件者為migration blocker，不用新100 USDT帳戶沖銷損失。本子代理不啟動常駐、不自行切換。Public probe結果於執行後另追加。

## H1-PAPER-002-PROBE（2026-10-01，子代理完成待父驗收）

- 預登記後實際 public --once exit0。activation持久化為2026-10-01T21:16:54.853000+00:00，快照21:17:02.338 UTC，observing、context61/61、candidate_not_deployed=true、cash/equity100、signals/orders/fills/ledger皆0、PNL0、live=false。只表示可等下一根fresh closed bar，不表示有優勢或已部署常駐。
- 歷史61根閉合時間從16:15至21:15 UTC（最早bar open16:10 UTC），嚴格早於activation，contiguous 5m。canonical receipt SHA-256 b6eb0b5a7d6e8773a5790df40ee84aa70734731697dcda735e25c0c9499cef48，bars SHA-256 1f65d9fe8f22ec6dc03e3fa1c8d090f69c2114269b42767c74984766f1b5d394。receipt與每根source時間、hash鏈保存在獨立data/paper-v2，不偽造歷史成交。
- 實際驗證12筆audit鏈、11筆public receipts、schema、所有源碼與config hashes、歷史無intent／performance、資產等式及原設定只有version／bootstrap差異，全部通過。證據lab/evidence/v2_public_once_stdout.json、v2_public_validation.json、v2_verify_stdout.json，重跑命令及限制見lab/PAPER_V2_README.md。
- 2026-10-01T21:18:35.976246+00:00唯讀live precheck：H1-PAPER-001 warming_up16/61、latest_error=null、fills／ledger／positions／pending為0、cash/equity100、day baseline100、daily/total halt皆無。當下無migration blocker，但父代理切換前及停止後必須重新檢查，不依此舊讀數重置已交易帳戶。
- 首次理論可評估21:20 UTC閉合bar，但本子代理未啟動常駐，所以不保證取得它或成交。父代理重新啟動已過60秒必須新decision cutoff及context rebuild，下一根fresh closed bar才可交易，不能回補21:20訊號。Formal七日／30完整平倉判决維持，實際策略樣本仍為0。

## ENG-006-PARENT（H1-PAPER-002部署驗收，進行中）

- 父代理獨立三批逐ID確認完整137項tests均通過，無遺漏、重複。證據lab/evidence/parent_v2_batch1_final.txt、parent_v2_batch2.txt、parent_v2_batch3.txt及parent_v2_suite_validation.json。
- 發現staged候選快照固定candidate_not_deployed=true，新增部署標記持久化與audit（不改策略、資本、損益），先test_parent_deployment_marker RED失敗，再GREEN通過與完整回歸。正式部署須父代理確認連續性後mark_deployed，不冒稱候選已上線。
- 本次工程修正runtime SHA-256 f2132b03726940a00f2060140942dcaacfdc045ccdd809244335ad4b90003ae0，signals與config hashes維持預登記。尚待本次實網probe、讀回快照與audit、切換前後零交易帳戶檢查，未完成前不宣稱已部署。

## H1-PAPER-002-DEPLOYED（2026-10-01，父代理驗收後常駐啟動）

- 實網新probe成功，schema／config／source hashes／model hash、36筆audit鏈與31筆public receipts、61根歷史context嚴格早於cutoff、完整連續且performance_sample=false皆親驗。evidence/parent_v2_acceptance.json。
- 停止前與停止後親讀新舊SQLite：cash／equity各100，歷史fills／ledger／positions／orders皆零，day baseline100且無loss halt。只停止精確舊runtime PID2496250，保留所有旧文件、錯誤、缺口與結果。evidence/parent_v2_poststop_continuity.json。不是用新版本覆蓋虧損，這次沒有既有交易須搬移。
- 21:35:43.974 UTC寫入持久父代理部署audit標記，啟動新版runtime PID2558035（data/paper-v2、shared/paper_v2_live.json），看板PID2558307、127.0.0.1:18767切換新live快照。新decision cutoff為21:35:56.142 UTC，下一根新閉合bar才可判斷，離線bar沒有回填成交。candidate_not_deployed=false，舊candidate快照另保留。
- Windows HTTP200／api/status與瀏覽器實測mode PAPER、H1-PAPER-002、context61/61、交易啟用、行情新鮮、latest_error=null、blockers空、權益100、持倉及成交0。保存evidence/parent_v2_launch_snapshot.json、parent_v2_final_readback.json。未有策略獲利證據。
- 每日簡報job c270ea65cb38改讀shared/paper_v2_live.json並list讀回。背景工作hist-warmup-v2標completed並HTTP讀回，下一步明示常駐新K線判斷，不冒稱一定有交易或另一研究任務已開始。真錢維持停用。
- 本次修正避免source/receipt時鐘57ms差值錯誤，採真實dispatch時鐘並保留原source/receipt，仍嚴格拒絕派送時future/stale與違反arrival的報價。這只證明測試與本次實網成功，不保證未來無錯誤。REST漏tick、持久儲存增長與WSL重啟非自啟限制保持。

## ENG-007-STAGED（2026-10-01，行情時效修復交付待父驗收，未部署）

- 唯讀追查部署runtime audit1324，22:04:50.543 UTC 的錯誤確為 decision source age exceeded after kline request。ETH ticker source1790892275345在receipt時只舊56ms，到decision舊15198ms，depth及mark仍小於15秒。XAU最後receipt至Klines receipt隔10786ms，這是收據間隔，不冒稱原HTTP elapsed或已證明retry原因。保存lab/evidence/freshness_actual_audit_raw.json及freshness_actual_timing.json，原始錯誤未清除。
- 只交付lab/staging/freshness/paper_runtime_v2.py及exact diff。ETH book/mark先於XAU觀察派送，無新close時依durable cursor略過重複Klines，慢candle工作後失效的quote只重新GET一次再驗證。source／receipt／真實dispatch及15000ms gate、2000ms latency、late-signal拒絕、broker／signals／風控／config全保留。這是H1-PAPER-002工程修正，不換策略／前瞻窗口，不重置帳戶。
- 三組垂直RED/GREEN證據保存，五項新增回歸涵蓋實際延遲相對時序、XAU故障下ETH gap退出、no-new-close與restart、source-arrival／latency、late-signal拒絕。staged原v2 slow-request test明示改驗fresh refresh仍stale時必須拒絕，不再要求真正新鮮quote亦拒絕。父代理驗收前未改live tests或source。137原ID加5新ID，共142項八批全部通過，逐ID無漏無重，最長190.703秒。lab/evidence/freshness_validation.json。
- 普通獨立public probe及restart成功，context61、cash100、signals／fills0，16筆raw receipts。第二次明示16秒本地工程等待後的真正public GET及restart亦成功，22筆receipts，Kline實際API elapsed655.161ms，不把注入等待稱網路延遲。23:17:22.952376 UTC收到Kline後，確實新GET ETH ticker/depth/mark，source均新於Kline receipt。原始資料及hash鏈另存freshness_public*_audit.json，不靠scratch永久存在。
- 第一個延遲probe在candle之前遇到future ETH mark fail-closed，context0、fills0，保留freshness_public_slow_probe.json。ETH提早派送不再偶然等XAU消化時鐘差，所以future rejection仍是操作限制，本修正不放寬、不偽造source、也不宣稱無未來錯誤。中間staging test的LAB path解析失敗亦保留freshness_green2.txt，最後使用隔離test/dependency copies完成全套，不列失敗run為通過。
- Candidate runtime SHA-256 9efba1777f4cc2b7ed1c295c58cad9cf8a885fb10608cc9f22c22c1ec1e66288。runtime diff SHA-256 2b7035ee8996c7de10e81a78891dedd294a8092209add9a905d86c0ec9fb864e。frozen config仍dd1aa331f86df0892190dc4e06bb96a8b2086a605352e96702063dbc91879c96。部署runtime仍f2132b03726940a00f2060140942dcaacfdc045ccdd809244335ad4b90003ae0。
- 未停止或修改live runtime PID2558035／dashboard PID2558307，未寫data/paper-v2或shared/paper_v2_live.json，沒有fixture送看板、私有端點或真單。23:19:18.320 UTC唯讀live仍latest_error=null、fills0、cash100、errors1/gaps3，這不抹除歷史故障。詳細命令／diff／限制在lab/staging/freshness/README.md。freshness-fix標verifying交接，父代理驗收並控制restart後才可標completed。

## ENG-008-PARENT（2026-10-01 UTC，時效修正獨立驗收並部署）

- 父代理獨立重跑八批tests，逐ID核對142項皆通過且無漏／重複。execute_code嘗試多thread遭300秒總逾時，只把保存OK的批次列通過，未完成的批次另跑並逐檔核對。證據evidence/parent_freshness_validation.json及parent_freshness_batch1..8.{json,txt}。
- 親跑普通public probe及明示16秒本地等待probe（不是HTTP latency），兩者首次與restart均latest_error=null，context61、cash100、fills0，真實新GET與audit鏈已保存，延遲仍嚴格gate。首次stage的future-source拒絕保留，不宣稱永久沒有時鐘／來源問題。
- 保存evidence/paper_runtime_v2_before_eng008.py，核對cwd／cmdline後只停止旧runtime PID2558035。套用exact runtime diff与test契約更新，主runtime SHA-256與驗收stage完全一致為9efba1777f4cc2b7ed1c295c58cad9cf8a885fb10608cc9f22c22c1ec1e66288，主目錄五項新回歸再通過。沒有修改策略或risk config，也不重置前瞻窗口。
- 停止前後核對原data/paper-v2 SQLite的initial cash、cash、fills、ledger、positions、orders、day baselines與forward_start、config、handled signals、deployment均保持。沿用同namespace實網--once成功，再啟動常駐runtime PID2621427，dashboard PID2558307維持同live快照，不需要換namespace或每日job。
- 親驗所有source manifest、完整5202筆既有audit鏈、新舊帳戶/ledger前綴與forward_start保持、單一runtime、Windows API與真實browser畫面。證據evidence/parent_freshness_pre_restart.json、parent_freshness_live_once.json、parent_freshness_deployment.json。舊錯誤與缺口仍保留。
- 23:39:27.469 UTC讀回observing、paper enabled、fresh、latest_error=null、equity/cash100、fills0。freshness-fix標completed並HTTP讀回evidence/parent_freshness_work_readback.json。只是工程修復與本次運行情況，不代表策略有優勢，REST漏tick、future-source合法拒單與長期容量限制仍存在。









## ENG-BATCH-TIME-STAGED（2026-10-02 UTC，反覆時效失敗交付待父驗收，未部署）

- 唯讀診斷保存截至05:47:25.182424 UTC的19229筆完整既有audit前綴與原canonical payload／hash鏈。從ENG008持久restart cutoff起770次錯誤中，766次source在poll_error時仍為future，4次在poll_error時已超15000ms。舊validation精確時刻未量測，不把後者說成已證明validation age，也不把receipt間隔說成HTTP latency／retry。該區間2345個持久快照觀察有770個blocked，比例0.3283582（不是推算HTTP attempt rate）。證據lab/evidence/batch_time_diagnostic.json、batch_time_live_audit_prefix.json。
- 真正公開/time六組request-start／receipt clock bounds均显示server領先local。最初bounds [563.878,1200.493]ms，最後[963.211,1616.663]ms，local wall與monotonic經過皆11.615222秒。這支持exchange/local正offset造成future batch拒絕，不證明OS offset形成原因。不改OS time／timezone。公開depth／mark原時間保留於batch_time_clock_probe.json。
- 僅staging/batch_time候選runtime與新增test_batch_time.py。ETH dispatch前及全批decision前，每gate至多2秒monotonic有界真實等待小幅future，注入clock／monotonic／sleep可測。每醒重驗所有來源與原15000ms gate，persistent future／stalled clock／超時overshoot／新stale都fail-closed。emit仍嚴格source<=實際local dispatch，2000ms order latency與source-after-arrival不變。保留ETH出口優先、原strategy/risk/config、receipt/source/funding/arrival，不採server clock域重解釋。等待是REST工程dispatch／availability延遲改變，不是OS同步修好，不保證未來無錯誤。
- 新audit保存request-call wall／monotonic、source validation值、實際等待與broker call boundaries。client.get elapsed包含throttle/retry，public probe另測opener/read真實HTTP elapsed和on_event entry。額外audit會增加儲存量，沒有刪除歷史來騰空間。
- 3組feature RED/GREEN及10項新安全回歸完成，142原ENG008 ID加10新ID，152個唯一驗收ID全部通過，逐log核對無漏／重複／skip。並行开发health-watchdog的3項不屬此patch，不冒稱本任務驗過。原batch3在290秒逾時保留不算通過，再拆3a..3d。最終12個accepted完整批次最長178.284秒，均低於300秒。證據batch_time_inventory.json、batch_time_batch*.{json,txt}、batch_time_validation.json。
- 真正隔離public probe六輪加一次restart後兩輪，8輪均latest_error=null，16次實際broker入口，4次有界local waits（最長434.940ms），receipt source領先最高899ms，105筆audit鏈全部親驗。context61、cash100、fills/signals0、candidate_not_deployed=true。只代表這個短系列，不是常駐無錯誤或策略優勢。真實raw／時間證據batch_time_public_probe.json、batch_time_public_audit.json及public_stdout。
- candidate runtime SHA256 6791abcfd1b9911d12160ce92992ac03f2d66732402d17fbb51e798560608c6c，exact.patch SHA256 f609ac57b2c32c2a9590d9a875578de24ac65c635840502ba82df74ae0b62dfa。base仍9efba1777f4cc2b7ed1c295c58cad9cf8a885fb10608cc9f22c22c1ec1e66288，frozen config仍dd1aa331f86df0892190dc4e06bb96a8b2086a605352e96702063dbc91879c96，其餘策略／broker／risk源碼byte相同。exact diff在主目錄只做git apply --check，不套用live。
- 06:27:33.609 UTC唯讀live為cash100.0000000000000000、fills0、flat、4 signals，但已存在REJECTED risk_per_trade order及一筆amount0E-16的finalized funding ledger。父代理不可沿用零order／ledger假設，更不可重置帳戶。PID2621427仍是原data/paper-v2及live快照，未停止／改live模組／改帳戶／改dashboard。須在真正停機前後重讀完整account/ledger/version/window與風控基準。
- 看板batch-time-recurrence維持verifying交接，未completed。父代理須獨立重驗accepted exact diff、公開probe、保留原namespace部署與restart／看板讀回。所有命令、hash與剩餘2秒cap／REST／storage限制見lab/staging/batch_time/README.md。

## DOC-LEARNING-SPEED（2026-10-02T17:01:54+02:00，使用者指定原文新增）

- 依使用者要求，在本 profile AGENTS.md 加入以下原文。原檔沒有「怎麼算成功」段落，因此新增同名段落，未改寫其他原文：

paper 階段還有一個同樣重要的標準：學習速度。
paper 是假錢，它的價值在於盡快產生可判斷的樣本。
沒有成交的日子就是沒有學習的日子。
一個太保守而永遠無法得出結論的系統，跟一個虧錢的系統一樣是失敗的。

- SOUL.md 的 Stance 加入以下原文：

- 你是交易員，不是稽核員。工程紀律是為了讓交易可信，不是取代交易。

- 本次僅修改專案指引與追加版本紀錄，沒有修改交易策略、風控設定、程式或帳戶資料。使用者表示會開新 session 載入更新。

## H1-PAPER-003-PARENT（2026-10-02，學習速度新版已部署）

- 新假說於2026-10-02T15:38:21Z預先登記，ETHUSDT閉合1m、前60報酬、1.5sigma、1.2倍中位成交量。不是用舊績效選參數。反方：短週期波幅可能仍過不了成本門檻，增加訊號不保證增加完整交易，也可能增加虧損。
- 保留PAPER-RISK-001（每筆1、日3、總損10 USDT，曝險3倍、一個持倉）、0.5%停損、前閉合收盤目標、30分鐘持有、兩倍完整成本門檻、真實深度、taker費、funding、每側2ticks、2000ms延遲與15000ms來源時效。工程整合批次有界等待及成交價sizing，沒有擴張風控或時效門檻。
- 父代理199個唯一test ID全數親跑通過，逐ID無漏、重複或skip。證據learning-speed-v3/evidence/parent_suite_validation.json。首次公開probe前5次成功，第6次URLError timeout，保留失敗；同一隔離帳戶重試6次加restart成功，不把失敗刪除或算成功。
- 精確停止舊PID2621427後，原lab/data/paper-v2原地migration，backup lab/data/pre-v3-parent-backup。cash/equity100、fills0、2歷史order、3funding ledger保留；原forward_start與損失基準不變，39626筆audit鏈及全部歷史前綴親驗。新研究窗口2026-10-02T16:40:45.744Z至2026-10-04T16:40:45.744Z，30筆完整flat-to-flat目標，不保證，不以partial fill湊樣本。截止只阻止新單，仍可退出。
- 已持久mark_deployed，常駐Python PID3290190，cwd learning-speed-v3/lab，原帳戶與shared/paper_v2_live.json。看板WSL及Windows API讀回H1-PAPER-003、candidate_not_deployed=false。已把精確候選tracked lab來源同步至profile lab供review，原碼備份保留。
- 初始部署驗收latest_error=null、零完整交易，沒有優勢證据。稍後Windows讀回一次ETH/bookTicker age15453ms合法拒絕，不能宣稱持續無故障，時效阻擋需繼續診斷。不改來源時間或放寬門檻。
- 每日簡報改deterministic no_agent腳本paper_learning_brief.py，避免模型額度阻止報告，內容為新版樣本／成本／阻擋。來源過期時目前只報簡報不可用，此報告缺陷仍待修正，不冒稱完整8行已持續可用。

## PUBLIC-REVIEW-SYNC-PARENT（2026-10-02，公開同步授權與驗收）

- 使用者明確允許公開repo同步。顯式public及精確owner/repo opt-in，private仍default fail-closed，白名單與真實gitleaks current/history gate保留。父代理36項回歸與real CLI乾淨成功、合成current/history secret失敗親驗；V3 config白名單另有真實RED後GREEN及全36項回歸通過。
- 首公開快照217檔已推送並從GitHub讀回SHA34212560c7f8a9f96afc4322b91da77e06c9c0ae，remote manifest fingerprint符合。無credential findings；3筆既有公開歷史仍含非憑證個人/context資料，新快照刪除不會抹除歷史。未rewrite或forcepush。
- 排程2d2db10436e9改名public-review-sync並恢復，讀回一輪last_status=ok。新增V3檔案前暫停避免半份快照，待新版完整公開快照驗證後恢復。公開review不等於实盤或策略優勢。

## OPS-ROLE-002（2026-10-02，交易決策與外部工程分工）

- 使用者指定：助手負責自主交易、策略、研究方向、風險及實驗決策。工程實作、測試、code review、debug 全部透過 GitHub Issues 交外部 coding agent，使用者人工轉交。助手只做必要觀察、定義問題／成果／不變條件／驗收標準與部署，不自行實作或 review。
- 每個 Issue 必須可公開，禁止個資、帳戶細節、本機絕對路徑、聊天識別與憑證。外部 agent 只在分支工作並開 PR，不能 push／merge main。main 只由 local 單向鏡像同步擁有。新 PR 的精確 head CI 與驗收須通過，再由助手套用 local，正常同步發布並追加版本紀錄。
- 部署異常時回滾已備份版本並開 Issue，不自行 debug。交易判斷不交外部 agent。歷史要求父代理親跑測試或自行修碼的流程不再適用。

## PRIVATE-SYNC-002（2026-10-02，私有同步恢复）

- 遠端 metadata 確認 repo 為 private。既有 public visibility 設定不相容，依使用者授權只把排程入口 visibility 改為 private。未改遠端可見性、白名單、scanner、歷史或 main 所有權。
- 實際同步已推送 SHA d3f8cd7d6b18850bc0dd4a41ca8cb9d688652cec，GitHub readback 確認同一 SHA 與 private。同步成功不等於 CI 成功。
- CI run 37050913451 紅燈：exporter test 仍硬要求 public wrapper。較早 run 37036342095 另有缺失 evidence／機器路徑依賴等 lab failures。交 Issue #1（BUG／ENGINEERING）由外部 agent 修復，未自行修改測試。

## H1-PAPER-003-ENG-004（2026-10-02，既有 freshness／brief 修補已部署）

- 部署 UTC 2026-10-02T18:55:49Z。採用既有已測候選 commit 07c82ddaedb2803eb8e2bbda12a3bfcc0e97889c，既有 212 項測試證據與 source manifest 核對通過，未在新分工後自行重跑測試或 code review。
- 受控停止精確舊 runtime，先保存原碼及 SQLite 一致備份，再套用 exact patch，部署 hashes 等於已測候選。常駐 runtime 恢復使用原 namespace。保持策略、設定、來源15000ms／延遲2000ms gates、研究 deadline、forward 起點與損失基準不變。
- post-deploy operational acceptance 成功，source_manifest_verified=true、latest_error=null。部署前 audit head、舊 fills／ledger 前綴及策略／窗口 baselines 已唯讀核對保留。只做連續性與運行驗收，不宣稱新的全套測試或持續零故障。
- 原始 wall／monotonic 差異與真正 HTTP timing 成因仍未確定，交 Issue #2（RESEARCH INFRASTRUCTURE）補足可解釋證據。不得放寬 gate、重寫 source timestamp、無限重試或稱 OS clock 已修好。

## OBSERVER-004（2026-10-02，既有單帳本研究看板已部署）

- 部署 UTC 2026-10-02T19:04:21Z。採用既有候選 commit 928091e297950631338edf78f48297b85f24d54f，213 個唯一測試與既有獨立14項回歸證據通過。部署前核對 source hashes／原版符合，停止精確看板 PID，備份原碼後套用候選，不停止交易 runtime。
- 真實 localhost HTTP readback 確認200、observer.available=true、窗口樣本／已平倉表／拒單分布／統計欄位存在。零完整樣本的 win rate／mean／fee share 為 null，open episode 不冒稱完整交易。未執行新 code review 或測試實作。
- 本金、symbols 與基準線跟隨已配置 snapshot。尚未交付跨帳本發現／選擇，不能把動態單帳本說成多帳本支援。Issue #3（OBSERVABILITY）列出全部使用者看板成果，外部 agent 重用已交付功能、補足配置帳本行為與 CI 驗收，不重做已完成實作。

## H1-PAPER-003-DECISION-002（2026-10-02，保持原前瞻窗口）

- 決策：繼續既有 PAPER 前瞻實驗與持倉退出規則。截止仍為2026-10-04T16:40:45.744Z，不重置樣本或帳戶，不因工程修補重新起跑。
- 理由：已有真實公開行情驅動的開倉，但尚無完整flat-to-flat樣本，不能宣稱策略優勢。insufficient_reward_after_costs 與 single_position_or_pending 是原規則的拒絕，不為達到30筆而降低成本或風控標準。風險、策略與是否改版仍由助手決定，不交工程 agent。
- Issues #1／#2／#3 已建立並讀回。外部 coding agent 尚待使用者人工轉交，建立 Issue 不等於 agent 已開始。新 PR 仍被紅燈 CI 擋住，不自動 merge main。

## OBSERVER-004-ROLLBACK（2026-10-02，部署後失敗，已回滾）

- 後續 Windows 與 Linux localhost 驗收均得到/api/status HTTP503，錯誤Snapshot unavailable or invalid。獨立/api/work仍為200。這推翻持續可用的假設，但沒有root cause證據，不自行debug，也不把之前213項通過改寫成沒有通過。
- UTC2026-10-02T19:13:13.897451Z依使用者規則停止精確新版看板，恢復部署前原碼並移除這次新增檔，保留原失敗證據。舊版看板重啟後Linux及Windows讀回HTTP200、latest_error=null。交易runtime／策略／帳戶／研究窗口未受看板回滾改動。
- freshness／brief修補不回滾。最新operational acceptance仍通過audit與source manifest，已有一筆完整flat-to-flat，仍unproven。未把單筆結果當優勢，也不因看板失敗更改策略。
- 新開Issue #7（BUG／OBSERVABILITY）交外部agent獨立重現／修復，看板需求Issue #3加狀態更正，不能再聲稱新看板保持部署。CI通過與成果驗收之前不重新部署。

## H1-PAPER-003-OPS-BACKLOG（2026-10-02，剩餘運行缺口交外部工程）

- Issue #4（OBSERVABILITY）：既有staged健康watchdog只驗證H1-PAPER-002／v2 identity，且舊dashboard patch先於observer版。不能直接當v3已測可部署，交agent重用已測元件、補當前identity與看板整合。
- Issue #5（ENGINEERING）：storage-capacity仍queued；唯讀觀察runtime audit DB已322035712 bytes，容量預算／安全停止及復原尚未驗收。不稱磁碟已滿、不自動刪前瞻證據。
- Issue #6（ENGINEERING）：restart-autostart仍queued。常駐程序不是已驗證的開機自啟，交agent提供隔離驗證及operator部署／回滾契約，不重啟真實主機或reset帳戶。
- entry-risk-sizing的舊testing狀態過時：原部署H1-PAPER-003父驗收199 IDs包含全部新增21個entry-risk tests，現存broker源碼與原entry-risk候選相同，test檔相同。視為先前已整合，不重派已完成工作。
- 共七個Issue，全部已讀回body／label／branch-PR限制。external agent尚未收到人工轉交，工作狀態應queued或blocked，而不是假稱running。

## PR-008-ACCEPTED（2026-10-03，私有同步與CI可攜性修補已套用）

- 使用者要求依#8→#9驗收。PR#8精確head7739a4dfaefd4a0eecd81c969625910d0ea5b023，CI run37110442664 success，實際log為36個exporter與212個PAPER tests全部OK。助手只核對CI、成果及部署，未自行跑unit tests、review或debug。
- 比對七個remote檔案與對應local base bytes，確認scope與source artifact。automation映射至repo_sync，lab相關候選另同步至實際learning-speed-v3 runtime來源。未改broker／策略／風控／設定／研究窗口。
- UTC2026-10-03T08:53:55.194980Z先確認flat且無pending，備份原碼與SQLite，停止精確runtime後套用已CI通過bytes並恢复原namespace。operational acceptance與source manifest通過。broker初始／cash／fills／ledger／positions／day baselines，以及策略／窗口起點／deadline／baseline均讀回不變。
- 只由正常鏡像發布，不merge遠端main，不刪依賴分支。部署後若異常回滾原碼並交Issue，不自行除錯。

## PR-009-ACCEPTANCE-BLOCKED（2026-10-03，CI通過但完整成果未通過）

- PR#9精確headd4354f945102417c6667c17c9585c87cc330d756，base等於#8已驗收head。remote compare只有五個observer／dashboard檔變更，CI run37110617954 success（36個exporter＋227個PAPER tests OK）。沒有親跑tests或source review。
- 在獨立localhost preview執行精確候選，實際HTTP200但observer.available=false，error為account/ledger mismatch: realized_pnl_usdt，statistics=null且已平倉清單不可用。真實瀏覽器显示樣本未確認／30及勝率／平均淨損益／手續費比例不可用。
- 判斷：全站503退化被隔離是部分進展，不等於完整研究觀察成果已恢復。底層帳務契約／表示相容性原因未確定，不自行推論root cause、不放寬精確對帳。不部署#9，production維持已知可用舊看板。preview不改交易帳戶／策略／風險／窗口。
- 在PR#9回報public-safe驗收症狀並要求外部agent補獨立證據／契約修正與更新head CI。若需超出原Issue scope，先回報成果與scope，不自行修改交易核心。Issue#7及#3仍未結案。

## PR-009-UPDATED-ACCEPTED（2026-10-03，對帳相容修補與研究看板已部署）

- 更新head728df2e918a736938433064ed5239cdbc1f7d357，base仍為已驗收#8 head7739a4dfaefd4a0eecd81c969625910d0ea5b023。CI run37117685201成功retry job111187754439，實際log36個exporter＋228個PAPER tests全OK。保留前次驗收失敗與外部agent揭露的前序syntax／fixture／exporter race失敗，不改寫舊紀錄。
- 外部agent交付precision40／ROUND_HALF_EVEN canonical cash replay與ledger storage order相容性證據，明示非epsilon容忍、component summaries仍精確核對，真正不一致仍fail-closed。助手未做code review、unit tests或debug，只核對CI證據與實際成果。
- 獨立preview對真實快照HTTP200、observer.available=true、error=null，closed-trade數／statistics sample與research count相符。之前realized_pnl_usdt mismatch不再出現，真實浏览器顯示1/30、平倉列及統計。
- UTC2026-10-03T10:57:52.305238Z備份並套用精確五個observer／dashboard artifacts，只重啟看板，交易runtime未重啟。部署hash等於CI head，帳戶ledger／fills歷史前綴、engine deployment／forward start與research起點／deadline讀回保持。
- Linux及Windows localhost18767實際HTTP200、observer.available=true，/api/work獨立200；真實瀏覽器進度／已平倉／拒單與成本統計可讀到實際資料。只算一筆完整往返，100%勝率不能當優勢證據。
- Issue#7可就已驗證的availability／representation相容回歸結案。Issue#3仍未完整交付跨帳本支援；有完整平倉資料時，長小數／比例值造成頁面橫向溢出，也列入該Issue的可讀性成果，不自行修碼。
- 正常local鏡像發布，不merge遠端main；部署異常依既有備份回滾並交外部工程，不調交易設定或洗掉舊資料。

## EXPORTER-CI-RELIABILITY-010（2026-10-03，發布CI間歇失敗另案追蹤）

- 已發布mirror538fd0e5b2e99650f9ceb044d5efc08440078a5c，五個#9 source artifacts及versions.md均從GitHub讀回符合。先前mirror620f7a30f423fd78d17f7fd48c9414f16c0545c6的CI已通過，不把後續紅燈藏起來。
- 新run37118382554 exporter36項有1error及1failure：test_unknown_descendant_history_is_never_published的maintenance.lock FileNotFoundError，以及test_cli_scanner_policy_cannot_be_overridden_by_export_or_environment的1!=0 assertion。第二失敗是否同根因未確定，不自行debug。#9外部agent亦曾揭露先前Git lock檢查race，passing retry不是root cause修復。
- 新開Issue#10（BUG／ENGINEERING），由外部agent修復CI可靠性，禁止弱化provenance、permission、scanner、divergence／unknown-history安全契約或刪測試。只要求一次unchanged-head GitHub failed-job rerun作有界驗證，保留原fail。無論重跑成功與否，Issue#10不以retry結案。
- #9精確head的36+228 CI與真實帳戶／Windows／Linux／browser成果驗收仍成立。此發布CI問題不等同runtime／帳戶故障，沒有重置帳戶或回退已確認可用的研究指標。Issue#7以已驗證的observer回歸修復結案；發布CI可靠性獨立標queued。
- 同一mirror head的run37118382554 attempt2重跑success，已從GitHub讀回。這只證明本次重跑通過，不證明先前maintenance.lock與scanner assertion缺陷已修復，Issue#10仍open／queued。

## PR-011-ACCEPTED（2026-10-03，同步工具暫態檔案檢查修補）

- 驗收精確heada19052eede0447d6c762b75cbf625c46e5c80a5d。GitHub run37119343446的原始及兩次有界追加jobs111192259083／111192464086／111192662080均success，實際log每次38個exporter＋228個PAPER tests OK。首個post-fix job已綠燈，不拿retry-only green作修復證據。
- Agent交付maintenance.lock在list/stat間消失的確定性合成證據、完整安全檢查最多三次且持續churn明確blocked的回歸，以及scanner-policy real CLI路徑與原安全條件。歷史scanner assertion的原始特定trigger未留完整trace，不宣稱必然與原lock事件同根因，接受其明示未知與獨立trigger證據。
- Scope只含automation/review_sync.py及automation/tests/test_review_sync.py，local映射repo_sync下兩檔。原base bytes與remote完全一致，UTC2026-10-03T11:53:11.272065Z備份後原子套用精確已CI通過artifact，保留原權限與回滾版本。無remote merge，不自行code review／unit test／debug。
- 未修改或重啟交易runtime／看板，不改broker、帳戶、風控、策略及研究窗口。既有同步入口已使用新module，先回報正常settling，發布完成與remote byte readback另作operational驗收，不能把settling當推送成功。
- 有界hosted證據不是永久無故障保證。新的不安全檔案／permission／symlink／provenance／scanner違規仍應fail-closed，不忽略Git entry或放寬安全policy。
- 發布驗收完成：正常private mirror推送70e8e3ff12338944dcb32e88aea58f7d09b52985，remote兩個artifact bytes與精確head相符，版本紀錄已發布；其CI run37121212981 success。PR#11驗收comment及Issue#10 CLOSED均從GitHub讀回。交易source manifest、帳戶fill／ledger歷史前綴與研究起點／deadline維持，HTTP200且observer.available=true。

## PR-012-OPERATIONAL-BLOCKED-ROLLBACK（2026-10-03）

- PR#12精確head06443d22d4c42d4701160630a02f135359fc9e62、run37123222563的GitHub metadata／實際log確認38 exporter＋228 PAPER通過。保留agent早期UI測試失敗說明，不把CI綠燈當真實部署驗收。
- 八個candidate檔經正確export來源映射、base bytes與head hash核對及備份後試套用。docs實際來源為repo_sync/docs/docs，不是profile root docs。monitor真實read-only tick回報runtime_pids=[]及runtime-absent，但獨立/proc確認既有唯一v3程序仍運行。monitor／runtime的lab分属profile root／runtime worktree，是既有不同cwd部署，不移動交易引擎以迎合candidate。
- 真實程序辨識驗收失敗，八檔恢復原byte hashes，新文件移除。交易引擎與看板均未重啟，HTTP200／observer available、交易source manifest／deployment／fill與ledger歷史前綴／研究起點deadline保持。失敗health事件歷史與原證據另保留，不刪除或改成healthy。
- Issue#13 BUG／OBSERVABILITY已建立並讀回，PR#12補修comment已讀回；Issue#4仍open、work task blocked。要求外部agent補split-root exact process驗收與operator文件export，不由operator寫碼、測試或debug。不建立監控排程，候選尚未正式部署。
- storage observation為namespace超過配置budget，不是disk full，已補safe evidence於Issue#5。其餘error-growth／work-overdue屬獨立觀察，不以本次false absent推論真實交易中斷或擅自resolve。
- 證據位於cache/scratch/pr12-06443d2-acceptance，包括metadata、ci.log、manifest、deployment、health_failed、process_evidence及rollback。

## PR-012-UPDATED-ACCEPTED（2026-10-03）

- 精確head b7ac63cab26b7243c4d71a84ddcaf16c75f5ed99、GitHub run37129738079 job111222299413 metadata及log確認38 exporter＋229 PAPER通過，含real split-root subprocess。保留06443d2真實失敗／回滾歷史，不將先前綠CI說成成功部署。
- 所有11個artifact local base完全匹配才備份並套用。Scope新增exporter operator文件allowlist與兩個scheduler wrappers，均屬#13成果所需而非交易規則改動。新增operator配置只存於排除export的shared/health_monitor_config.json。
- 真實one-shot與正式wrapper均辨識唯一既有v3 PID3826480，runtime-absent／duplicate不在目前faults。runtime cwd保持原worktree，交易引擎不重啟。只停止舊dashboard PID3892838並以原port18767/status重啟，persistent handle proc_8ac4b881e1e6。
- Linux及Windows status／health／work三個API HTTP200。真實browser執行確認health區和原1/30研究進度、平倉清單、拒單與cost統計仍可見。交易source manifest、deployment、fills／ledger歷史前綴與research起點deadline讀回保持。
- 已list後create／readback唯讀no_agent監控job e7e8658af9c1，每2分鐘，local輸出，沒有coding-agent喚醒／自主修復。正式wrapper已手動執行驗證，排程真實tick結果另核對後紀錄。WSL／gateway停機不保證持續覆蓋，自啟仍#6。
- operational_healthy=false是已知storage-capacity／work-overdue等真實觀察的呈現，不是本次integration失敗，不能宣稱全部系統健康。歷史incident保留，recovered_monitoring不是root-cause resolved。既有source timing／storage／startup／dashboard剩餘工作仍分別#2/#5/#6/#3，不因本次交付結案。
- Final驗收：private mirror 2fd779e52488599d2c4d30cdc7f598b6e88e8316的11個artifact bytes全部等於精確head，包括operator docs；tree確認私有health config未export。Mirror CI37131842412 success。排程job e7e8658af9c1實際scheduled tick於2026-10-03T15:02:07.315759Z、last_status=ok已讀回。Issue#4/#13 CLOSED及PR acceptance comment已GitHub讀回。證據cache/scratch/pr12-b7ac63c-acceptance。

## PR-014-STORAGE-ACCEPTED（2026-10-03）

- PR#14精確head becba34721dab6bc48a8f9f7f6deed830b8608eb／run37136107271 job111240853088 metadata及真實CI log確認38 exporter＋238 PAPER通過。外部agent覆蓋warning／new risk inhibition／durable protective cancellation與reduce-only exit／reserve exhausted before market delivery／SQLite interruption rollback及同namespace restart前綴，不由operator重跑code tests或review。
- 運行策略／帳戶risk／研究窗口不改。部署前空倉、所有orders已terminal，精確驗證舊PID3826480 cwd／argv才停止。停止後SQLite各namespace原生backup，再套用八個accepted source artifacts，runtime worktree同步v3入口與storage_protection新dependency，hash皆符合head，保留source回滾備份。
- Operator獨立容量判斷：namespace約598319853bytes，2.5164h觀察增長約15916195bytes/h，原窗口剩約24.0581h，線性估計到期981233075bytes。這只是短歷史推估，不是保證。原warning536870912bytes不變，新risk limit1073741824bytes，exit reserve268435456bytes，hard namespace1342177280bytes，disk free reserve2147483648bytes，declared max cycle67108864bytes。Cycle是保守宣告估計，不是已量出所有未來最壞寫入上限，超界會halt。保留警告並新增可執行限制，不以抬高觀察budget抹掉警告。
- Policy只存local shared/storage_policy_v3.json，原namespace原status啟動附--storage-policy，persistent handle proc_ae63aa351fb2，實際Python PID4079131。不reset、遷移、刪除、truncate／compact證據，不補離線單或延長窗口。看板不重啟。
- 真實post snapshot storage_protection=warning／disk_full=false／new_risk_allowed=true，原512MiB警告仍可見且尚未達新增禁止風險門檻。health匹配新唯一runtime PID；API observer available，Linux／Windows status／health／work各200。歷史incident保留，當前work-overdue不冒稱全健康。
- SQLite runtime audit原101215行逐row比對exact prefix保持，SHA256 721ec3aa2f8b951603892235999dd945337880dc6c7a98b8d1d69b837377c466。Snapshot fills／cost ledger前綴、initial equity／risk／deployment、research start／deadline讀回保持，source v3 hash等於accepted head。已snapshot／operational驗收，不宣稱live觸發了protect/halt或實際故障恢復（該部分為agent隔離CI證據）。
- Final：private mirror 38d7a15eba6301f8de6688b582a6b518b6341b18全部八artifact bytes與accepted head匹配，新operator docs已export，local storage policy不在tree。發布CI37137829793 success，PR#14 acceptance comment與Issue#5 CLOSED從GitHub讀回。證據cache/scratch/pr14-becba34-acceptance。

## PR-015-ACTIVATION-BLOCKED（2026-10-03）

- 精確head6090b297fac30bf62b542695b8b7d1f2f896d6f0、run37140145833 job111252700560 metadata／實際log確認38 exporter＋246 PAPER通過。六個artifact local base匹配，試套用後使用operator-local配置跑真實read-only --check。
- 真實preflight輸出already-running，辨識既有唯一runtime PID4079131，H1-PAPER-003、forward start1790889414853、strategy start1790959245744、deadline1791132045744、fills3／ledger10／broker audit7／open0／pending0保持。該preflight不接管既有程序，也未啟動第二個引擎。
- 目前user-systemd bus不存在且systemctl --user無法連線，供應的user-unit activation路徑不能實際驗收。無service install／enable／host reboot／gateway修改，不假稱WSL自啟已完成。
- 同時目前health僅work-overdue導致operational_healthy=false，queued工程逾期不等於交易引擎停機；supervisor冷啟readiness對此情境尚未實際驗證，不冒稱已發生supervisor啟動失敗。要求agent補獨立隔離證據與supported activation／明確platform prerequisite。
- 未啟動supervisor或service、未停止或重啟runtime/dashboard、未停用既有2分鐘health schedule。六個候選source恢復精確base bytes，local試驗startup配置僅保存在private驗收evidence，inactive。原storage policy／帳戶／risk／window與API observer available保持。
- PR#15／Issue#6 blocked comments已GitHub讀回，Issue#6仍OPEN，restart-autostart task blocked。證據cache/scratch/pr15-6090b29-acceptance（CI／metadata／manifest／preflight_readback／service_manager_preflight／health_readiness_boundary／rollback），無operator coding／testing／review／debug。

## PR-015-UPDATED-MANUAL-SUPERVISOR-ACCEPTED（2026-10-03）

- 更新精確head d19430b472b61a37210c6556138643a2c86a5e88，GitHub run37145395026 job111268152346 metadata／實際log確認38 exporter＋249 PAPER通過。保留6090b29 activation blocked歷史；新版明確把work-overdue／work-unavailable列startup advisory，其餘已知及未知fault仍blocking，health原report不改。
- 六個artifact local base完全匹配後備份套用。operator config從先前已驗證paths保留，runtime Python由實際/proc/exe解出，不猜interpreter。配置只存shared/startup_supervisor_config.json，不export。
- 真實read-only --check辨識唯一既有PID4079131／原durable基準，--check-activation回報autostart_supported=false／manual_supervisor_supported=true／user bus unavailable。選擇文檔Path B手動foreground管理，沒有install/enable user unit、host/WSL reboot／linger／gateway修改，也未宣稱Windows會啟動WSL。
- 切換前空倉／無pending，精確驗證舊runtime與dashboard cmd/cwd後停止，保存同namespace audit prefix及snapshot。重新preflight ready-to-start、runtime_pids=[]後啟動手動supervisor，persistent terminal handle proc_588d7526e482。實際supervisor PID4163606直接擁有runtime4163608及dashboard4163607，單一engine、原state/status/config/storage policy保持。
- 真實startup在health僅work-overdue時成功且仍持續，advisory保持顯示，沒有把health改成全healthy。Linux／Windows status／health／work200／observer available，initial equity、risk、deployment、fills／ledger歷史前綴、research start／deadline保持。停止前原audit逐row hash前綴完整保留。
- Supervisor health output驗證後才list／pause精確舊monitor cron e7e8658af9c1，readback disabled/paused。pause後health checked_at由supervisor推進，sole periodic health owner驗收通過。daily brief／mirror sync不變。回滾時須停supervisor owned children並resume原monitor，不能還原舊account副本。
- PR#15的手動管理／恢復契約接受並部署，但Issue#6 user-systemd／host boot autostart仍platform blocked，task仍blocked而非全部completed。沒有讓agent反覆修已知缺失平台，工程下一張#2來源timing。
- Final發布：mirror404ff1cb0d32afc1095897bae212f638d7853332六個artifact bytes相符，operator文件與entrypoint正常export，私有startup配置不在tree；發布CI37147073621 success。PR#15 manual acceptance與Issue#6仍OPEN/platform blocked comments已GitHub讀回。證據cache/scratch/pr15-d19430b-acceptance。

## PR-015-MANAGER-STOP-ROLLBACK／PR-016-TIMING-ACCEPTANCE（2026-10-03）

- PR#16精確head254fa8ffed35e902dbf36f42d69b45dfbd9a1232，run37147855553 job111275394684 metadata/log確認38 exporter＋254 PAPER通過。CI engineering public probe實際3次HTTP451，wait/backoff另列，2s／4sbackoff已解析核對。451是實際HTTP失敗，不叫行情成功，不把約7s outer elapsed叫單次HTTP latency。原OS clock異常根因仍unknown。
- 停止既有supervisor切換時，SIGTERM後15秒bounded wait仍有supervisor/runtime/dashboard，第二次readback三者sleeping。後來正常停止並emit stopped-by-operator，不是永遠停不掉，也未force kill。第一次stop驗收失敗發生在任何PR#16 source改動之前。未查明root cause或精確最終latency，不自行debug。
- 回滾PR#15 process-management layer，改回獨立runtime/dashboard，原namespace/status/config/storage policy保持，舊health cron e7e8658af9c1已list/resume（需readback）。Supervisor inactive，不因先前start成功維持錯誤的stop承諾。Issue#17 BUG／ENGINEERING已建立/readback，Issue#6新增失敗及rollback comment已readback，仍open/platform blocked。
- 八個timing artifact local base一致後備份精確套用，runtime worktree同步v2/perp_collector兩檔（v3 inherited入口不改），source SHA已記manifest。原account/fills/ledger/risk/deployment/research start/deadline保持，audit原114457行逐row prefix hash保留。不補停機交易、延長窗口或重置帳戶。
- Standalone runtime persistent handle proc_a8ef1b741b39，實際Python PID54169；dashboard proc_af32c39a563d。Linux／Windows status/health/work200／observer available，最新runtime error=null。
- 真實durable state source_timing_evidence=32 records，logical_fetch各<=12 internal events，含分離wait與actual HTTP200attempt、batch_peer_aging/source_validation。原clock domains、source timestamp及window baseline保持。Local public /time probe實際HTTP200，wait約250ms、attempt397.77860895264894ms、outer647.9599920511246ms，是host工程觀察非策略樣本，也不解釋歷史clock discrepancy。
- GitHub API曾連續timeout，首次Issue建立結果未知且未宣稱發布/建立成功。有界重查及IPv4公開network probe後連線恢復，確認無同名Issue才成功建立#17/readback，不duplicate。不把此暫態timeout推論工程root cause。
- Issue#2尚待mirror exact bytes／新docs/probe export及發布CI確認才結案。Evidence cache/scratch/pr16-254fa8f-acceptance包含CI、actual public error、live bounded timing、audit/account acceptance、manager stop／rollback與Issue body。

