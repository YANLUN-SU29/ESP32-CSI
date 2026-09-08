"""
批次分析與門檻校準工具

一次跑完 data/ 內所有錄製檔，輸出每檔指標、分組彙總、判別力（AUC）與門檻建議。
直接沿用 analyzer_gui.py 的清理與峰值函式，確保批次結果與 GUI 顯示一致。

用法：
    python batch_analyze.py                # 分析振幅（預設）
    python batch_analyze.py --phase        # 一併分析相位
    python batch_analyze.py --band heart   # 改分析心跳頻段
    python batch_analyze.py --no-cache     # 忽略快取重算

分組規則依檔名前綴（可用 --pattern 調整）：
    CSI_EMPTY*      -> EMPTY（無人對照）
    CSI_HOLD_<pos>* -> HOLD（閉氣對照）
    CSI_GEO_<pos>*  -> GEO（呼吸）
"""
import argparse
import glob
import os
import pickle
import re
import sys
import warnings
from itertools import combinations

import numpy as np
import pandas as pd
from scipy.signal import welch

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyzer_gui import (CSIFeatureViewer, MODES, TOP_K, CONSENSUS_TOL,  # noqa: E402
                          WELCH_WINDOW_SEC, TH_HIGH_PROM, TH_HIGH_CONS)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.batch_cache.pkl')
NFFT = 8192


# --------------------------------------------------------------- 分組 --
def parse_label(name):
    """由檔名解出 (大類, 位置)。位置不存在時回傳 None。"""
    if name.startswith('CSI_EMPTY'):
        return 'EMPTY', None
    m = re.match(r'CSI_(GEO|HOLD)_(P\d+)_', name)
    if m:
        return m.group(1), m.group(2)
    return 'OTHER', None


# ----------------------------------------------------------- 訊號處理 --
def sanitize_phase(phase_mat):
    """相位前處理。

    原始相位在 ±π 之間跳變，且每個封包帶有 CFO/SFO 造成的隨機偏移。
    這裡做兩件事：
      1. 沿時間軸 unwrap，消除 ±π 跳變。
      2. 逐封包對子載波索引做線性擬合並扣除，移除 CFO（相位斜率）與
         SFO（相位截距）。此為文獻常用的最小校正。
    """
    unwrapped = np.unwrap(phase_mat, axis=0)
    idx = np.arange(unwrapped.shape[1], dtype=float)
    idx_c = idx - idx.mean()
    denom = (idx_c ** 2).sum()
    if denom == 0:
        return unwrapped
    # 逐列（逐封包）扣除跨子載波的線性項
    mean_per_row = unwrapped.mean(axis=1, keepdims=True)
    slope = ((unwrapped - mean_per_row) * idx_c).sum(axis=1, keepdims=True) / denom
    return unwrapped - (mean_per_row + slope * idx_c)


def load_file(path, want_phase):
    """讀檔並回傳 (時間軸, fps, 振幅矩陣, 相位矩陣或 None, 欄名)。"""
    df = pd.read_csv(path)
    cols = list(df.columns[1:])
    ts = pd.to_datetime(df.iloc[:, 0], format='%H:%M:%S.%f')
    t = (ts - ts.iloc[0]).dt.total_seconds().values
    fps = (len(t) - 1) / t[-1] if t[-1] > 0 else 60.0

    p_cols = [c for c in cols if str(c).endswith('_P')]
    if p_cols:
        a_cols = [c for c in cols if str(c).endswith('_A')]
    else:
        a_cols = cols
    amp = df[a_cols].values.astype(float)
    pha = None
    if want_phase and p_cols:
        pha = sanitize_phase(df[p_cols].values.astype(float))
    return t, fps, amp, pha, a_cols


def per_subcarrier_psd(mat, fps, helper):
    """逐條子載波清理後計算 Welch PSD。回傳 (頻率軸, PSD 矩陣, 保留的欄索引)。"""
    keep = ~np.all(mat == 0, axis=0)
    idx = np.where(keep)[0]
    raw = mat[:, keep]
    clean = np.empty_like(raw)
    for j in range(raw.shape[1]):
        clean[:, j] = helper._clean(raw[:, j])
    nper = int(min(len(clean), fps * WELCH_WINDOW_SEC))
    freqs, _ = welch(clean[:, 0], fs=fps, nperseg=nper, nfft=NFFT)
    psd = np.empty((len(freqs), clean.shape[1]))
    for j in range(clean.shape[1]):
        _, psd[:, j] = welch(clean[:, j], fs=fps, nperseg=nper, nfft=NFFT)
    return freqs, psd, idx


