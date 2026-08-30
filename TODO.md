# 開發路線圖 (TODO)

## ✅ 階段一：標準化資料收集 (已完成)
- [x] 確認物理配置：N300 路由器與 ESP32 距離固定 (約 1.5m)，Channel 6 / 20MHz。
- [x] 錄製 `Empty_10m` (空蕩房間)：純環境底噪，1 筆 10 分鐘。
- [x] 錄製 `Walk_LoS` (視距線走動)：人員於 LoS 來回走動，3 筆各約 2 分鐘。
- [x] 錄製 `Walk_NLoS` (非視距線走動)：人員於 NLoS 走動，3 筆各約 2 分鐘。
- [x] 錄製 `SitStand_1m` (起立坐下)：1 公尺距離內重複起立坐下，3 筆。
- [x] 錄製 `Breath_Center` (正面呼吸)：人員靜坐於 LoS 中央正常呼吸，3 筆。
- [x] 錄製 `Breath_Side` (側面呼吸)：人員靜坐於側邊正常呼吸，3 筆。
- [x] 錄製 `Apnea_Center` (閉氣對照)：人員靜坐閉氣作為對照組，3 筆。

## ✅ 階段二：資料驗證與訊號處理 (已完成)
- [x] 確認 CSV 格式正確 (53 欄: Timestamp + Sub_0~Sub_51)。
- [x] 驗證採樣率穩定 (~60.9 FPS)。
- [x] 實作 PCA 多載波融合 (52 條 → PC1 超級波形)。
- [x] 實作多層訊號清洗管線 (Rolling MAD → Detrend → Butterworth BP)。
- [x] 驗證呼吸偵測：Welch PSD 可抓到 0.15~0.4 Hz 峰值。
- [x] 驗證心跳偵測：0.8~2.0 Hz 帶通濾波可觀察到微弱訊號。
- [x] 優化 analyzer_gui.py：改用實際 Timestamp 換算 fps（取代固定假設值），並於 PCA 融合前剔除全零的 null/guard 子載波。
- [x] 優化 monitor.py：Timestamp 改為微秒精度（減少重複時間戳），並新增即時訊號斷點偵測與警告。

## 🟡 階段三：進階分析與 AI 建模 (進行中)
- [ ] 整合 CWT (連續小波變換) 生成時頻譜圖 (Scalogram)。
- [ ] 評估 EMD / VMD 自適應分解法取代固定頻段濾波。
- [ ] 建立 CNN/LSTM 模型進行動作分類 (Walking / Sitting / Breathing / Empty)。
- [ ] 評估 Transfer Learning 可行性 (跨場景泛化)。

## 🔵 階段四：系統整合 (規劃中)
- [ ] 邊緣運算：將推論模型部署至 ESP32 或樹莓派。
- [ ] 即時警報機制：偵測到跌倒或異常靜止時發送通知。
- [ ] 多設備協同感測：多個 ESP32 覆蓋更大空間。
