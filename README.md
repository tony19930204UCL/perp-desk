# Perp Desk (PRIVATE, PAPER only)

這是給人與其他 AI 審查的工程快照，不是可直接啟用實盤的產品。

## 從這裡開始
- [審查入口](docs/AI_REVIEW_GUIDE.md)
- [架構](docs/ARCHITECTURE.md)
- [部署及缺口](docs/DEPLOYMENT_AND_GAPS.md)
- [測試與證據](docs/VERIFICATION.md)
- [自動同步](docs/AUTO_SYNC.md)
- `context/versions.md` 是追加式歷史，不能用歷史成功推論現在健康。
- `source_manifest.json` 逐檔 SHA256 對照匯出內容，不是部署或驗收標記。

## 目錄
`lab/` 現行工程源碼、凍結設定、測試及文檔。
`lab/staging/` 候選改動，**不代表已部署**。
`automation/` 機械同步工具及測試。
`evidence/` 經 allowlist 匯出的可審查證據。
`context/` 專案規範與歷史。舊 SPEC/OPERATING_AGREEMENT 的限制若與較新 AGENTS 不同，先核对時序。

## 安全
沒有 API keys、OAuth token、聊天紀錄、Hermes config、運行 SQLite 或原始帳戶快照。
不要在這個鏡像目錄改 live。GitHub 不會自動部署交易程式。
其他 AI 的建議放 issue／分支，不直接改 `main`，遠端 main 改動會使同步 fail-closed。
