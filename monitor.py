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

# ----------------- [是否一併記錄相位] -----------------
# ESP32 量到的是複數 CSI，過去只存振幅 sqrt(I²+Q²)，把 atan2(Q,I) 丟掉了，
# 等於捨棄硬體提供的一半資訊。相位對微動偵測通常比振幅靈敏，
# 且這是唯一無法事後補救的一項——錄完才想加就得整批重錄，故預設開啟。
#
# 存的是「原始」相位：原始相位帶有 CFO/SFO 造成的隨機偏移，需跨子載波做
# 線性去趨勢校正才可用。校正屬分析階段的工作，採集階段不應破壞原始資料。
RECORD_PHASE = True

# 欄位名稱直接標出區塊與原始振幅索引，讓資料自我描述。
# 有相位時以 _A / _P 後綴區分，分析端才能只挑振幅欄（否則會把相位當成子載波混進 PCA）。
_BASE_NAMES = [f"L{i}" for i in LLTF_IDX] + [f"H{i}" for i in HTLTF_IDX]
if RECORD_PHASE:
    COLUMN_NAMES = [f"{n}_A" for n in _BASE_NAMES] + [f"{n}_P" for n in _BASE_NAMES]
else:
    COLUMN_NAMES = list(_BASE_NAMES)

print(f"\n[啟動] Wi-Fi 雷達 (CSV 錄製模式：{ACTION_LABEL} / 動態 COM 版)...")

# ----------------- [動態 COM Port 掃描] -----------------
def get_com_port():
    """挑選 ESP32 所在的序列埠。

    只考慮真正的 USB 裝置（有 VID/PID）：主機板內建的傳統序列埠
    （COM1，HWID 形如 ACPI\\PNP0501）沒有 VID/PID，且無法設定到 921600 baud，
    若誤鎖它會在開埠時拋出難以理解的 OSError(22, '參數錯誤')。
    """
    ports = list(serial.tools.list_ports.comports())
    usb_ports = [p for p in ports if p.vid is not None]

    if not usb_ports:
        print("[錯誤] 找不到任何 USB 序列裝置，ESP32 似乎沒有被系統辨識。")
        print("       請檢查：")
        print("         1. USB 線是否支援資料傳輸（純充電線不行）")
        print("         2. 是否已安裝 USB 轉序列埠驅動（NodeMCU-32S 多為 CP2102 或 CH340）")
        print("         3. 裝置管理員的「連接埠」或「其他裝置」有無帶驚嘆號的項目")
        if ports:
            print("       目前系統上只有這些非 USB 的序列埠（都不是 ESP32）：")
            for p in ports:
                print(f"         {p.device} - {p.description}")
        exit()

    if len(usb_ports) == 1:
        p = usb_ports[0]
        print(f"[自動] 鎖定唯一 USB 裝置：{p.device} ({p.description})")
        return p.device

    print("\n[掃描] 偵測到多個 USB 序列裝置，請選擇你的 ESP32：")
    for i, p in enumerate(usb_ports):
        print(f"  [{i}] {p.device} - {p.description}")
    while True:
        try:
            idx = int(input("\n請輸入設備號碼 (例如 0): ").strip())
            if 0 <= idx < len(usb_ports):
                print(f"[選擇] 已選擇：{usb_ports[idx].device}")
                return usb_ports[idx].device
            print("[錯誤] 號碼超出範圍，請重新輸入。")
        except ValueError:
            print("[錯誤] 請輸入有效的數字號碼。")

SERIAL_PORT = get_com_port()

# ----------------- [先連硬體，成功後才建立 CSV] -----------------
# 順序很重要：若先開檔再連線，連線失敗時會留下一堆只有標題列的空 CSV，
# 之後批次分析會踩到這些空檔。
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
except Exception as e:
    print(f"[錯誤] 無法連線至 {SERIAL_PORT}: {e}")
    exit()
print(f"[連線] 已開啟 {SERIAL_PORT} @ {BAUD_RATE} baud")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

