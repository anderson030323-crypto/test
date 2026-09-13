# ✈️ 台北 → 峇里島 機票價格監控

每天 **台北時間早上 09:00** 由 GitHub Actions 自動查詢 **TPE → DPS** 來回機票，
比較 **經濟艙、豪華經濟艙、商務艙** 三種艙等的最低價，只要任一艙等價格
**低於上一次查價** 或 **低於歷史最低價**，就寄 Email 到 `anderson030323@gmail.com` 通知你。

## 運作方式

1. `.github/workflows/flight-monitor.yml` 依排程（`0 1 * * *` UTC = 09:00 台北）執行。
2. `monitor/flight_monitor.py` 透過 [Amadeus Flight Offers Search API](https://developers.amadeus.com/self-service/category/flights/api-doc/flight-offers-search)
   查詢預設 **30 / 45 / 60 / 90 天後出發、5 晚來回** 的最低價（可調整）。
3. 每次結果寫入 `data/price_history.json` 並自動 commit，作為歷史紀錄。
4. 比較兩個基準：**上一次查價**（前一天的價格）與 **歷史最低價**。
   任一艙等低於其中一個 → 寄 Email 通知（信中會標示 📉 降價幅度 / 🔥 歷史新低）。
   Email 沒設定或寄送失敗時，改開 GitHub Issue 當備援。
5. 每次執行的完整結果也會顯示在 Actions 的 Job Summary。

## 一次性設定（必要）

### 1. 取得 Amadeus API 金鑰（免費）

1. 到 <https://developers.amadeus.com> 註冊並登入。
2. **My Self-Service Workspace → Create new app**，取得 `API Key` 與 `API Secret`。
3. 到 GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名稱 | 內容 |
| --- | --- |
| `AMADEUS_CLIENT_ID` | Amadeus API Key |
| `AMADEUS_CLIENT_SECRET` | Amadeus API Secret |

> 免費的 **test** 環境每月約 2,000 次呼叫（本專案每天約 12 次），
> 但 test 環境資料為快取、非即時。若要真實報價，請在 Amadeus 申請 production 金鑰後，
> 於 **Variables** 新增 `AMADEUS_ENV = production`。

### 2. 設定 Gmail 寄信（Email 通知）

通知信預設寄到 `anderson030323@gmail.com`，透過 Gmail SMTP 寄出，需要一組「應用程式密碼」：

1. 用要「寄信」的 Gmail 帳號（可以就是 anderson030323@gmail.com）登入 <https://myaccount.google.com/security>，
   確認已開啟 **兩步驗證**。
2. 到 <https://myaccount.google.com/apppasswords>，建立一組應用程式密碼（名稱隨意，例如 `flight-monitor`），
   會得到 16 碼密碼。
3. 到 GitHub repo → **Settings → Secrets and variables → Actions** 新增：

| Secret 名稱 | 內容 |
| --- | --- |
| `SMTP_USER` | 寄信用的 Gmail 地址，例如 `anderson030323@gmail.com` |
| `SMTP_PASSWORD` | 上一步取得的 16 碼應用程式密碼 |

> 若要改收件人，在 **Variables** 新增 `NOTIFY_EMAIL_TO`。
> 若沒設定 Email，通知會改用 GitHub Issue（需 Watch 這個 repo 才會收到 GitHub 的信）。

## 可選：其他通知管道

| 管道 | 需要的 Secrets / Variables |
| --- | --- |
| Telegram | Secrets `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（用 @BotFather 建 bot，再用 @userinfobot 查自己的 chat id） |
| 其他 SMTP 服務 | Variables `SMTP_HOST`、`SMTP_PORT`（預設 `smtp.gmail.com` / `587`） |

## 可選：調整搜尋條件

到 **Settings → Secrets and variables → Actions → Variables** 新增：

| Variable | 預設 | 說明 |
| --- | --- | --- |
| `DAYS_AHEAD` | `30,45,60,90` | 取樣的出發日（距今天數，逗號分隔） |
| `DEPARTURE_DATES` | 空 | 指定出發日期，例如 `2026-12-20,2026-12-27`（設定後會忽略 `DAYS_AHEAD`） |
| `TRIP_NIGHTS` | `5` | 停留晚數；設 `0` 改查單程 |
| `ADULTS` | `1` | 人數 |
| `CURRENCY` | `TWD` | 幣別 |
| `NOTIFY_ALWAYS` | `false` | 設 `true` 則每天都寄當日價格（不只降價時） |
| `ORIGIN` / `DESTINATION` | `TPE` / `DPS` | 可改成其他航線 |

## 手動執行 / 測試

Actions → **Daily TPE→DPS fare monitor → Run workflow**，
勾選 `notify_always` 可立即收到一封完整報價通知信，確認 Email 設定正常。

本機執行：

```bash
pip install -r requirements.txt
export AMADEUS_CLIENT_ID=... AMADEUS_CLIENT_SECRET=...
python monitor/flight_monitor.py
```

## 注意事項

- GitHub 排程有時會延遲數分鐘到數十分鐘，屬正常現象。
- 公開 repo 若 60 天沒有任何 commit，GitHub 會自動停用排程；本專案每天都會 commit 價格紀錄，所以不受影響。
- 「歷史新低」是以本專案開始監控後的紀錄比較，第一次執行時三種艙等都會視為新低並通知一次。
- 「上一次查價」指前一次成功執行的價格；價格持平或上漲時不通知，但仍會記錄在 Job Summary 與歷史檔。
