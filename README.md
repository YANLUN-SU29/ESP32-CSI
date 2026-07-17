# ESP32 Wi-Fi CSI Monitor (ESP32 微波感測與視覺化系統)

## 📌 專案簡介
本專案是一個輕量級的 Wi-Fi CSI (Channel State Information) 擷取與視覺化系統。
透過 ESP32 開發板擷取 2.4GHz Wi-Fi (OFDM 52 條子載波) 的底層實體層訊號，並利用 Python 進行即時的 I/Q 複數解碼、環境去背與 2D 熱力圖視覺化，將無形的微波擾動轉化為肉眼可見的物理特徵。同時具備 CSV 自動錄影功能，可用於後續機器學習模型的資料收集。

## 📁 專案結構
```
ESP32-CSI/
├── data/                ← 錄製的 CSV 資料 (已被 .gitignore 排除)
│   ├── CSI_Empty_Room_...csv
│   ├── CSI_Walking_...csv
│   └── CSI_Waving_...csv
├── firmware.ino         ← ESP32 Arduino 韌體 (TCP 敲門版)
├── monitor.py           ← Python 即時監控與收案程式 (Online)
├── analyzer_gui.py      ← Python 離線數據分析 GUI (Offline)
├── requirements.txt     ← Python 相依套件
├── .gitignore           ← Git 忽略規則
├── README.md            ← 專案說明 (本文件)
└── TODO.md              ← 實驗室收案計畫
```

## 🛠️ 硬體設備與環境
*   **發射端 (Tx):** MERCUSYS N300 (MW302R) 路由器
    *   環境鎖定：Channel 6, 20MHz 頻寬。
    *   硬體逼近：信標間隔 (Beacon Interval) 設為最低 `40ms`，確保高頻率微波廣播。
*   **接收端 (Rx):** ESP32 NodeMCU-32S 開發板
*   **監測端:** Windows PC (Python 環境)

## ✨ 已實現的核心技術
1.  **TCP 敲門激發機制 (TCP Knocking):**
    為了解決 ESP32 底層 CSI 引擎過濾廣播封包的問題，開發板會持續向路由器的 Port 80 發起 TCP 連線，強迫路由器回傳單播 (Unicast) 封包，達成極高頻率且穩定的 CSI 數據觸發，無需依賴外部設備 Ping。
2.  **I/Q 複數解碼至真實振幅:**
    將 ESP32 輸出的原始交錯陣列 `[I1, Q1, I2, Q2...]`，透過畢氏定理 ($Amplitude = \sqrt{I^2 + Q^2}$) 轉換為真實距離振幅，消除未解碼時的馬賽克雜訊。
3.  **動態背景消除演算法:**
    於 Python 端即時運算 `data_matrix - baseline`，消除環境中靜態物體（如牆壁、電腦機殼）的恆定微波反射，乾淨萃取出「人員移動」所產生的多徑效應 (Multipath Fading) 波紋。

## 🚀 使用方法

### 1. ESP32 接收端設定
1. 使用 Arduino IDE 開啟 `firmware.ino`。
2. 將 `ssid` 與 `password` 修改為發射端路由器的設定。
3. 燒錄至 ESP32 後，**務必關閉序列埠監控視窗**。

### 2. Python 監測端設定
1. 安裝所需套件：
   ```bash
   pip install -r requirements.txt
   ```
2. 執行監控程式：
   ```bash
   # 互動模式（程式會問你要錄哪個動作）
   python monitor.py

   # 命令列模式（直接指定標籤）
   python monitor.py Walking
   ```
3. 程式啟動後會自動掃描 COM Port 並連線 ESP32。
4. 錄製的 CSV 資料會自動儲存至 `data/` 資料夾。
5. 關閉視覺化視窗即可結束錄製，終端機會顯示錄製統計。

### 3. 離線數據分析 (事後分析)
錄製完成後，可使用 GUI 分析工具回顧任何 CSV 資料：
```bash
python analyzer_gui.py
```
開啟後點擊「選擇 CSV 檔案」即可載入，程式會自動繪製 4 張圖表：
| 圖表 | 說明 |
|------|------|
| 1. Global Variance | 全域變異數，反映整體移動活躍程度 |
| 2. 2D Spectrogram | 去背熱力圖，視覺化多徑效應波紋 |
| 3. First 5 Subcarriers | 前 5 條子載波細節振幅 |
| 4. All 52 Subcarriers | 全部 52 條子載波巨觀總覽 |

### CSV 資料格式
每個 CSV 檔案包含 53 欄：
| 欄位 | 說明 |
|------|------|
| `Timestamp` | 錄製時間戳 (HH:MM:SS.mmm) |
| `Sub_0` ~ `Sub_51` | 52 條子載波的真實振幅值 |
