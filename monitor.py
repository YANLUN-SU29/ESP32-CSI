import serial
import serial.tools.list_ports
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import csv
import datetime
import os
import sys
import time

# ----------------- [動態標籤輸入] -----------------
VALID_LABELS = ["Empty_Room", "Walking", "Waving"]

if len(sys.argv) > 1:
    ACTION_LABEL = sys.argv[1]
else:
    print("[選擇] 請選擇本次錄製的動作標籤：")
    for i, label in enumerate(VALID_LABELS):
        print(f"  [{i}] {label}")
    print(f"  或直接輸入自訂名稱")
    while True:
        choice = input("\n請輸入編號或名稱: ").strip()
        if choice.isdigit() and 0 <= int(choice) < len(VALID_LABELS):
            ACTION_LABEL = VALID_LABELS[int(choice)]
            break
        elif choice:
            ACTION_LABEL = choice
            break
        print("[錯誤] 輸入不能為空，請重新輸入。")

BAUD_RATE = 921600
WINDOW_SIZE = 100
GAP_WARN_THRESHOLD = 0.5  # 秒；相鄰兩筆樣本間隔超過此值視為訊號斷點

# ----------------- [子載波擷取佈局] -----------------
# 韌體實測送出 384 bytes = 192 條子載波，由三個 64 條的區塊組成：
#   振幅索引   0~ 63  LLTF          （DC 在 0，保護頻帶 28~36）
#   振幅索引  64~127  HT-LTF        （DC 在 64，保護頻帶 93~99）
#   振幅索引 128~191  STBC-HT-LTF2  （DC 在 128，保護頻帶 157~163）
#
# 取捨依據（以 600 筆實測 + 30 筆標註資料驗證）：
#   - 各區塊第 0 條是 DC 離群值（LLTF 為 134.4，區塊中位數僅 17.6），非真實通道，剔除。
#   - STBC-HT-LTF2 與 HT-LTF 的逐條時間序列相關係數中位數 0.902，屬冗餘，
#     納入只會讓 PCA 對重複方向加倍加權，故捨棄。
#   - HT-LTF 與 LLTF 相關性僅 0.06，是獨立的通道量測，值得納入。
#
# 舊版寫死 amplitudes[5:57]，等於存了 9 條恆為零的保護頻帶，
# 又把視窗兩端切在滿振幅上（漏掉 LLTF 的 1~4 與 57~63 共 11 條）。
LLTF_IDX = list(range(1, 28)) + list(range(37, 64))       # 54 條
HTLTF_IDX = list(range(65, 93)) + list(range(100, 128))   # 56 條
SUBCARRIER_IDX = LLTF_IDX + HTLTF_IDX                     # 110 條
SUBCARRIERS = len(SUBCARRIER_IDX)
REQUIRED_AMPS = max(SUBCARRIER_IDX) + 1                   # 需要至少 128 條振幅（即 len=256 以上）
# 欄位名稱直接標出區塊與原始振幅索引，讓資料自我描述
COLUMN_NAMES = [f"L{i}" for i in LLTF_IDX] + [f"H{i}" for i in HTLTF_IDX]

print(f"\n[啟動] Wi-Fi 雷達 (CSV 錄製模式：{ACTION_LABEL} / 動態 COM 版)...")

# ----------------- [動態 COM Port 掃描] -----------------
def get_com_port():
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("[錯誤] 找不到任何 COM Port，請檢查 ESP32 是否已接上 USB。")
        exit()
    if len(ports) == 1:
        print(f"[自動] 鎖定唯一設備：{ports[0].device} ({ports[0].description})")
        return ports[0].device
    print("\n[掃描] 偵測到多個 COM Port，請選擇你的 ESP32：")
    for i, port in enumerate(ports):
        print(f"  [{i}] {port.device} - {port.description}")
    while True:
        try:
            choice = input("\n請輸入設備號碼 (例如 0): ").strip()
            idx = int(choice)
            if 0 <= idx < len(ports):
                selected_port = ports[idx].device
                print(f"[選擇] 已選擇：{selected_port}")
                return selected_port
            else:
                print("[錯誤] 號碼超出範圍，請重新輸入。")
        except ValueError:
            print("[錯誤] 請輸入有效的數字號碼。")

SERIAL_PORT = get_com_port()

# ----------------- [CSV 檔案常駐開啟] -----------------
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

