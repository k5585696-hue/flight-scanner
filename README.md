# ✈️ 機票雷達

每小時自動查一次 Google Flights 機票價格，結果放在網頁上隨時可以看；
發現比平常便宜的價格，就寄 Email 通知你。要追蹤哪些機票，直接在網頁上像訂票網站一樣填表就好。

**全部跑在 GitHub 的免費雲端上，電腦關機也會照常運作。**

| 功能 | 由誰負責 |
|---|---|
| 每小時自動掃描 | GitHub Actions（免費） |
| 結果網頁、設定航線 | GitHub Pages（免費），網址 `https://你的帳號.github.io/flight-scanner/` |
| 寄信通知 | 你的 Gmail（用「應用程式密碼」） |
| 機票價格 | Google Flights |

---

## 已經上傳過舊版？這樣更新

1. 打開 GitHub 專案首頁 → **Add file → Upload files**
2. 從電腦的 `flight-scanner` 資料夾，把這 4 樣拖進網頁：
   `scanner.py`、`README.md`、`site` 資料夾、`.github` 資料夾
   （**不要**拖 `config.toml`，不然你改過的航線會被蓋掉）
3. 按 **Commit changes**
4. 如果 `.github` 沒傳上去：在 GitHub 上打開 `.github/workflows/scan.yml` → 鉛筆 ✏️ 編輯 →
   全部刪掉，貼上電腦裡同名檔案的內容 → Commit changes
5. 到 **Actions → 機票掃描 → Run workflow** 跑一次，網頁就會變成新版

原本 `config.toml` 裡的航線會自動出現在新網頁上，可以直接用網頁編輯。

---

## 第一次設定（約 15 分鐘）

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
1. 專案上方點 **Actions** 分頁，左邊點 **機票掃描**
   （新建的專案通常不會出現「啟用」按鈕，直接看左邊就好）
2. 右邊 **Run workflow** → 勾選「順便寄一封測試信」→ **Run workflow**
3. 等 1～2 分鐘變成綠色勾勾 ✅
4. 收信（沒看到請找垃圾郵件匣）；打開 `https://你的帳號.github.io/flight-scanner/` 看網頁

完成！之後每小時第 7 分左右會自動掃一次，不用再管它。

---

## 在網頁上設定要追蹤的機票

打開你的機票雷達網頁，按 **＋ 新增追蹤**，像訂票網站一樣填：

- **出發地／目的地**：打中文（例如「東京」）或機場代碼（例如 `TYO`），從清單選。
  城市代碼會一次查該城市所有機場，例如 `TYO` = 成田＋羽田。
- **出發日期**：只查一天就填一格；想比較好幾天，就再填「最晚出發日」，會查這段期間每一天。
- **回程**：選回程日期，或填「住幾晚」（4 晚 = 5 天 4 夜；可以填 `3,4,5` 同時比較）。
- **大人／小孩／嬰兒**：價格是所有人的總價。
- **托運行李（整團總共）**：例如 2 大 1 小、小孩不帶行李，就填 2。
- **艙等、轉機、去程起飛時段**、**低於多少錢一定寄信**：都可以不填。

按 **開始追蹤** 後，GitHub 會馬上查一次價，大約 1～3 分鐘後網頁會自動更新。
每條航線上也有 **✎ 編輯**、**⏸ 暫停**、**🗑 刪除** 按鈕；最上面的 **⏸ 全部暫停** 可以一次停掉全部，之後按 **▶ 恢復掃描** 就會繼續。

### 第一次在網頁上儲存：設定 GitHub 金鑰

網頁要修改你 GitHub 專案裡的設定，需要一把「只能改這個專案」的金鑰。第一次按儲存時會跳出說明視窗：

1. 打開視窗裡的「GitHub 建立金鑰頁面」連結（要先登入 GitHub）
2. **Token name** 隨便取，例如「機票雷達」；**Expiration** 選最長的期限
3. **Repository access** 選 **Only select repositories**，再選 `flight-scanner`
4. **Permissions** 加入 **Contents**，權限選 **Read and write**
5. 按 **Generate token**，複製 `github_pat_` 開頭的那串字，貼回網頁的視窗

金鑰只存在那台裝置的瀏覽器裡（電腦、手機各要設定一次），而且只能改這個專案。
金鑰過期後網頁會再跳出視窗，照同樣步驟重新建立一把就好。

---

## 托運行李怎麼算？

Google Flights **沒有亞洲廉航的行李費資料**，所以程式這樣處理：

- **廉航**（台灣虎航、酷航、樂桃、捷星、亞航、越捷、濟州航空等）：票價 ＋ 估計行李費
  （每件 × 件數 × 去回 2 趟）。例如虎航每件單程約 NT$900，2 件來回就加 NT$3,600。
- **傳統航空**（華航、長榮、星宇、日航、全日空等）：票價本來就含托運行李，不另外加。
- 然後用「含行李的總價」比較哪一班最便宜、要不要寄信。網頁上會分開顯示「票價＋行李估計」。

估計的行李費可以在 `config.toml` 的 `[bag_fees]` 改成航空公司官網的實際價格。
少數傳統航空的「基本票價」也可能不含行李，訂票時請再確認一次。

## 什麼時候會寄信？

- 價格比「平常價」便宜 **15% 以上**（平常價 = 同一個出發日、過去 14 天價格的中位數）
- 或價格低於你設定的「低於多少錢一定寄信」
- 同一個日期寄過之後，要再降 3% 才會再寄，不會每小時一直轟炸
- 剛開始追蹤的前 24 次（約 1 天）在累積平常價，這段時間只有「低於多少錢」會生效
- 如果連續 6 小時都查不到價格（例如 Google 擋住），會寄一封「查詢連續失敗」提醒你

這些數字可以在 `config.toml` 的 `[alert]` 調整。

## 注意事項

- **價格來源**：Google Flights 網頁（非官方 API）。如果 Google 改版導致查詢失敗，網頁上會顯示錯誤，
  通常把 `requirements.txt` 裡 `fast-flights` 的版本更新就能修好。
- **查詢數量**：出發日數 × 住幾晚的選項數，全部航線加起來建議不要超過 50 筆，太多可能被 Google 限制。
- **排程延遲**：GitHub 尖峰時段的排程可能晚 5～30 分鐘，偶爾會跳過一次，屬正常現象。
- **價格紀錄**：存在 `data/history/` 資料夾（每月一個 CSV 檔），可以下載用 Excel 打開。
- **航線存在哪**：網頁上設定的存在 `routes.json`。有了 `routes.json` 之後，`config.toml` 裡的 `[[routes]]` 就不再使用。
- **完全停止**：除了網頁上的「全部暫停」，也可以到 Actions → 機票掃描 → 右上角 **⋯ → Disable workflow**。

## 在自己電腦上試跑（非必要）

```bash
pip install -r requirements.txt
python scanner.py
```

會在 `_site/index.html` 產生網頁（本機預覽不能儲存設定）；沒有設定 Email 的話，通知信會存成 `out/last_email.html` 讓你預覽。
