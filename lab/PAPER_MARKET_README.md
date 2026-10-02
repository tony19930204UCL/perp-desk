# PAPER 公開行情介接（工程已驗證，交易引擎仍在整合）

`paper_market.py` 繼承原公開 GET allowlist，僅增加 `/fapi/v1/fundingRate` 和 `/fapi/v1/time`。沒有私有端點、交易所下單或憑證讀取。

- `instrument(receipt, symbol, category, fees)` 使用 exchangeInfo 的 PRICE_FILTER、LOT_SIZE、MARKET_LOT_SIZE、MIN_NOTIONAL，不用展示精度代替 tick/step。
- `book_event(receipt, symbol, max_source_age_ms)` 驗證實際深度、時間、排序、有限數值和非交叉價格。輸出明示 REST depth snapshot，不冒充逐筆成交。
- `mark_event(...)` 只產生實際 mark 觀察，供 downstream stop 判斷。不將 predicted lastFundingRate 包裝成已付款。
- `finalized_funding(receipt, symbol, forward_start_ms)` 保留實際毫秒 fundingTime、fundingRate、結算 markPrice。只使用 `/fapi/v1/fundingRate`，舊版本之前的結算不算新前瞻樣本。事件 ID 是 symbol 與完整 fundingTime，不四捨五入。
- `probe_inputs(client, symbol, category, fees, max_source_age_ms)` 親跑 metadata/depth/mark/funding 的 GET 與正規化。回應沒有任何 PAPER 資產、成交或績效。

驗證：`python -m unittest discover -s tests -p test_paper_market.py -v`，六項真實單元測試全通過。所有人工 fixture 都在單元測試內，未輸入 runtime 資產。
RED/GREEN 證據：`evidence/tdd_paper_market.txt` 與 `evidence/paper_market_red{3,4,5,6}.txt`、`paper_market_green{3,4,5,6}.txt`。
實網驗證：`evidence/paper_market_live_probe.json` 保存真正公開 GET 的完整 receipts，包含 exchangeInfo 的 ETHUSDT min_notional=20、tick_size=0.01、qty_step=0.001。這是本次觀察，不保證未來 filters 不變。
費率使用者提供，不是已查驗帳戶/VIP 費率。

官方文件三個舊網址目前導到 Binance 新 catalog。直接 HTTP 返回 202 空內容，`web_extract` 又因 search-only backend 無法取頁，沒有假裝已讀到文件。紀錄在 profile/cache/scratch/paper-primary-docs.json。本次 inputs 驗證依據實際 Binance 公開 API 原始回應，不用未讀到的文字作證。

尚未接進模擬券商，尚未初始化正式前瞻引擎。完整交易程序啟動後，須獨立持久化原始行情 receipts、risk/model hash、signal、fills、fees/funding、資產快照；不能用本 probe 的歷史 funding 作 paper 本金扣款。