filename = os.path.join(DATA_DIR, f"CSI_{ACTION_LABEL}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
csv_file = open(filename, 'w', newline='')
csv_writer = csv.writer(csv_file)
csv_writer.writerow(['Timestamp'] + COLUMN_NAMES)

print(f"[錄製] CSV 輸出路徑：{filename}")

# ----------------- [初始化矩陣與硬體] -----------------
data_matrix = np.zeros((SUBCARRIERS, WINDOW_SIZE))
record_count = 0
error_count = 0
gap_count = 0
layout_skip = 0       # 因長度不符預期佈局而丟棄的幀數
last_sample_time = None

# 韌體 CSI_V2 格式帶回的中繼資料統計（用於確認子載波佈局與連線品質）
len_stats = {}        # 原始 CSI 位元組數 -> 出現次數
sig_mode_stats = {}   # 0:非HT(11b/g) 1:HT(11n) -> 出現次數
rssi_samples = []
meta_reported = False

try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
except Exception as e:
    print(f"[錯誤] 無法連線至 {SERIAL_PORT}: {e}")
    csv_file.close()
    exit()

# ----------------- [建立雙層視覺化畫布] -----------------
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), gridspec_kw={'height_ratios': [1, 1.5]})
fig.tight_layout(pad=4.0)

ax1.set_title(f"{SUBCARRIERS} Subcarriers True Amplitude "
              f"(LLTF {len(LLTF_IDX)} + HT-LTF {len(HTLTF_IDX)}) — Recording: {ACTION_LABEL}")
ax1.set_xlim(0, WINDOW_SIZE)
ax1.set_ylim(0, 100)
ax1.set_ylabel("Amplitude")
ax1.set_facecolor('#111111')
colors = plt.cm.jet(np.linspace(0, 1, SUBCARRIERS))
lines = []
for i in range(SUBCARRIERS):
    line, = ax1.plot(np.zeros(WINDOW_SIZE), color=colors[i], linewidth=1.5, alpha=0.7)
    lines.append(line)

cax = ax2.imshow(data_matrix, aspect='auto', cmap='jet', vmin=-15, vmax=15)
ax2.set_title("2D CSI Spectrogram (Background Subtracted)")
ax2.set_ylabel(f"Subcarrier (0-{SUBCARRIERS-1}: LLTF then HT-LTF)")
ax2.set_xlabel("Time (Frames)")
fig.colorbar(cax, ax=ax2, label="Amplitude Fluctuation")

