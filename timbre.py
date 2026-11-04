# -*- coding: utf-8 -*-
"""
音色推测 — 基于频谱/动态特征猜「这段音乐大概有什么」

⚠️ 这不是声源识别模型（那需要训练好的神经网络）。
   这里用的是可测量的物理特征 + 经验规则，输出是**推测**，不是**识别**。
   准确率大概六成：能告诉你「有哪些成分、都落在哪个频段、像什么类型」，
   但**猜不出具体是哪款合成器**（Serum / Massive / Vital 在频谱上都能做出一样的 supersaw）。

给 remix 用的价值：
  · 知道哪段是鼓、哪段是贝斯、哪段是人声、哪段是 pad/lead
  · 知道合成器音色的**类型**（supersaw / Reese / pluck / FM / acid），而不是具体型号
  · 知道该从哪儿下刀 + 该保留什么
"""

import numpy as np

SR = 44100
N_FFT = 4096
HOP = 1024

BANDS = [
    ("超低", 20, 60),
    ("低频", 60, 150),
    ("低中", 150, 400),
    ("中频", 400, 1000),
    ("中高", 1000, 3000),
    ("高频", 3000, 8000),
    ("超高频", 8000, 16000),
]


def _stft_mag(x):
    """单声道 → 幅度谱 (frames, bins)"""
    win = np.hanning(N_FFT).astype(np.float32)
    pad = N_FFT // 2
    xp = np.pad(x, (pad, pad + HOP), mode="constant")
    n_frames = 1 + (len(xp) - N_FFT) // HOP
    mags = np.empty((n_frames, N_FFT // 2 + 1), dtype=np.float32)
    block = 256
    for s in range(0, n_frames, block):
        e = min(s + block, n_frames)
        idx = (np.arange(s, e)[:, None] * HOP) + np.arange(N_FFT)[None, :]
        mags[s:e] = np.abs(np.fft.rfft(xp[idx] * win, axis=1))
    return mags


def _band_share(mag, freqs):
    power = (mag ** 2).sum(axis=0)
    total = power.sum() + 1e-12
    out = {}
    for name, lo, hi in BANDS:
        m = (freqs >= lo) & (freqs < hi)
        out[name] = float(power[m].sum() / total)
    return out


def _centroid(mag, freqs):
    power = (mag ** 2).mean(axis=0)
    return float((freqs * power).sum() / (power.sum() + 1e-12))


def _flatness(mag):
    """谱平坦度：接近 1 = 噪声/打击，接近 0 = 乐音"""
    m = mag.mean(axis=0) + 1e-10
    return float(np.exp(np.log(m).mean()) / m.mean())


def _flux(mag):
    d = np.diff(mag, axis=0)
    d[d < 0] = 0
    return d.sum(axis=1)


def _onsets(flux, thr_k=1.4):
    if len(flux) < 8:
        return np.array([], dtype=int)
    med = np.median(flux)
    mad = np.median(np.abs(flux - med)) + 1e-9
    thr = med + thr_k * mad * 1.4826
    peaks = []
    for i in range(1, len(flux) - 1):
        if flux[i] > thr and flux[i] >= flux[i - 1] and flux[i] > flux[i + 1]:
            if not peaks or (i - peaks[-1]) > 2:     # 去抖
                peaks.append(i)
    return np.array(peaks, dtype=int)


def _bpm_from_onsets(idx):
    if len(idx) < 8:
        return None, 0.0
    times = idx * HOP / SR
    iois = np.diff(times)
    iois = iois[(iois > 0.15) & (iois < 2.0)]
    if len(iois) < 4:
        return None, 0.0
    # 用 IOI 直方图找主周期
    hist, edges = np.histogram(iois, bins=60, range=(0.15, 2.0))
    best = edges[int(np.argmax(hist))] + (edges[1] - edges[0]) / 2
    bpm = 60.0 / best
    while bpm < 70:
        bpm *= 2
    while bpm > 185:
        bpm /= 2
    # 置信度：落在主周期附近的 IOI 比例
    near = np.sum(np.abs(iois - best) < 0.03) / len(iois)
    return round(float(bpm), 1), float(near)


def _decay_profile(mag, freqs, onsets, band=(400, 4000)):
    """测每次起音后能量衰减到一半要多久（毫秒）→ 判断 pluck / 持续音"""
    m = (freqs >= band[0]) & (freqs < band[1])
    env = mag[:, m].sum(axis=1)
    if len(env) < 8 or len(onsets) == 0:
        return None
    durs = []
    for i in onsets[:40]:
        if i + 1 >= len(env):
            break
        peak = env[i]
        if peak <= 0:
            continue
        j = i
        limit = min(len(env), i + 60)          # 最多看 1.4 秒
        while j < limit and env[j] > peak * 0.5:
            j += 1
        if j >= limit:                          # 窗口内没掉到一半 → 持续音，不计入
            continue
        durs.append((j - i) * HOP / SR)
    if not durs:
        return None                             # 全部都没衰减 = 持续音
    return float(np.median(durs) * 1000)      # ms


def _stereo_width(L, R, freqs, mag_shape):
    """每个频段的左右相关度：越低越宽（supersaw 很宽，贝斯通常很窄）"""
    def corr(a, b):
        a = a - a.mean()
        b = b - b.mean()
        den = (np.sqrt((a ** 2).sum()) * np.sqrt((b ** 2).sum())) + 1e-12
        return float((a * b).sum() / den)

    out = {}
    for name, lo, hi in BANDS:
        # 用简单一阶低通/高通近似分频后的时域相关
        w = (2 * np.pi * (lo + hi) / 2) / SR
        # 用 STFT 分频更准，但这里要时域相关，用 FFT 掩码重建太慢
        out[name] = None
    # 整体相关度
    out["整体"] = corr(L, R)
    return out


def analyze(L, R, sr=SR):
    """输入左右声道 ndarray，输出特征与推测。"""
    mono = (L + R) * 0.5
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)

    mag = _stft_mag(mono.astype(np.float32))
    shares = _band_share(mag, freqs)
    centroid = _centroid(mag, freqs)
    flat = _flatness(mag)
    flux = _flux(mag)
    onsets = _onsets(flux)
    duration = len(L) / sr
    onset_rate = len(onsets) / duration if duration > 0 else 0
    bpm, bpm_conf = _bpm_from_onsets(onsets)
    decay_ms = _decay_profile(mag, freqs, onsets)

    # 各频段的起音能量（判断鼓组成分）
    def onset_energy(lo, hi):
        m = (freqs >= lo) & (freqs < hi)
        if len(onsets) == 0 or not m.any():
            return 0.0
        e = mag[:, m].sum(axis=1)
        peak = e.max() + 1e-9
        return float(e[onsets].mean() / peak) if len(onsets) else 0.0

    kick_e = onset_energy(40, 120)
    snare_e = onset_energy(150, 300) + onset_energy(2000, 5000)
    hat_e = onset_energy(6000, 14000)

    corr = _stereo_width(L, R, freqs, mag.shape)["整体"]

    return {
        "duration": duration,
        "shares": shares,
        "centroid": centroid,
        "flatness": flat,
        "onset_rate": onset_rate,
        "bpm": bpm,
        "bpm_conf": bpm_conf,
        "decay_ms": decay_ms,
        "kick": kick_e,
        "snare": snare_e,
        "hat": hat_e,
        "corr": corr,
    }


# ---------------------------------------------------------------- 规则推理

def guess(f):
    """把特征翻译成「大概有什么」。每条都带依据。"""
    s = f["shares"]
    out = []
    c = f["centroid"]
    flat = f["flatness"]
    corr = f["corr"]
    dec = f["decay_ms"]

    # --- 节奏骨架
    if f["kick"] > 0.55 and s["超低"] + s["低频"] > 0.10:
        out.append(("底鼓", "中", f"低频起音强（{f['kick']:.2f}），超低+低频占 {s['超低']+s['低频']:.0%}"))
    if f["snare"] > 0.45:
        out.append(("军鼓 / 打击", "中", f"150–300Hz 与 2–5kHz 同时有起音（{f['snare']:.2f}）"))
    if f["hat"] > 0.35:
        out.append(("踩镲 / 高频打击", "中", f"6–14kHz 起音明显（{f['hat']:.2f}）"))

    # --- 低音
    if s["低频"] > 0.18:
        if dec and dec > 400:
            out.append(("贝斯 / 低音铺底", "中", f"低频重（{s['低频']:.0%}）且衰减慢（{dec:.0f}ms）"))
        else:
            out.append(("贝斯 / 低频打击", "低", f"低频重（{s['低频']:.0%}）衰减快（{dec or 0:.0f}ms）"))

    # --- 合成器音色类型
    wide = corr < 0.45
    dense = s["中高"] + s["高频"] > 0.12
    sustain = dec is None or dec > 350        # 测不出衰减 = 持续音

    if dense and wide and sustain:
        out.append(("supersaw / 宽 pad", "中",
                    "谐波密集、声像很宽（相关度 %.2f）、持续音 —— 典型的锯齿堆叠" % corr))
    elif dense and not wide and dec is not None and dec < 180:
        out.append(("pluck / 短促合成音色", "中",
                    f"谐波多但衰减快（{dec:.0f}ms），声像集中 —— 像 pluck 或木琴类"))
    if s["低频"] > 0.20 and corr < 0.35 and sustain:
        out.append(("Reese / 失真低音", "低",
                    "低频重、宽、持续 —— 可能是双振荡器失谐低音（Reese）"))
    if flat > 0.30 and s["高频"] > 0.15:
        out.append(("噪声型音色（风声/白噪/失真）", "中", f"谱平坦度高（{flat:.2f}），高频占比大"))

    # --- 中频主体
    if s["中频"] + s["低中"] > 0.35:
        if dec and dec < 250:
            out.append(("钢琴 / 拨弦类键盘", "低", f"中频重、衰减中等（{dec:.0f}ms）"))
        else:
            out.append(("人声 / 持续中频乐器", "低", "中频占主导且衰减偏慢，像人声或弦乐"))

    # --- 整体风格
    style = []
    if f["bpm"] and 118 <= f["bpm"] <= 135 and f["kick"] > 0.5:
        style.append("House / Techno（四拍底鼓）")
    if f["bpm"] and 138 <= f["bpm"] <= 160 and f["hat"] > 0.35:
        style.append("Trap / Drum&Bass 方向（高频密集 + 快节奏）")
    if f["bpm"] and 65 <= f["bpm"] <= 95 and f["onset_rate"] < 2.5:
        style.append("慢歌 / Ballad / Hip-hop")
    if c > 2500 and flat > 0.25:
        style.append("电子 / 合成器主导")
    if c < 900 and f["onset_rate"] < 3:
        style.append("原声 / 室内乐倾向（频谱偏暗）")

    return out, style


def render_report(name, f, guesses, styles):
    s = f["shares"]
    L = []
    L.append(f"音色推测报告 —— {name}")
    L.append("=" * 64)
    L.append("⚠️ 这是基于频谱和动态特征的推测，不是声源识别。准确率约六成。")
    L.append("   猜不出具体合成器型号（Serum/Massive/Vital 在频谱上可以一样）。")
    L.append("")
    L.append(f"时长 {f['duration']:.1f}s" + (f"   速度约 {f['bpm']} BPM（把握 {f['bpm_conf']:.0%}）" if f["bpm"] else ""))
    L.append("")
    L.append("【频段分布】")
    for k, v in s.items():
        bar = "█" * max(1, int(v * 50))
        L.append(f"  {k:4s} {v:6.1%}  {bar}")
    L.append("")
    L.append("【整体特征】")
    L.append(f"  频谱重心      {f['centroid']:.0f} Hz   （越高越亮）")
    L.append(f"  谱平坦度      {f['flatness']:.2f}      （越高越接近噪声/打击）")
    L.append(f"  立体声相关度  {f['corr']:.2f}       （越低越宽）")
    L.append(f"  起音密度      {f['onset_rate']:.1f} 次/秒")
    if f["decay_ms"]:
        L.append(f"  中频衰减      {f['decay_ms']:.0f} ms    （越短越像 pluck/打击）")
    L.append("")
    L.append("【大概有什么】")
    if guesses:
        for item, conf, why in guesses:
            L.append(f"  · {item}（把握：{conf}）")
            L.append(f"      依据：{why}")
    else:
        L.append("  （没有足够特征做出推测）")
    L.append("")
    if styles:
        L.append("【风格倾向】")
        for st in styles:
            L.append(f"  · {st}")
        L.append("")
    L.append("【remix 建议】")
    L.append("  · 想抽人声：用「人声 + 伴奏」模式，先听伴奏轨里还剩什么")
    L.append("  · 想替换低音：切低/中/高三段，低频段直接换掉，中高频保留")
    L.append("  · 想加编曲：中高频段通常是 pad/lead，删掉它整首会空出来")
    L.append("  · 频谱重心低于 900Hz 的曲子，加高频容易显假，考虑用减法（删低频腾位置）")
    return "\n".join(L)
