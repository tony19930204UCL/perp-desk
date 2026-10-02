# 本地影子看板（唯讀）

地址：`http://127.0.0.1:8767/`（在這台 Windows／WSL 電腦的瀏覽器開啟）。不對外開埠，不建公開 tunnel。

顯示來自 shared/status.json 的 100 USDT 影子本金、現金／權益、已實現／未實現 PNL、持倉與成交數、兩個觀察市場、阻擋原因與版本。實際 Binance 錢包餘額未知。
目前尚無訊號引擎或模擬券商，沒有任何交易。水平的 100 USDT 權益紀錄不是策略績效。

啟動與測試：
```sh
cd /home/chihcheng/.hermes/profiles/perp-desk/lab
python -m unittest discover -s tests -v
python -u dashboard.py --port 8767
python -u perp_collector.py --interval 30
python brief.py --status shared/status.json
```

UI 每 5 秒刷新。API 動態計算 snapshot、heartbeat 與 quote source age，超過 60 秒標過期。API 資料缺失或格式錯誤返回 503，介面不填假資金。
只提供 GET / 與 GET /api/status。其他路徑返回 404，寫入方法返回 405，拒绝非 localhost Host、非同源 Origin 及 cross-site fetch。不讀資料庫或憑證，不提供交易控制。

目前程序：dashboard PID 2306053，collector PID 2306069（2026-10-01 啟動當時的識別，不假設未來仍相同）。
可先用 `ps -p 2306053,2306069 -o pid,args` 確認識別再停止。重新開機／WSL 終止後不保證自動恢復，沒有安裝 systemd service。
完整測試證據在 evidence/parent_full_tests.txt，實網與帳本檢查在 evidence/parent_validation.json。

每日簡報已排程，每 24 小時一次。需要 Hermes gateway 與本機在線，不能保證電腦離線時送達。