# ----------------- [資料更新邏輯] -----------------
def update(frame):
    global data_matrix, record_count, error_count, gap_count, last_sample_time
    global meta_reported, layout_skip
    updated = False

    while ser.in_waiting > 0:
        try:
            raw_data = ser.readline().decode('utf-8').strip()
            if raw_data.startswith("CSI_V2") or raw_data.startswith("CSI_DATA"):
                tokens = raw_data.split(',')

                if tokens[0] == "CSI_V2":
                    # 新格式：CSI_V2,<len>,<rssi>,<sig_mode>,<csi...>
                    declared_len = int(tokens[1])
                    rssi = int(tokens[2])
                    sig_mode = int(tokens[3])
                    csi_numbers = [int(x) for x in tokens[4:] if x.strip() != ""]

                    # 長度與宣告不符代表該行被截斷（序列埠壅塞），整幀丟棄
                    if len(csi_numbers) != declared_len:
                        raise ValueError(
                            f"CSI 長度不符：宣告 {declared_len}，實得 {len(csi_numbers)}")

                    len_stats[declared_len] = len_stats.get(declared_len, 0) + 1
                    sig_mode_stats[sig_mode] = sig_mode_stats.get(sig_mode, 0) + 1
                    rssi_samples.append(rssi)

                    if not meta_reported:
                        meta_reported = True
                        mode_name = "非HT (11b/g)" if sig_mode == 0 else "HT (11n)"
                        print(f"[韌體] CSI_V2 格式 | 原始長度 {declared_len} bytes "
                              f"({declared_len // 2} 條子載波) | RSSI {rssi} dBm | {mode_name}")
                        print(f"[擷取] LLTF {len(LLTF_IDX)} 條 + HT-LTF {len(HTLTF_IDX)} 條 "
                              f"= {SUBCARRIERS} 條（已剔除 DC 與保護頻帶，捨棄冗餘的 STBC 區塊）")
                else:
                    # 舊格式：CSI_DATA,<csi...>
                    csi_numbers = [int(x) for x in tokens[1:] if x.strip() != ""]

                amplitudes = []
                for i in range(0, len(csi_numbers)-1, 2):
                    amp = np.sqrt(csi_numbers[i]**2 + csi_numbers[i+1]**2)
                    amplitudes.append(amp)

                if len(amplitudes) < REQUIRED_AMPS:
                    # 長度不足代表這幀不是預期的三區塊佈局（例如非 HT 封包只有 LLTF），
                    # 若照樣取索引會取到不存在或對應錯誤的子載波，故整幀丟棄。
                    layout_skip += 1
                else:
                    selected = [amplitudes[i] for i in SUBCARRIER_IDX]

                    # 即時偵測訊號斷點（例如 Wi-Fi/TCP 重連導致的資料空窗）
                    now = time.perf_counter()
                    if last_sample_time is not None:
                        gap = now - last_sample_time
                        if gap > GAP_WARN_THRESHOLD:
                            gap_count += 1
                            print(f"[警告] 偵測到訊號斷點：{gap:.2f}s（發生於第 {record_count + 1} 筆之前）")
                    last_sample_time = now

                    # CSV 寫入（常駐 file handle，定期 flush）
                    timestamp = datetime.datetime.now().strftime('%H:%M:%S.%f')
                    csv_writer.writerow([timestamp] + selected)
                    record_count += 1
                    if record_count % 50 == 0:
                        csv_file.flush()

                    # 環形緩衝區更新（避免 hstack 重新分配記憶體）
                    data_matrix = np.roll(data_matrix, -1, axis=1)
                    data_matrix[:, -1] = selected
                    updated = True
        except Exception as e:
            error_count += 1
            if error_count <= 10:
                print(f"[警告] 第 {error_count} 筆解析錯誤: {e}")
            elif error_count == 11:
                print("[警告] 後續錯誤將不再逐筆顯示，結束時統一報告。")

    if updated:
        # 更新上方折線圖
        for i, line in enumerate(lines):
            line.set_ydata(data_matrix[i, :])

        # 動態 Y 軸（根據最近資料自動伸縮）
        current_max = np.max(data_matrix)
        if current_max > 0:
            ax1.set_ylim(0, current_max * 1.2)

        # 更新下方熱力圖（動態去背）
        baseline = np.mean(data_matrix, axis=1, keepdims=True)
        cax.set_array(data_matrix - baseline)

        # 即時計數器顯示於視窗標題
        fig.canvas.manager.set_window_title(
            f"CSI Monitor - {ACTION_LABEL} | 已錄製 {record_count} 筆 | 丟棄 {error_count} 筆 | 斷點 {gap_count} 次"
        )

    return lines + [cax]

ani = animation.FuncAnimation(fig, update, interval=20, blit=False, cache_frame_data=False)

try:
    plt.show()
except KeyboardInterrupt:
    pass
finally:
    csv_file.flush()
    csv_file.close()
    if 'ser' in locals() and ser.is_open:
        ser.close()
    print(f"\n[結束] 觀測結束。")
    print(f"  - 有效錄製：{record_count} 筆")
    print(f"  - 解析丟棄：{error_count} 筆")
    print(f"  - 訊號斷點：{gap_count} 次（單次間隔 > {GAP_WARN_THRESHOLD}s）")
    if layout_skip:
        print(f"  - 佈局不符丟棄：{layout_skip} 筆（振幅數不足 {REQUIRED_AMPS}，"
              f"多為非 HT 封包只含 LLTF）")
    if len_stats:
        total = sum(len_stats.values())
        print(f"  - CSI 原始長度分布：")
        for L, c in sorted(len_stats.items(), key=lambda x: -x[1]):
            print(f"      {L} bytes ({L // 2} 條子載波)：{c} 筆 ({c / total * 100:.1f}%)")
        if len(len_stats) > 1:
            print("      ⚠ 長度不一致代表混入不同型別的封包，同一陣列索引在不同幀")
            print("        會對應到不同的實體子載波，需在韌體端過濾。")
        modes = {0: "非HT(11b/g)", 1: "HT(11n)", 2: "HT40", 3: "其他"}
        print(f"  - 封包型別分布：" + "，".join(
            f"{modes.get(m, m)} {c} 筆" for m, c in sorted(sig_mode_stats.items())))
    if rssi_samples:
        print(f"  - RSSI：平均 {sum(rssi_samples) / len(rssi_samples):.1f} dBm "
              f"(範圍 {min(rssi_samples)} ~ {max(rssi_samples)})")
    print(f"  - 儲存位置：{filename}")