def band_metrics(freqs, psd, band, helper, col_names=None, subset=None):
    """回傳該頻段的 (最強突出度, 共識度, 共識頻率, 最強欄名)。

    subset: 只考慮這些欄索引（用於 LLTF / HT-LTF 分區比較）。
    """
    n = psd.shape[1]
    sel = np.arange(n) if subset is None else np.asarray(subset)
    if len(sel) < 3:
        return None
    peaks = [helper._band_peak(freqs, psd[:, j], band) for j in sel]
    f = np.array([a for a, _ in peaks])
    pr = np.array([b for _, b in peaks])
    valid = ~np.isnan(pr)
    if valid.sum() < 3:
        return None
    order = sel[valid][np.argsort(-pr[valid])]
    ordered_prom = np.sort(pr[valid])[::-1]
    top = order[:TOP_K]
    top_f = f[valid][np.argsort(-pr[valid])][:TOP_K]
    cons_f = float(np.median(top_f))
    return dict(
        prom=float(ordered_prom[0]),
        med10=float(np.median(ordered_prom[:TOP_K])),
        consensus=float(np.mean(np.abs(top_f - cons_f) < CONSENSUS_TOL)),
        freq=cons_f,
        best_col=(col_names[order[0]] if col_names is not None else int(order[0])),
    )


# --------------------------------------------------------------- 主流程 --
def analyse(paths, band, want_phase, use_cache=True):
    cache = {}
    if use_cache and os.path.exists(CACHE):
        try:
            cache = pickle.load(open(CACHE, 'rb'))
        except Exception:
            cache = {}

    helper = CSIFeatureViewer.__new__(CSIFeatureViewer)
    rows = []
    for path in paths:
        name = os.path.basename(path)
        key = (name, os.path.getmtime(path), want_phase)
        if key in cache:
            rows.append(cache[key])
            print(f"  {name[:38]:38s} (快取)")
            continue

        t, fps, amp, pha, a_cols = load_file(path, want_phase)
        freqs, psd, idx = per_subcarrier_psd(amp, fps, helper)
        names = [a_cols[i] for i in idx]

        # 分區索引（依欄名開頭 L / H）
        lltf = [k for k, nm in enumerate(names) if str(nm).startswith('L')]
        htltf = [k for k, nm in enumerate(names) if str(nm).startswith('H')]

        grp, pos = parse_label(name)
        rec = dict(name=name, grp=grp, pos=pos, fps=fps, dur=t[-1], n=len(t),
                   n_sub=len(names))
        rec['all'] = band_metrics(freqs, psd, band, helper, names)
        rec['lltf'] = band_metrics(freqs, psd, band, helper, names, lltf) if lltf else None
        rec['htltf'] = band_metrics(freqs, psd, band, helper, names, htltf) if htltf else None

        if pha is not None:
            pf, ppsd, pidx = per_subcarrier_psd(pha, fps, helper)
            rec['phase'] = band_metrics(pf, ppsd, band, helper)
        else:
            rec['phase'] = None

        cache[key] = rec
        rows.append(rec)
        print(f"  {name[:38]:38s} {len(names)} 條 / {fps:.1f} fps")

    if use_cache:
        try:
            pickle.dump(cache, open(CACHE, 'wb'))
        except Exception:
            pass
    return rows


def auc(pos, neg):
    """Mann-Whitney U 轉 AUC：1.0 完美分離、0.5 無判別力。"""
    if not len(pos) or not len(neg):
        return float('nan')
    c = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return c / (len(pos) * len(neg))


def best_threshold(pos, neg):
    """找最大化正確率的門檻，回傳 (門檻, 正確率, 命中, 誤報)。"""
    cand = sorted(set(list(pos) + list(neg)))
    best = (float('nan'), -1, 0, 0)
    for th in cand:
        tp = sum(1 for v in pos if v >= th)
        fp = sum(1 for v in neg if v >= th)
        acc = (tp + (len(neg) - fp)) / (len(pos) + len(neg))
        if acc > best[1]:
            best = (th, acc, tp, fp)
    return best