filename = os.path.join(DATA_DIR, f"CSI_{ACTION_LABEL}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
csv_file = open(filename, 'w', newline='')
csv_writer = csv.writer(csv_file)
csv_writer.writerow(['Timestamp'] + COLUMN_NAMES)

print(f"[錄製] CSV 輸出路徑：{filename}")

# ----------------- [初始化矩陣] -----------------
data_matrix = np.zeros((SUBCARRIERS, WINDOW_SIZE))
record_count = 0
error_count = 0
gap_count = 0
layout_skip = 0       # 因長度不符預期佈局而丟棄的幀數
consecutive_errors = 0  # 連續失敗次數，用於偵測序列埠斷線
last_sample_time = None

# 丟失率追蹤（需韌體 CSI_V3 以上）
last_seq = None         # 上一筆的事件序號
seq_lost = 0            # 序號跳號累計 = ESP32→PC 之間整行遺失的數量
esp_drop_total = 0      # ESP32 端自報的累計丟棄數（佇列滿 + 長度異常）

# 韌體 CSI_V2 格式帶回的中繼資料統計（用於確認子載波佈局與連線品質）
len_stats = {}        # 原始 CSI 位元組數 -> 出現次數
sig_mode_stats = {}   # 0:非HT(11b/g) 1:HT(11n) -> 出現次數
rssi_samples = []
meta_reported = False

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
    global meta_reported, layout_skip, consecutive_errors
    global last_seq, seq_lost, esp_drop_total
    updated = False

    # 序列埠若被拔掉，in_waiting 會直接拋例外，需在迴圈外攔下
    try:
        pending = ser.in_waiting
    except Exception as e:
        print(f"\n[錯誤] 序列埠連線中斷：{e}")
        print("       請檢查 USB 是否被拔除，關閉視窗後重新執行。")
        plt.close(fig)
        return lines + [cax]

    while pending > 0:
        try:
            raw_data = ser.readline().decode('utf-8').strip()
            if raw_data.startswith(("CSI_V3", "CSI_V2", "CSI_DATA")):
                tokens = raw_data.split(',')
                kind = tokens[0]

                if kind == "CSI_V3":
                    # CSI_V3,<seq>,<len>,<rssi>,<sig_mode>,<dropped>,<csi...>
                    seq = int(tokens[1])
                    declared_len = int(tokens[2])
                    rssi = int(tokens[3])
                    sig_mode = int(tokens[4])
                    esp_dropped = int(tokens[5])
                    csi_numbers = [int(x) for x in tokens[6:] if x.strip() != ""]
                elif kind == "CSI_V2":
                    # CSI_V2,<len>,<rssi>,<sig_mode>,<csi...>
                    seq = esp_dropped = None
                    declared_len = int(tokens[1])
                    rssi = int(tokens[2])
                    sig_mode = int(tokens[3])
                    csi_numbers = [int(x) for x in tokens[4:] if x.strip() != ""]
                else:
                    # 舊格式：CSI_DATA,<csi...>
                    seq = esp_dropped = declared_len = rssi = sig_mode = None
                    csi_numbers = [int(x) for x in tokens[1:] if x.strip() != ""]

                if declared_len is not None:
                    # 長度與宣告不符代表該行被截斷（序列埠壅塞），整幀丟棄
                    if len(csi_numbers) != declared_len:
                        raise ValueError(
                            f"CSI 長度不符：宣告 {declared_len}，實得 {len(csi_numbers)}")
                    len_stats[declared_len] = len_stats.get(declared_len, 0) + 1
                    sig_mode_stats[sig_mode] = sig_mode_stats.get(sig_mode, 0) + 1
                    rssi_samples.append(rssi)

                if seq is not None:
                    # 序號跳號 = 該行在 ESP32 到 PC 之間整行遺失（過去完全偵測不到）
                    if last_seq is not None and seq > last_seq + 1:
                        seq_lost += seq - last_seq - 1
                    last_seq = seq
                    esp_drop_total = esp_dropped

                if not meta_reported and declared_len is not None:
                    meta_reported = True
                    mode_name = "非HT (11b/g)" if sig_mode == 0 else "HT (11n)"
                    print(f"[韌體] {kind} 格式 | 原始長度 {declared_len} bytes "
                          f"({declared_len // 2} 條子載波) | RSSI {rssi} dBm | {mode_name}")
                    print(f"[擷取] LLTF {len(LLTF_IDX)} 條 + HT-LTF {len(HTLTF_IDX)} 條 "
                          f"= {SUBCARRIERS} 條（已剔除 DC 與保護頻帶，捨棄冗餘的 STBC 區塊）")
                    print(f"[記錄] {'振幅 + 相位' if RECORD_PHASE else '僅振幅'}"
                          f"，CSV 共 {len(COLUMN_NAMES) + 1} 欄")
                    if kind != "CSI_V3":
                        print("[提示] 韌體為舊版格式，無法偵測傳輸遺失（建議更新韌體）")

                amplitudes, phases = [], []
                for i in range(0, len(csi_numbers) - 1, 2):
                    I, Q = csi_numbers[i], csi_numbers[i + 1]
                    amplitudes.append(np.sqrt(I * I + Q * Q))
                    if RECORD_PHASE:
                        phases.append(np.arctan2(Q, I))

                if len(amplitudes) < REQUIRED_AMPS:
                    # 長度不足代表這幀不是預期的佈局（例如非 HT 封包只有 LLTF），
                    # 若照樣取索引會取到不存在或對應錯誤的子載波，故整幀丟棄。
                    layout_skip += 1
                else:
                    # 折線圖與熱力圖只顯示振幅，故振幅單獨留一份
                    amp_selected = [amplitudes[i] for i in SUBCARRIER_IDX]
                    row = amp_selected
                    if RECORD_PHASE:
                        row = amp_selected + [phases[i] for i in SUBCARRIER_IDX]

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
                    csv_writer.writerow([timestamp] + row)
                    record_count += 1
                    if record_count % 50 == 0:
                        csv_file.flush()

                    # 環形緩衝區更新（避免 hstack 重新分配記憶體）
                    data_matrix = np.roll(data_matrix, -1, axis=1)
                    data_matrix[:, -1] = amp_selected
                    updated = True
                    consecutive_errors = 0
        except Exception as e:
            error_count += 1
            consecutive_errors += 1
            if error_count <= 10:
                print(f"[警告] 第 {error_count} 筆解析錯誤: {e}")
            elif error_count == 11:
                print("[警告] 後續錯誤將不再逐筆顯示，結束時統一報告。")
            # 連續大量失敗且完全沒有成功解析，多半是序列埠斷線或韌體格式不符，
            # 不再無限重試下去（原本會一直刷錯誤訊息且不會結束）
            if consecutive_errors >= 500:
                print(f"\n[錯誤] 連續 {consecutive_errors} 筆解析失敗且無任何成功樣本。")
                print("       可能原因：USB 被拔除、ESP32 重開機、或韌體輸出格式不符。")
                plt.close(fig)
                return lines + [cax]

        try:
            pending = ser.in_waiting
        except Exception as e:
            print(f"\n[錯誤] 序列埠連線中斷：{e}")
            plt.close(fig)
            return lines + [cax]

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

    # ---- 丟失率（需韌體 CSI_V3 以上）----
    if last_seq is not None:
        produced = last_seq              # ESP32 端產生的事件總數（序號即計數）
        got = record_count + error_count + layout_skip
        print(f"\n  【丟失率分析】")
        print(f"  - ESP32 產生事件：{produced} 筆（序號最大值）")
        print(f"  - ESP32 端丟棄  ：{esp_drop_total} 筆"
              f"（佇列滿=序列埠追不上，或長度異常）")
        print(f"  - 傳輸中遺失    ：{seq_lost} 筆（序號跳號）")
        print(f"  - PC 端收到      ：{got} 筆")
        total_lost = esp_drop_total + seq_lost
        if produced > 0:
            rate = total_lost / (produced + esp_drop_total) * 100
            print(f"  - 總丟失率      ：{total_lost} 筆 ({rate:.2f}%)")
            if rate > 5:
                print("    ⚠ 丟失率偏高。序列埠頻寬是已知瓶頸，可考慮改二進位輸出"
                      "或提高 baud rate。")
            elif total_lost == 0:
                print("    ✓ 無任何丟失")
    else:
        print("\n  【丟失率分析】韌體為舊版格式（無序號），無法計算丟失率")
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
