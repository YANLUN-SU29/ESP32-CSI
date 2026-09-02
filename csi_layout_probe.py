"""
CSI 子載波佈局診斷工具（一次性使用，量完即可刪）

目的：monitor.py 目前寫死取 amplitudes[5:57]，但實測韌體送出 384 bytes
（= LLTF + HT-LTF + STBC-HT-LTF2 三個區塊，共 192 條子載波），
只有第一個區塊的一部分被用到，且擷取視窗兩端都切在滿振幅上。

本工具讀取數百筆原始 CSI，統計每一條子載波的平均振幅與零值比例，
標出保護頻帶的實際位置，供決定正確的擷取索引。

用法：python csi_layout_probe.py
"""
import sys
import numpy as np
import serial
import serial.tools.list_ports

SAMPLES = 300
BAUD_RATE = 921600


def pick_port():
    ports = list(serial.tools.list_ports.comports())
    # 只考慮真正的 USB 裝置：主機板內建的 COM1 (ACPI\PNP0501) 沒有 VID/PID
    usb = [p for p in ports if p.vid is not None]
    if not usb:
        print("[錯誤] 找不到任何 USB 序列裝置。ESP32 可能沒接上，或缺 CP2102/CH340 驅動。")
        if ports:
            print("       系統上只有這些非 USB 的埠：")
            for p in ports:
                print(f"         {p.device} - {p.description}")
        sys.exit(1)
    if len(usb) == 1:
        print(f"[自動] 使用 {usb[0].device} ({usb[0].description})")
        return usb[0].device
    for i, p in enumerate(usb):
        print(f"  [{i}] {p.device} - {p.description}")
    while True:
        try:
            idx = int(input("請選擇裝置號碼: ").strip())
            if 0 <= idx < len(usb):
                return usb[idx].device
        except ValueError:
            pass
        print("[錯誤] 請輸入有效號碼。")


def main():
    port = pick_port()
    ser = serial.Serial(port, BAUD_RATE, timeout=2)
    print(f"[讀取] 收集 {SAMPLES} 筆 CSI 中...")

    rows, lengths, errors = [], {}, 0
    while len(rows) < SAMPLES:
        try:
            line = ser.readline().decode('utf-8', errors='strict').strip()
        except UnicodeDecodeError:
            errors += 1
            continue
        if not line.startswith("CSI_V2"):
            continue
        tok = line.split(',')
        try:
            declared = int(tok[1])
            vals = [int(x) for x in tok[4:] if x.strip()]
        except (ValueError, IndexError):
            errors += 1
            continue
        if len(vals) != declared:
            errors += 1
            continue
        lengths[declared] = lengths.get(declared, 0) + 1
        amps = [np.hypot(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
        rows.append(amps)
        if len(rows) % 50 == 0:
            print(f"   ... {len(rows)}/{SAMPLES}")

    ser.close()

    n_sub = min(len(r) for r in rows)
    A = np.array([r[:n_sub] for r in rows])
    mean = A.mean(axis=0)
    zero = (A == 0).mean(axis=0)

    print(f"\n收集完成：{len(rows)} 筆，每筆 {n_sub} 條子載波，解析失敗 {errors} 筆")
    print(f"原始長度分布：{lengths}")

    # ESP32 HT20 在三個 LTF 都啟用時為 384 bytes = 3 個 64 條的區塊
    block = 64
    names = ["LLTF", "HT-LTF", "STBC-HT-LTF2"]

    print("\n" + "=" * 74)
    print("各子載波平均振幅剖面（# 每格約 1.0）")
    print("=" * 74)
    for i in range(n_sub):
        if i % block == 0:
            b = i // block
            label = names[b] if b < len(names) else f"區塊{b}"
            print(f"\n--- 振幅索引 {i}~{min(i+block, n_sub)-1}　{label} "
                  f"(原始位元組 {i*2}~{min(i+block, n_sub)*2-1}) ---")
        tag = ""
        if zero[i] > 0.99:
            tag = "  <= 全零(保護頻帶)"
        elif zero[i] > 0.05:
            tag = f"  <= 有時為零 {zero[i]*100:.0f}%"
        star = " *" if 5 <= i < 57 else "  "   # 標出 monitor.py 目前取的範圍
        print(f"{star}[{i:3d}] {mean[i]:7.2f} {'#' * int(mean[i])}{tag}")

    print("\n" + "=" * 74)
    print("摘要")
    print("=" * 74)
    print("  ( * 記號 = monitor.py 目前實際取用的範圍 amplitudes[5:57] )")
    for b in range(0, n_sub, block):
        seg_m, seg_z = mean[b:b+block], zero[b:b+block]
        nulls = np.where(seg_z > 0.99)[0]
        valid = np.where(seg_z <= 0.99)[0]
        label = names[b // block] if b // block < len(names) else f"區塊{b//block}"
        print(f"\n  {label} (索引 {b}~{b+len(seg_m)-1}):")
        print(f"    全零(保護頻帶)索引 : {[int(x) + b for x in nulls]}")
        print(f"    有效條數           : {len(valid)}")
        print(f"    有效區平均振幅     : {seg_m[valid].mean():.2f}")
        if len(valid):
            print(f"    建議擷取索引       : {int(valid.min()) + b} ~ {int(valid.max()) + b}"
                  f"（扣掉中間保護頻帶）")

    print("\n請把以上輸出貼回對話，用以決定正確的擷取索引。")


if __name__ == "__main__":
    main()