def report(rows, band, want_phase):
    lab = f"{band[0]}~{band[1]} Hz"
    print(f"\n{'=' * 92}\n逐檔結果（頻段 {lab}）\n{'=' * 92}")
    print(f"{'檔案':32s} {'組別':10s} {'fps':>5s} {'突出度':>7s} {'共識':>5s} "
          f"{'推估/分':>8s} {'最強載波':>9s}")
    for r in sorted(rows, key=lambda x: (x['grp'], x['pos'] or '', x['name'])):
        a = r['all']
        if a is None:
            print(f"{r['name'][:32]:32s} 頻段內樣本不足"); continue
        g = r['grp'] + (f"_{r['pos']}" if r['pos'] else '')
        print(f"{r['name'][:32]:32s} {g:10s} {r['fps']:5.1f} {a['prom']:7.1f} "
              f"{a['consensus'] * 100:4.0f}% {a['freq'] * 60:7.1f} {str(a['best_col']):>9s}")

    # ---- 分組彙總 ----
    print(f"\n{'=' * 92}\n分組彙總\n{'=' * 92}")
    groups = {}
    for r in rows:
        key = r['grp'] + (f"_{r['pos']}" if r['pos'] else '')
        groups.setdefault(key, []).append(r)
    print(f"{'組別':12s} {'n':>3s} {'突出度(平均)':>12s} {'共識度':>7s} {'推估呼吸率':>16s}")
    for k in sorted(groups):
        v = [r['all'] for r in groups[k] if r['all']]
        if not v:
            continue
        pr = [x['prom'] for x in v]
        cs = [x['consensus'] for x in v]
        fr = [x['freq'] * 60 for x in v]
        print(f"{k:12s} {len(v):3d} {np.mean(pr):12.1f} {np.mean(cs) * 100:6.0f}% "
              f"{np.mean(fr):8.1f} ±{np.std(fr):4.1f} 次/分")

    # ---- 判別力 ----
    def collect(pred, field='all', metric='prom'):
        return [r[field][metric] for r in rows if r[field] and pred(r)]

    print(f"\n{'=' * 92}\n判別力：呼吸(GEO) vs 對照(EMPTY + HOLD)\n{'=' * 92}")
    for excl_p0 in (False, True):
        def is_geo(r):
            if r['grp'] != 'GEO':
                return False
            return not (excl_p0 and r['pos'] == 'P0')
        is_neg = lambda r: r['grp'] in ('EMPTY', 'HOLD')  # noqa: E731
        tag = '排除 P0' if excl_p0 else '含 P0  '
        print(f"\n  --- {tag} ---")
        print(f"  {'指標':22s} {'AUC':>6s} {'最佳門檻':>9s} {'正確率':>7s} {'命中':>8s} {'誤報':>8s}")
        for field, metric, nm in [('all', 'prom', '振幅 最強子載波'),
                                  ('all', 'med10', '振幅 前10強中位'),
                                  ('all', 'consensus', '振幅 載波共識度'),
                                  ('lltf', 'prom', '　只用 LLTF'),
                                  ('htltf', 'prom', '　只用 HT-LTF')] + \
                                 ([('phase', 'prom', '相位 最強子載波')] if want_phase else []):
            P = collect(is_geo, field, metric)
            N = collect(is_neg, field, metric)
            if not P or not N:
                continue
            a = auc(P, N)
            th, acc, tp, fp = best_threshold(P, N)
            print(f"  {nm:22s} {a:6.3f} {th:9.2f} {acc * 100:6.1f}% "
                  f"{tp:3d}/{len(P):<4d} {fp:3d}/{len(N):<4d}")

    # ---- 現行 GUI 門檻的實際表現 ----
    G = [r for r in rows if r['all'] and r['grp'] == 'GEO']
    Nr = [r for r in rows if r['all'] and r['grp'] in ('EMPTY', 'HOLD')]
    if G and Nr:
        print(f"\n{'=' * 92}\n現行門檻驗證（analyzer_gui.py：突出度 ≥ {TH_HIGH_PROM} "
              f"且 共識度 ≥ {TH_HIGH_CONS * 100:.0f}%，Welch 窗長 {WELCH_WINDOW_SEC}s）\n{'=' * 92}")
        hit = lambda r: (r['all']['prom'] >= TH_HIGH_PROM  # noqa: E731
                         and r['all']['consensus'] >= TH_HIGH_CONS)
        tp = [r for r in G if hit(r)]
        fp = [r for r in Nr if hit(r)]
        print(f"  命中 {len(tp)}/{len(G)}　誤報 {len(fp)}/{len(Nr)}　"
              f"正確率 {(len(tp) + len(Nr) - len(fp)) / (len(G) + len(Nr)) * 100:.1f}%")
        for r in G:
            if not hit(r):
                print(f"  漏判：{r['name'][:38]:38s} 突出度 {r['all']['prom']:5.1f} "
                      f"共識 {r['all']['consensus'] * 100:3.0f}%")
        for r in fp:
            print(f"  誤報：{r['name'][:38]:38s} 突出度 {r['all']['prom']:5.1f} "
                  f"共識 {r['all']['consensus'] * 100:3.0f}%")

        # 安全裕度：對照組中共識達標者的最高分 vs 呼吸組陽性最低分
        neg_pass = [r['all']['prom'] for r in Nr if r['all']['consensus'] >= TH_HIGH_CONS]
        if neg_pass and tp:
            lo, hi = max(neg_pass), min(r['all']['prom'] for r in tp)
            print(f"  安全裕度：對照組共識達標者最高 {lo:.1f}，呼吸組陽性最低 {hi:.1f}"
                  f"{f'，間隔 {hi - lo:.1f}' if hi > lo else '（無間隔，門檻偏險）'}")

    # ---- 留一交叉驗證（誠實估計）----
    if len(G) >= 3 and len(Nr) >= 3:
        print(f"\n{'=' * 92}\n留一交叉驗證（每折用其餘資料重選門檻，避免樂觀偏誤）\n{'=' * 92}")
        allr = G + Nr
        labels = [True] * len(G) + [False] * len(Nr)

        def pick(train, ytr):
            best = None
            for th in np.arange(2, 40, 0.5):
                for cs in (0.7, 0.8, 0.9, 1.0):
                    t_ = sum(1 for r, y in zip(train, ytr)
                             if y and r['all']['prom'] >= th and r['all']['consensus'] >= cs)
                    f_ = sum(1 for r, y in zip(train, ytr)
                             if not y and r['all']['prom'] >= th and r['all']['consensus'] >= cs)
                    key = (f_ == 0, t_, -th)
                    if best is None or key > best[0]:
                        best = (key, th, cs)
            return best[1], best[2]

        tp = fp = fn = tn = 0
        for i in range(len(allr)):
            tr = [r for j, r in enumerate(allr) if j != i]
            ytr = [y for j, y in enumerate(labels) if j != i]
            th, cs = pick(tr, ytr)
            pred = (allr[i]['all']['prom'] >= th and allr[i]['all']['consensus'] >= cs)
            if labels[i] and pred: tp += 1
            elif labels[i]: fn += 1
            elif pred: fp += 1
            else: tn += 1
        acc = (tp + tn) / len(allr)
        print(f"  正確率 {tp + tn}/{len(allr)} = {acc * 100:.1f}%　"
              f"敏感度 {tp / max(1, tp + fn) * 100:.1f}%　特異度 {tn / max(1, tn + fp) * 100:.1f}%")
        print(f"  命中 {tp}/{tp + fn}　漏判 {fn}　誤報 {fp}/{fp + tn}")
        print("  註：此為誠實估計。上方「現行門檻驗證」是用同一批資料選門檻再評估，會偏樂觀。")

    # ---- 門檻掃描建議 ----
    if G and Nr:
        print(f"\n{'=' * 92}\n零誤報門檻掃描\n{'=' * 92}")
        best = []
        for th in np.arange(2, 40, 0.5):
            for cs in (0.7, 0.8, 0.9, 1.0):
                t_ = sum(1 for r in G if r['all']['prom'] >= th and r['all']['consensus'] >= cs)
                f_ = sum(1 for r in Nr if r['all']['prom'] >= th and r['all']['consensus'] >= cs)
                if f_ == 0:
                    best.append((t_, th, cs))
        if best:
            best.sort(key=lambda x: (-x[0], -x[1]))
            seen = set()
            for t_, th, cs in best:
                if (t_, cs) in seen:
                    continue
                seen.add((t_, cs))
                if len(seen) > 6:
                    break
                print(f"  突出度 ≥ {th:4.1f} 且 共識 ≥ {cs * 100:3.0f}%  →  "
                      f"命中 {t_}/{len(G)}、誤報 0/{len(Nr)}")
        else:
            print("  找不到可零誤報的組合")


def main():
    ap = argparse.ArgumentParser(description='CSI 批次分析與門檻校準')
    ap.add_argument('--band', default='breath', choices=list(MODES),
                    help='分析頻段（預設 breath）')
    ap.add_argument('--phase', action='store_true', help='一併分析相位')
    ap.add_argument('--no-cache', action='store_true', help='忽略快取重算')
    ap.add_argument('--dir', default=DATA_DIR, help='資料夾路徑')
    args = ap.parse_args()

    band = MODES[args.band]['band']
    paths = sorted(glob.glob(os.path.join(args.dir, '*.csv')))
    if not paths:
        print(f"[錯誤] {args.dir} 內找不到 CSV")
        return
    print(f"[分析] {len(paths)} 個檔案，頻段 {args.band} {band} Hz"
          f"{'，含相位' if args.phase else ''}")
    rows = analyse(paths, band, args.phase, use_cache=not args.no_cache)
    report(rows, band, args.phase)


if __name__ == '__main__':
    main()
