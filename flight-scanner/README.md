# ✈️ 機票雷達

每小時自動查一次 Google Flights 機票價格，結果放在網頁上隨時可以看；
發現比平常便宜的價格，就寄 Email 通知你。

**全部跑在 GitHub 的免費雲端上，電腦關機也會照常運作。**

| 功能 | 由誰負責 |
|---|---|
| 每小時自動掃描 | GitHub Actions（免費） |
| 結果網頁 | GitHub Pages（免費），網址 `https://你的帳號.github.io/flight-scanner/` |
| 寄信通知 | 你的 Gmail（用「應用程式密碼」） |
| 機票價格 | Google Flights |

---

## 一次性設定（約 15 分鐘）

### 1. 註冊 GitHub 帳號
到 <https://github.com/signup> 註冊（免費）。已經有帳號就跳過。

### 2. 建立一個 repository（專案）
1. 登入後右上角 **＋ → New repository**
2. Repository name 填 `flight-scanner`
3. 選 **Public**（公開專案的自動排程才是完全免費、不限時數）
4. 按 **Create repository**

> 公開的內容只有：航線設定和價格紀錄。你的 Email 和密碼放在下面第 4 步的「Secrets」，是加密的，別人看不到。

### 3. 上傳程式
1. 在剛建好的 repository 頁面，點 **uploading an existing file** 連結
2. 打開電腦裡的 `flight-scanner` 資料夾，**把裡面所有東西**（包含 `.github` 資料夾）一起拖進網頁
3. 按最下面 **Commit changes**

上傳完，專案裡應該看得到 `.github/workflows/scan.yml` 這個檔案。
如果 `.github` 資料夾沒有傳上去：點 **Add file → Create new file**，檔名打 `.github/workflows/scan.yml`，
內容貼上電腦裡同名檔案的全部內容，再按 Commit changes。

### 4. 設定 Gmail 寄信
1. 到 <https://myaccount.google.com/apppasswords> 建立「應用程式密碼」
   （如果說無法使用，要先到 Google 帳戶 → 安全性 → 開啟「兩步驟驗證」）
2. 名稱隨便打，例如「機票雷達」，會得到一組 **16 個字母的密碼**，先複製起來
3. 回到 GitHub 專案 → **Settings → Secrets and variables → Actions → New repository secret**，新增三個：

| Name | Secret（值） |
|---|---|
| `SMTP_USER` | 你的 Gmail 地址（寄件人） |
| `SMTP_PASSWORD` | 剛剛那組 16 字母應用程式密碼（**不是** Gmail 登入密碼） |
| `MAIL_TO` | 要收通知的信箱（可以跟上面一樣；多個用逗號分隔） |

### 5. 開啟網頁
GitHub 專案 → **Settings → Pages** → Build and deployment 的 **Source** 選 **GitHub Actions**。

### 6. 第一次手動執行（順便測試寄信）
1. 專案上方點 **Actions** 分頁（如果出現提示，按綠色按鈕啟用 workflows）
2. 左邊點 **機票掃描** → 右邊 **Run workflow** → 勾選「順便寄一封測試信」→ **Run workflow**
3. 等 1～2 分鐘變成綠色勾勾 ✅
4. 收信（沒看到請找垃圾郵件匣）；打開 `https://你的帳號.github.io/flight-scanner/` 看網頁

完成！之後每小時第 7 分左右會自動掃一次，不用再管它。

---

## 想改追蹤的航線

在 GitHub 網頁上打開 `config.toml` → 按右上角鉛筆 ✏️ 編輯 → Commit changes。
下一次掃描就會用新的設定。檔案裡有寫每個欄位的意思，例如：

```toml
[[routes]]
name        = "台北 → 首爾 跨年"
from        = "TPE"
to          = "SEL"
trip        = "round-trip"
depart      = ["2026-12-28..2026-12-31"]   # 出發日期區間
stay_days   = [4, 5]                        # 玩 4 天或 5 天都查
adults      = 2
alert_below = 12000                         # 兩人總價低於 12000 一定通知
```

每次掃描的查詢數 = 出發日數 × `stay_days` 個數（上例是 4 × 2 = 8 筆），全部航線加起來建議不要超過 50 筆。

## 什麼時候會寄信？

- 價格比「平常價」便宜 **15% 以上**（平常價 = 同一個出發日、過去 14 天價格的中位數）
- 或價格低於你在 `alert_below` 設定的金額
- 同一個日期寄過之後，要再降 3% 才會再寄，不會每小時一直轟炸
- 剛開始追蹤的前 24 次（約 1 天）在累積平常價，這段時間只有 `alert_below` 會生效
- 如果連續 6 小時都查不到價格（例如 Google 擋住），會寄一封「查詢連續失敗」提醒你

這些數字都可以在 `config.toml` 的 `[alert]` 調整。

## 注意事項

- **價格來源**：Google Flights 網頁（非官方 API）。如果 Google 改版導致查詢失敗，網頁上會顯示錯誤，
  通常把 `requirements.txt` 裡 `fast-flights` 的版本更新就能修好。
- **排程延遲**：GitHub 尖峰時段的排程可能晚 5～30 分鐘，偶爾會跳過一次，屬正常現象。
- **價格紀錄**：存在 `data/history/` 資料夾（每月一個 CSV 檔），可以下載用 Excel 打開。
- **暫停掃描**：Actions → 機票掃描 → 右上角 **⋯ → Disable workflow**。

## 在自己電腦上試跑（非必要）

```bash
pip install -r requirements.txt
python scanner.py
```

會在 `_site/index.html` 產生網頁；沒有設定 Email 的話，通知信會存成 `out/last_email.html` 讓你預覽。
