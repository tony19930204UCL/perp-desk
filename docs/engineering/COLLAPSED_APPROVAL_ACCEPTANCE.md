# Approval transaction repair acceptance contract

Synthetic fixtures are not authenticated-live reproduction.

## 固定環境要求

- 離線 DOM fixture、假 job/active_instruction、可控 clock、假 gh base/blob、假 transport，無任何 real connector/real browser endpoint。
- 使用現有合法工具 scope；不安裝繞過／不更改權限。所有測試 profile/ledger/temp paths 指向隔離目錄，不能指 live profile；資料以 synthetic content 明示，不使用真 secret。
- mock 不能直接宣告 selectors 正確：需要 DOM 驅動執行候選實際 JS，並以 spies 記錄所有 expand/details/allow clicks 及 target identity。除 DOM tests 外，須執行 real helper/caller entrypoint integration。
- 保留 immutable source manifest、test discovery IDs、完整 stdout/stderr、每項 duration/rc/count，以及覆蓋到的 entrypoints。只保存 run output 並不能算所有 case 通過。

## Case matrix

| ID | 情境 | 必要結果 |
|---|---|---|
| C01 | 正確 job/chat，唯一 collapsed 已知「先前活動」，內藏唯一 pending allowlisted GitHub 卡 | 展開 1 次，重新定位並讀 details，驗證 policy/identity，allow-once 1 次 |
| C02 | 正確 chat，卡已展開且唯一 pending | 展開 0 次，allow-once 1 次 |
| C03 | collapsed 活動只有 completed operation，無 pending 卡 | 可以在精確 admission 後展開 1 次；approve=0，NO_PENDING |
| C04 | expanded completed activity 保留舊 GitHub/card 文字 | approve=0，不根據舊文字核准 |
| C05 | 無活動控制、無卡 | expand/details/approve 均 0，NO_PENDING |
| C06 | 多個可見 collapsed 控制或多個 pending GitHub 卡 | fail closed，approve=0；歧義控制不展開 |
| C07 | pending+completed 混合；唯一 pending 可明確綁當前 turn | 不讀舊卡詳情、不點舊 allow，僅同一 pending 卡至多 1 次 |
| C08 | 錯 origin、錯 exact /c/id、root 首頁既有 retained user draft | expand/details/approve 均 0；composer 完全不變 |
| C09 | active instruction 與 job/task 不符、缺 active identity、unknown-send 無對帳證據 | 所有 UI mutation=0，不恢復或派送 |
| C10 | 入場前 expired、invalid/missing/naive valid_until | fail closed；不能默認無限效期 |
| C11 | 展開後／details 後／click 前正好跨 valid_until 邊界 | approve=0，不延長授權 |
| C12 | repo 或 branch 錯，path 超白名單，非 update/create operation | approve=0；diff-clean 限 update，不能藉展開繞 policy |
| C13 | strict 所有既有 sensitive/long-token 非法形式、content 非 str/超長 | approve=0，保留原規則；每一 pattern 有獨立 case |
| C14 | diff-clean added sensitive／non-identifier、empty diff、>MAXL、gh rc非0/空base | approve=0；真錯誤不當作乾淨差異 |
| C15 | strict 拒絕、diff-clean 也拒絕 | approve=0，沒有第三繞道／conversation-wide fallback |
| C16 | 已知 label 但 hidden、disabled、aria-expanded=true 或未知 expanded state | 不盲展開；不使用 startsWith/regex 猜未知 semantic action |
| C17 | 未知 locale、未知 label、sidebar 相似「活動」、plain text 非控制 | 所有非當前活動控制的 click=0；fail closed |
| C18 | 詳情有 0 或 >1 call_tool、malformed JSON、dialog 來源不屬同一卡 | approve=0，不借另一 dialog 補足 |
| C19 | 展開後聊天導航／job變更／active instruction替換 | approve=0，原交易中止，不跟隨新 job |
| C20 | inspect後卡 content/repo/branch/path/invocation 任一變更，即使原卡文案還在 | fingerprint mismatch，approve=0；不能只重查 CARD 字串 |
| C21 | inspect後 DOM node replacement 或 card detach；唯一 allow 屬另一張卡 | approve=0，禁止全頁 button index 重用 |
| C22 | 同一交易 strict→diff fallback 途中另一程序競爭 | 另一程序不能 interleave 展開/details/approve；同一 lock 保持到 readback |
| C23 | auto_allow + relay-poll + carrier/drive + direct helper 同時啟動 | 每條實際入口共享交易 lock；跨程序整體 approve<=1；沒有 nested-lock deadlock |
| C24 | lock busy、transport timeout、空 stdout、JSON/adapter錯誤、JS exception | INFRA 或可驗錯誤，approve=0；不報 NO_CARD／成功；後續沒有盲重試 click |
| C25 | click acknowledgement 不確定／readback 未能證明同一卡完成 | 不重點，結果 UNKNOWN；receipt不能只依 prompt_gone 斷定成功 |
| C26 | 可見相似「允許整段對話」或未知 once suffix | 從不選 conversation-wide；allow-once 精確 action 且屬同一卡才可能 1 次 |
| C27 | 標準 expanded strict 與原 diff-clean 合法小差異 regression | 原 repo/branch/path/content/secret/diff 所有有效與拒絕用例保留 |
| C28 | fixture 有舊 message editor + active composer、有使用者草稿 | composer、send、navigation、授權設定均不被測試或 approver 改動 |

## 除 fixture 外的驗收界線

- 真 live folded-card observation、fixture 成功、candidate integration、deployment acceptance 四者分開列報。fixture 不證明真 ChatGPT label/container/expanded state。
- 本次 live 沒有 pending 卡，因此不以真 GitHub 寫入製造測例，也沒有「等真卡」當作測試成功。
- 可以接受外部工程離線候選，但未經 owner/父明確 scope 不可部署到 live helpers、改 cron、升授權、merge main 或寫交易 DB。
