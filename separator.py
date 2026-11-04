# -*- coding: utf-8 -*-
"""
音轨分离器 — 从音频/视频里分离人声与伴奏

功能：
  · 导入音频或视频（视频自动提取音轨）
  · 分离人声 / 伴奏
  · 导出为所有主流格式：MP3 / FLAC / WAV / M4A / ALAC / OGG / OPUS / WMA / AIFF
  · 视频文件可同时导出「无声视频」（音视频分离）
  · 纯 numpy 实现，不需要安装任何机器学习库

可选：装了 Demucs 会自动用它（质量高得多）
      pip install demucs
      程序检测到 demucs 就优先调用，否则用内置算法。

用法：双击「启动.bat」
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_NAME = "音轨分离器"
APP_VER = "2.4"

if getattr(sys, "frozen", False):
    # 打包成 exe 后，__file__ 在 _internal 里；以 exe 所在目录为准
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

PYLIBS = APP_DIR / "pylibs"          # 随程序携带的第三方库（demucs / torch）
BIN_DIR = APP_DIR / "bin"            # 随程序携带的 ffmpeg / ffprobe
PY_RUNTIME = APP_DIR / "runtime" / "python.exe"   # 便携版自带的 Python（跑 demucs 用）

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---------------------------------------------------------------- ffmpeg 定位

def _find_in_winget(name):
    """在 WinGet 的包目录里找（winget 装的 ffmpeg 有时不在 PATH 上）。"""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    root = os.path.join(local, "Microsoft", "WinGet", "Packages")
    if not os.path.isdir(root):
        return None
    for dirpath, _dirnames, filenames in os.walk(root):
        if name in filenames:
            return os.path.join(dirpath, name)
    return None


def find_tool(name):
    """找 ffmpeg / ffprobe：先看程序自带的 bin 目录，再 PATH，再常见目录。"""
    local = BIN_DIR / f"{name}.exe"
    if local.is_file():
        return str(local)
    exe = shutil.which(name)
    if exe:
        return exe
    for cand in (rf"C:\Program Files\ffmpeg\bin\{name}.exe",
                 rf"C:\ffmpeg\bin\{name}.exe"):
        if os.path.isfile(cand):
            return cand
    return _find_in_winget(name + ".exe")


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
FFMPEG_DOWNLOAD = "https://www.gyan.dev/ffmpeg/builds/"

# ---------------------------------------------------------------- 导出格式

FORMATS = {
    "MP3（兼容最好）":       dict(codec="libmp3lame", ext="mp3",  bitrate=True),
    "FLAC（无损）":          dict(codec="flac",       ext="flac", bitrate=False),
    "WAV（24bit 无损）":     dict(codec="pcm_s24le",  ext="wav",  bitrate=False),
    "WAV（16bit）":          dict(codec="pcm_s16le",  ext="wav",  bitrate=False),
    "M4A / AAC（通用）":     dict(codec="aac",        ext="m4a",  bitrate=True),
    "ALAC（无损，苹果）":    dict(codec="alac",       ext="m4a",  bitrate=False),
    "OGG Vorbis":            dict(codec="libvorbis",  ext="ogg",  bitrate=True),
    "OPUS（体积最小）":      dict(codec="libopus",    ext="opus", bitrate=True),
    "WMA":                   dict(codec="wmav2",      ext="wma",  bitrate=True),
    "AIFF（无损）":          dict(codec="pcm_s24be",  ext="aiff", bitrate=False),
}

BITRATES = ["128", "160", "192", "256", "320"]
SAMPLE_RATES = ["保持原样", "22050", "32000", "44100", "48000", "96000"]

MODES = {
    "人声 + 伴奏": ("vocals", "instrumental"),
    "只要人声":   ("vocals",),
    "只要伴奏":   ("instrumental",),
}

STRENGTHS = {"温和": 1.0, "标准": 2.0, "强力": 3.5}

SR_DECODE = 44100          # 解码统一到 44.1k（分离质量足够，导出时再重采样）
HP_HZ = 120                # 人声高通，低频留给伴奏（贝斯/底鼓不该进人声）
N_FFT = 4096
HOP = 1024

AUDIO_EXT = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus",
             ".wma", ".aiff", ".aif", ".ac3", ".ape", ".wv", ".mp2", ".amr",
             ".mka", ".alac", ".dsf", ".dff"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".m4v",
             ".wmv", ".mpg", ".mpeg", ".3gp", ".rmvb", ".rm", ".vob"}

# ---------------------------------------------------------------- 基础工具

def probe(path):
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             creationflags=CREATE_NO_WINDOW, timeout=30)
        if out.returncode != 0:
            return None
        info = json.loads(out.stdout or "{}")
    except Exception:
        return None
    streams = info.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    fmt = info.get("format", {})
    try:
        dur = float(fmt.get("duration") or (audio or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return {
        "duration": dur,
        "has_audio": audio is not None,
        "has_video": video is not None,
        "codec": (audio or {}).get("codec_name", ""),
        "sr": int((audio or {}).get("sample_rate") or 0),
        "channels": int((audio or {}).get("channels") or 0),
        "video_codec": (video or {}).get("codec_name", ""),
    }


def fmt_duration(sec):
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "—"
    if sec <= 0:
        return "—"
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def unique_path(p):
    p = Path(p)
    if not p.exists():
        return p
    for i in range(1, 10000):
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
    return p


def decode_audio(path, sr=SR_DECODE):
    """用 ffmpeg 把任意音视频解成 float32 立体声 ndarray (2, N)。"""
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
           "-i", str(path), "-vn", "-f", "f32le", "-ac", "2", "-ar", str(sr),
           "pipe:1"]
    out = subprocess.run(cmd, capture_output=True,
                         creationflags=CREATE_NO_WINDOW)
    if out.returncode != 0 or not out.stdout:
        return None
    data = np.frombuffer(out.stdout, dtype=np.float32)
    if data.size < 2:
        return None
    data = data[: data.size - (data.size % 2)]
    return data.reshape(-1, 2).T.copy()


def encode_stem(samples, sr, out_path, fmt_label, bitrate="320", rate="保持原样"):
    """把 (2,N) float32 交给 ffmpeg 编码成目标格式。"""
    spec = FORMATS[fmt_label]
    if samples.shape[0] == 1:
        samples = np.vstack([samples, samples])
    interleaved = samples.T.astype(np.float32).ravel()
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-nostdin", "-y",
           "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", "pipe:0",
           "-c:a", spec["codec"]]
    if spec["bitrate"] and bitrate and bitrate != "自动":
        cmd += ["-b:a", f"{bitrate}k"]
    if rate and rate != "保持原样":
        cmd += ["-ar", rate]
    cmd += [str(out_path)]
    proc = subprocess.run(cmd, input=interleaved.tobytes(),
                          capture_output=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-300:] or "ffmpeg 编码失败")
    return out_path


# ---------------------------------------------------------------- 分离算法

def _separate_numpy(mono_center_hint, L, R, strength=2.0, progress=None):
    """
    频域中心提取：
      1) 把 L/R 分解成 mid(M=(L+R)/2) 与 side(D=(L-R)/2)
      2) 每个频点算「中心度」 mask = M²/(M²+D²)，再取 strength 次幂做柔化
      3) 中心成分 = M × mask  → 人声（同理，D 大的是侧向乐器）
      4) 人声去掉 120Hz 以下（低频归伴奏）
      5) 伴奏 = 原始 - 人声（保证两者相加等于原曲）
    返回 (vocals(2,N), instrumental(2,N))
    """
    n = L.shape[0]
    win = np.hanning(N_FFT).astype(np.float32)
    pad = N_FFT // 2
    Lp = np.pad(L, (pad, pad + HOP), mode="constant")
    Rp = np.pad(R, (pad, pad + HOP), mode="constant")
    total = len(Lp)
    n_frames = 1 + (total - N_FFT) // HOP

    out_v = np.zeros(total, dtype=np.float32)
    wsum = np.zeros(total, dtype=np.float32)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SR_DECODE)
    low_cut = freqs < HP_HZ

    block = 256
    for start in range(0, n_frames, block):
        end = min(start + block, n_frames)
        idx = (np.arange(start, end)[:, None] * HOP) + np.arange(N_FFT)[None, :]
        Lb = Lp[idx] * win
        Rb = Rp[idx] * win
        SL = np.fft.rfft(Lb, axis=1)
        SR = np.fft.rfft(Rb, axis=1)
        M = (SL + SR) * 0.5
        D = (SL - SR) * 0.5
        pm = np.abs(M) ** 2
        pd = np.abs(D) ** 2
        mask = (pm / (pm + pd + 1e-10)) ** float(strength)
        mask[:, low_cut] = 0.0
        vb = np.fft.irfft(M * mask, n=N_FFT, axis=1) * win

        # 重叠相加
        for k in range(end - start):
            s = (start + k) * HOP
            out_v[s:s + N_FFT] += vb[k]
            wsum[s:s + N_FFT] += win * win
        if progress:
            progress(min(0.99, end / max(1, n_frames)))

    wsum = np.maximum(wsum, 1e-8)
    v1 = (out_v / wsum)[pad:pad + n]
    vocals = np.vstack([v1, v1])                    # 中心成分是单声道，复制成立体声
    original = np.vstack([L, R])
    inst = original - vocals
    return vocals, inst


def separate(path, mode_names, strength, progress=None):
    """返回 dict: {'vocals': ndarray?, 'instrumental': ndarray?}"""
    data = decode_audio(path)
    if data is None:
        raise RuntimeError("解码失败")
    L, R = data[0], data[1]
    need_v = "vocals" in mode_names
    need_i = "instrumental" in mode_names

    vocals, inst = _separate_numpy(None, L, R, strength, progress)
    out = {}
    if need_v:
        out["vocals"] = vocals
    if need_i:
        out["instrumental"] = inst
    return out


# ---------------------------------------------------------------- 高中低频分离

def _band_weights(freqs, cross):
    """按对数频率做软分频，三个权重加起来恒等于 1（所以三段相加=原曲）。"""
    f = np.maximum(freqs, 1e-6)
    width = 0.18                      # 过渡带宽度（八度）

    def hp(fc):
        return 1.0 / (1.0 + np.exp(-np.log2(f / fc) / width))

    h1 = hp(cross[0])
    h2 = hp(cross[1])
    low = 1.0 - h1
    mid = np.maximum(h1 - h2, 0.0)
    high = h2
    return low.astype(np.float32), mid.astype(np.float32), high.astype(np.float32)


def split_bands(arr, cross=(250.0, 4000.0), progress=None):
    """把 (2,N) 拆成 低 / 中 / 高 三段。"""
    n = arr.shape[1]
    win = np.hanning(N_FFT).astype(np.float32)
    pad = N_FFT                      # 两端各留一个窗长，保证原信号整段都落在"稳态区"
    Lp = np.pad(arr[0], (pad, pad), mode="constant")
    Rp = np.pad(arr[1], (pad, pad), mode="constant")
    total = len(Lp)
    n_frames = 1 + (total - N_FFT) // HOP
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SR_DECODE)
    wl, wm, wh = _band_weights(freqs, cross)

    outs = {k: np.zeros((2, total), dtype=np.float32) for k in ("低", "中", "高")}
    # 汉宁窗 + 75% 重叠时，Σwin² 在稳态区是常数 = (N/HOP)·mean(win²) = 1.5
    wsum_val = float((N_FFT / HOP) * np.mean(win.astype(np.float64) ** 2))
    block = 192
    for s in range(0, n_frames, block):
        e = min(s + block, n_frames)
        idx = (np.arange(s, e)[:, None] * HOP) + np.arange(N_FFT)[None, :]
        WL = np.fft.rfft(Lp[idx] * win, axis=1)
        WR = np.fft.rfft(Rp[idx] * win, axis=1)
        for key, W in (("低", wl), ("中", wm), ("高", wh)):
            bl = np.fft.irfft(WL * W, n=N_FFT, axis=1) * win
            br = np.fft.irfft(WR * W, n=N_FFT, axis=1) * win
            buf = outs[key]
            for j in range(e - s):
                t = (s + j) * HOP
                buf[0, t:t + N_FFT] += bl[j]
                buf[1, t:t + N_FFT] += br[j]
        if progress:
            progress(min(0.99, e / max(1, n_frames)))
    return {k: (v / wsum_val)[:, pad:pad + n] for k, v in outs.items()}


def demucs_available():
    """能不能用 Demucs：需要 pylibs 里有 demucs，并且有一个能跑它的 Python。"""
    if not (PYLIBS / "demucs").is_dir():
        try:
            import demucs  # noqa: F401
            return True
        except Exception:
            return False
    if getattr(sys, "frozen", False) and not PY_RUNTIME.is_file():
        return False          # 打包版没有自带 Python，跑不了 demucs
    return True


def _demucs_python():
    """跑 demucs 用的解释器：便携版用自带 runtime，源码版用当前 Python。"""
    if PY_RUNTIME.is_file():
        return str(PY_RUNTIME)
    return sys.executable


def _demucs_env():
    """把 pylibs 加到 PYTHONPATH，让 demucs 用自己那份 numpy/torch，不污染系统环境。"""
    env = os.environ.copy()
    parts = []
    if (PYLIBS / "demucs").is_dir():
        parts.append(str(PYLIBS))
    old = env.get("PYTHONPATH")
    if old:
        parts.append(old)
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    # 便携版里带着模型时，让 torch 直接用本地缓存（换电脑/断网也能跑）
    cache = APP_DIR / "torchcache"
    if (cache / "hub" / "checkpoints").is_dir():
        env["TORCH_HOME"] = str(cache)
    return env


def separate_demucs(path, tmpdir, progress=None):
    """用 Demucs 分离（质量远高于内置算法）。返回 stems 目录。"""
    outdir = Path(tmpdir) / "demucs"
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [_demucs_python(), "-m", "demucs", "--two-stems=vocals",
           "-o", str(outdir), str(path)]
    if progress:
        progress(0.1)
    proc = subprocess.run(cmd, capture_output=True, env=_demucs_env(),
                          creationflags=CREATE_NO_WINDOW, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "")[-400:] or "Demucs 运行失败")
    vocs = list(outdir.rglob("vocals.wav"))
    if not vocs:
        raise RuntimeError("Demucs 没有输出 vocals.wav")
    if progress:
        progress(0.9)
    return vocs[0].parent


# ---------------------------------------------------------------- 任务

class Job:
    def __init__(self, src):
        self.src = str(src)
        self.status = "等待"
        self.outputs = []
        self.info = None

    @property
    def name(self):
        return Path(self.src).name

    @property
    def kind(self):
        ext = Path(self.src).suffix.lower()
        return "视频" if ext in VIDEO_EXT else ("音频" if ext in AUDIO_EXT else "未知")


class Worker(threading.Thread):
    def __init__(self, jobs, outdir, fmt_label, bitrate, rate, strength_label,
                 silent_video, use_demucs, events,
                 sep_vocals=True, want_full=True, split_bands=True,
                 cross=(250.0, 4000.0)):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.outdir = outdir
        self.fmt_label = fmt_label
        self.bitrate = bitrate
        self.rate = rate
        self.strength = STRENGTHS.get(strength_label, 2.0)
        self.silent_video = silent_video
        self.use_demucs = use_demucs
        self.sep_vocals = sep_vocals
        self.want_full = want_full
        self.split_bands = split_bands
        self.cross = cross
        self.events = events
        self._stop_flag = threading.Event()
        self._proc = None

    def stop(self):
        self._stop_flag.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _export(self, arr, src_sr, base, tag):
        spec = FORMATS[self.fmt_label]
        out = unique_path(Path(self.outdir) / f"{base}_{tag}.{spec['ext']}")
        encode_stem(arr, src_sr, out, self.fmt_label, self.bitrate, self.rate)
        return out

    def run(self):
        try:
            try:
                Path(self.outdir).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            total = len(self.jobs)
            for idx, job in enumerate(self.jobs, 1):
                if self._stop_flag.is_set():
                    job.status = "已停止"
                    self.events.put(("row", job))
                    continue

                self.events.put(("log", f"[{idx}/{total}] {job.name}"))
                job.status = "读取中"
                self.events.put(("row", job))

                info = probe(job.src)
                job.info = info
                if not info:
                    job.status = "失败（无法识别）"
                    self.events.put(("log", "      ✗ ffprobe 读不出来"))
                    self.events.put(("row", job))
                    continue
                if not info.get("has_audio"):
                    job.status = "跳过（没有音轨）"
                    self.events.put(("log", "      ⚠ 文件里没有音频流"))
                    self.events.put(("row", job))
                    continue

                self.events.put(("log",
                    f"      {info['codec'] or '?'} · {info['sr']}Hz · "
                    f"{info['channels']}ch · {fmt_duration(info['duration'])}"
                    + ("  +视频" if info["has_video"] else "")))

                base = Path(job.src).stem

                try:
                    if self.sep_vocals:
                        # ---- 分离人声：产出 vocals + instrumental
                        job.status = "AI 分离中" if self.use_demucs else "分离人声"
                        self.events.put(("row", job))
                        if self.use_demucs:
                            stems_dir = separate_demucs(
                                job.src, self.outdir,
                                lambda p: self.events.put(("progress", p * 70)))
                            v_file = stems_dir / "vocals.wav"
                            i_file = stems_dir / "no_vocals.wav"
                            vocals = decode_audio(v_file) if v_file.exists() else None
                            inst = decode_audio(i_file) if i_file.exists() else None
                            if vocals is None or inst is None:
                                raise RuntimeError("Demucs 没有输出完整的两条轨")
                        else:
                            parts = separate(job.src, ("vocals", "instrumental"),
                                             self.strength,
                                             lambda p: self.events.put(("progress", 10 + p * 70)))
                            vocals = parts["vocals"]
                            inst = parts["instrumental"]

                        out = self._export(vocals, SR_DECODE, base, "人声")
                        job.outputs.append(str(out))
                        self.events.put(("log", f"      ✓ {out.name}"))

                        tag_full, arr_bands = "伴奏", inst
                    else:
                        # ---- 不分离人声：整段就是一个"伴奏/音频"
                        job.status = "解码中"
                        self.events.put(("row", job))
                        data = decode_audio(job.src)
                        if data is None:
                            raise RuntimeError("解码失败")
                        tag_full, arr_bands = "音频", data

                    if self.want_full:
                        out = self._export(arr_bands, SR_DECODE, base, tag_full)
                        job.outputs.append(str(out))
                        self.events.put(("log", f"      ✓ {out.name}"))

                    # 只有伴奏/音频才切频段，人声不切
                    if self.split_bands:
                        job.status = f"切分频段（{tag_full}）"
                        self.events.put(("row", job))
                        for bname, barr in split_bands(arr_bands, self.cross).items():
                            ob = self._export(barr, SR_DECODE, base, f"{tag_full}_{bname}")
                            job.outputs.append(str(ob))
                            self.events.put(("log", f"      ✓ {ob.name}"))
                except Exception as exc:  # noqa: BLE001
                    job.status = "失败"
                    self.events.put(("log", f"      ✗ {exc}"))
                    self.events.put(("row", job))
                    continue

                # 音视频分离：导出无声视频
                if self.silent_video and info.get("has_video"):
                    ext = Path(job.src).suffix.lstrip(".") or "mp4"
                    v_out = unique_path(Path(self.outdir) / f"{base}_无声.{ext}")
                    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-y",
                           "-i", job.src, "-an", "-c:v", "copy", str(v_out)]
                    r = subprocess.run(cmd, capture_output=True,
                                       creationflags=CREATE_NO_WINDOW)
                    if r.returncode == 0 and v_out.exists():
                        job.outputs.append(str(v_out))
                        self.events.put(("log", f"      ✓ {v_out.name}（无声视频）"))
                    else:
                        self.events.put(("log", "      ⚠ 无声视频导出失败（视频流可能不支持直接复制）"))

                job.status = "完成" if job.outputs else "失败"
                self.events.put(("progress", 100))
                self.events.put(("row", job))
        finally:
            self.events.put(("done", None))


# ---------------------------------------------------------------- GUI

def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title(f"{APP_NAME} v{APP_VER} — 人声 / 伴奏 分离")
    root.geometry("1020x700")
    root.minsize(880, 620)

    events = queue.Queue()
    state = {"jobs": [], "worker": None}

    top = ttk.Frame(root, padding=(10, 10, 10, 4))
    top.pack(fill="both", expand=True)

    cols = ("name", "kind", "dur", "fmt", "status")
    tree = ttk.Treeview(top, columns=cols, show="headings", height=11)
    for c, t, w, a in (("name", "文件", 360, "w"), ("kind", "类型", 70, "center"),
                       ("dur", "时长", 80, "center"), ("fmt", "源格式", 140, "w"),
                       ("status", "状态", 200, "w")):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor=a)
    vsb = ttk.Scrollbar(top, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    bar = ttk.Frame(root, padding=(10, 0))
    bar.pack(fill="x")

    def _row(job):
        info = job.info or {}
        f = ""
        if info:
            f = f"{info.get('codec') or ''} {info.get('sr') or ''}Hz"
            if info.get("has_video"):
                f += f" +{info.get('video_codec') or '视频'}"
        return (job.name, job.kind, fmt_duration(info.get("duration", 0)), f, job.status)

    def add_files():
        paths = filedialog.askopenfilenames(
            title="选择音频 / 视频",
            filetypes=[("音频与视频", " ".join(f"*{e}" for e in sorted(AUDIO_EXT | VIDEO_EXT))),
                       ("所有文件", "*.*")])
        for p in paths:
            if any(j.src == str(p) for j in state["jobs"]):
                continue
            job = Job(p)
            state["jobs"].append(job)
            tree.insert("", "end", iid=str(id(job)), values=_row(job))
            root.after(10, lambda j=job: (_probe(j)))

    def _probe(job):
        job.info = probe(job.src)
        try:
            tree.item(str(id(job)), values=_row(job))
        except tk.TclError:
            pass

    def add_folder():
        d = filedialog.askdirectory(title="选择文件夹（含子文件夹）")
        if not d:
            return
        n = 0
        for rootdir, _dirs, files in os.walk(d):
            for f in files:
                if Path(f).suffix.lower() in (AUDIO_EXT | VIDEO_EXT):
                    job = Job(os.path.join(rootdir, f))
                    if any(j.src == job.src for j in state["jobs"]):
                        continue
                    state["jobs"].append(job)
                    tree.insert("", "end", iid=str(id(job)), values=_row(job))
                    n += 1
        log(f"从文件夹添加了 {n} 个文件")

    def remove_sel():
        for iid in tree.selection():
            for j in list(state["jobs"]):
                if str(id(j)) == iid:
                    state["jobs"].remove(j)
            tree.delete(iid)

    def clear_all():
        state["jobs"].clear()
        for iid in tree.get_children():
            tree.delete(iid)

    ttk.Button(bar, text="添加文件", command=add_files).pack(side="left", padx=(0, 6))
    ttk.Button(bar, text="添加文件夹", command=add_folder).pack(side="left", padx=(0, 6))
    ttk.Button(bar, text="移除所选", command=remove_sel).pack(side="left", padx=(0, 6))
    ttk.Button(bar, text="清空", command=clear_all).pack(side="left")

    def analyze_timbre():
        """音色推测：输出一份 *.txt 报告（不是识别，是基于特征的猜测）。"""
        jobs = [j for j in state["jobs"] if str(id(j)) in tree.selection()]
        if not jobs:
            jobs = list(state["jobs"])
        if not jobs:
            messagebox.showinfo("没有文件", "先添加文件再点音色分析。")
            return
        if np is None:
            messagebox.showerror("缺少依赖", "需要 numpy。")
            return

        def work():
            try:
                import timbre
            except Exception as exc:  # noqa: BLE001
                log(f"✗ 载入 timbre 模块失败：{exc}")
                return
            for j in jobs:
                log(f"—— 音色分析：{j.name}")
                data = decode_audio(j.src)
                if data is None:
                    log("      ✗ 解码失败")
                    continue
                try:
                    feats = timbre.analyze(data[0], data[1], SR_DECODE)
                    guesses, styles = timbre.guess(feats)
                    report = timbre.render_report(j.name, feats, guesses, styles)
                except Exception as exc:  # noqa: BLE001
                    log(f"      ✗ 分析失败：{exc}")
                    continue

                base_dir = Path(j.src).parent if same_dir.get() \
                    else Path(out_var.get().strip() or ".")
                try:
                    base_dir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                path = unique_path(base_dir / f"{Path(j.src).stem}_音色分析.txt")
                try:
                    path.write_text(report, encoding="utf-8")
                except OSError as exc:
                    log(f"      ✗ 写报告失败：{exc}")
                    continue
                log(f"      ✓ {path.name}")
                if feats["bpm"]:
                    log(f"        {feats['bpm']} BPM · 重心 {feats['centroid']:.0f}Hz · "
                        f"相关度 {feats['corr']:.2f}")
                for item, conf, _why in guesses[:4]:
                    log(f"        · {item}（把握 {conf}）")
                if styles:
                    log(f"        风格倾向：{' / '.join(styles)}")

        threading.Thread(target=work, daemon=True).start()

    ttk.Button(bar, text="音色分析（推测）", command=analyze_timbre)\
        .pack(side="left", padx=(6, 0))

    # ---- 设置
    box = ttk.LabelFrame(root, text="分离设置", padding=10)
    box.pack(fill="x", padx=10, pady=(8, 4))

    r = 0
    sep_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(box, text="分离人声", variable=sep_var)\
        .grid(row=r, column=0, sticky="w")
    ttk.Label(box, text="（纯音乐 / 纯 BGM 请取消）", foreground="#666")\
        .grid(row=r, column=1, columnspan=2, sticky="w")

    full_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(box, text="输出完整伴奏", variable=full_var)\
        .grid(row=r, column=3, sticky="w")

    bands_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(box, text="伴奏切「低/中/高」（人声不切）", variable=bands_var)\
        .grid(row=r, column=4, columnspan=3, sticky="w")

    r += 1
    ttk.Label(box, text="分离强度：").grid(row=r, column=0, sticky="w", pady=(8, 0))
    str_var = tk.StringVar(value="标准")
    ttk.Combobox(box, textvariable=str_var, values=list(STRENGTHS),
                 state="readonly", width=8).grid(row=r, column=1, sticky="w", pady=(8, 0))

    ttk.Label(box, text="导出格式：").grid(row=r, column=4, sticky="w")
    fmt_var = tk.StringVar(value="MP3（兼容最好）")
    ttk.Combobox(box, textvariable=fmt_var, values=list(FORMATS),
                 state="readonly", width=20).grid(row=r, column=5, sticky="w", padx=(0, 16))

    ttk.Label(box, text="码率：").grid(row=r, column=6, sticky="w")
    br_var = tk.StringVar(value="320")
    ttk.Combobox(box, textvariable=br_var, values=BITRATES,
                 state="readonly", width=7).grid(row=r, column=7, sticky="w")

    r += 1
    ttk.Label(box, text="采样率：").grid(row=r, column=0, sticky="w", pady=(8, 0))
    sr_var = tk.StringVar(value="保持原样")
    ttk.Combobox(box, textvariable=sr_var, values=SAMPLE_RATES,
                 state="readonly", width=12).grid(row=r, column=1, sticky="w", pady=(8, 0))

    silent_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(box, text="视频文件同时导出「无声视频」（音视频分离）",
                    variable=silent_var).grid(row=r, column=2, columnspan=3, sticky="w", pady=(8, 0))

    # 低 / 中 / 高 频段切分（只切伴奏，人声不切）
    r += 1
    ttk.Label(box, text="分频点").grid(row=r, column=0, sticky="w", pady=(8, 0))
    cross1 = tk.StringVar(value="250")
    cross2 = tk.StringVar(value="4000")
    ttk.Entry(box, textvariable=cross1, width=6).grid(row=r, column=1, sticky="w", pady=(8, 0))
    ttk.Label(box, text="Hz  /").grid(row=r, column=2, sticky="w", pady=(8, 0))
    ttk.Entry(box, textvariable=cross2, width=6).grid(row=r, column=3, sticky="w", pady=(8, 0))
    ttk.Label(box, text="Hz").grid(row=r, column=4, sticky="w", pady=(8, 0))
    ttk.Label(box, text="（低于第一个值=低，中间=中，高于第二个=高）",
              foreground="#666").grid(row=r, column=5, columnspan=3, sticky="w", pady=(8, 0))

    r += 1
    ttk.Label(box, text="分离引擎：").grid(row=r, column=0, sticky="w", pady=(8, 0))
    demucs_var = tk.BooleanVar(value=False)
    dm_cb = ttk.Checkbutton(box, text="用 Demucs（AI，质量高但慢）",
                            variable=demucs_var)
    dm_cb.grid(row=r, column=1, columnspan=4, sticky="w", pady=(8, 0))

    r += 1
    ttk.Label(box, text="输出目录：").grid(row=r, column=0, sticky="w", pady=(8, 0))
    out_var = tk.StringVar(value=str(Path.home() / "Music" / "分离输出"))
    ttk.Entry(box, textvariable=out_var, width=58)\
        .grid(row=r, column=1, columnspan=4, sticky="we", pady=(8, 0))

    def pick_out():
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            out_var.set(d)

    ttk.Button(box, text="浏览…", command=pick_out)\
        .grid(row=r, column=5, sticky="w", pady=(8, 0))

    same_dir = tk.BooleanVar(value=False)
    ttk.Checkbutton(box, text="输出到源文件同目录",
                    variable=same_dir).grid(row=r, column=6, columnspan=2, sticky="w", pady=(8, 0))

    # ---- 执行
    run_bar = ttk.Frame(root, padding=(10, 6))
    run_bar.pack(fill="x")
    prog = ttk.Progressbar(run_bar, mode="determinate", maximum=100)
    prog.pack(side="left", fill="x", expand=True, padx=(0, 10))
    btn_start = ttk.Button(run_bar, text="开始分离", width=14)
    btn_start.pack(side="left", padx=(0, 6))
    btn_stop = ttk.Button(run_bar, text="停止", width=8, state="disabled")
    btn_stop.pack(side="left")

    logbox = ttk.LabelFrame(root, text="日志", padding=6)
    logbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    log_txt = tk.Text(logbox, height=9, wrap="none", font=("Consolas", 9))
    lvsb = ttk.Scrollbar(logbox, orient="vertical", command=log_txt.yview)
    log_txt.configure(yscrollcommand=lvsb.set)
    log_txt.pack(side="left", fill="both", expand=True)
    lvsb.pack(side="right", fill="y")

    def log(msg):
        log_txt.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        log_txt.see("end")

    # 自检
    has_demucs = demucs_available()
    if not FFMPEG:
        log("✗ 没有找到 ffmpeg，无法解码/编码")
    if np is None:
        log("✗ 缺少 numpy，无法分离")
    log(f"{APP_NAME} v{APP_VER} 就绪")
    if FFMPEG:
        log(f"ffmpeg：{FFMPEG}")
    else:
        log("✗ 没找到 ffmpeg / ffprobe —— 程序无法解码音频。")
        log(f"  下载：{FFMPEG_DOWNLOAD}")
        log("  解压后把 ffmpeg.exe 和 ffprobe.exe 放进本程序的 bin\\ 目录，"
            "或加进系统 PATH，然后重启本程序。")
    if has_demucs:
        log("检测到 Demucs —— 勾选后可用 AI 分离（质量高得多）")
    else:
        dm_cb.configure(state="disabled",
                        text="用 Demucs（未安装，当前用内置算法）")
        log("Demucs 未安装 —— 所以这个框是灰的，不影响其它功能。")
        log("  想用 AI 分离：pip install --target pylibs demucs，然后重启。")
    log(f"程序目录：{APP_DIR}")
    log("内置算法说明：分离的是「居中的人声」，混响重或双声道的曲子效果会差一些。")

    def poll():
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "log":
                    log(payload)
                elif kind == "progress":
                    prog["value"] = payload
                elif kind == "row":
                    try:
                        tree.item(str(id(payload)), values=_row(payload))
                    except tk.TclError:
                        pass
                elif kind == "done":
                    btn_start["state"] = "normal"
                    btn_stop["state"] = "disabled"
                    prog["value"] = 100
                    n_ok = sum(1 for j in state["jobs"] if j.status == "完成")
                    log(f"—— 结束：成功 {n_ok} / 共 {len(state['jobs'])}")
        except queue.Empty:
            pass
        root.after(120, poll)

    def start():
        if state["worker"] and state["worker"].is_alive():
            return
        jobs = list(state["jobs"])
        if not jobs:
            messagebox.showinfo("没有文件", "先添加要分离的音频或视频。")
            return
        if not FFMPEG or np is None:
            if not FFMPEG:
                if messagebox.askyesno(
                        "缺少 ffmpeg",
                        "找不到 ffmpeg.exe / ffprobe.exe。\n\n"
                        "程序需要它来解码和编码音频。\n\n"
                        f"下载地址：{FFMPEG_DOWNLOAD}\n\n"
                        "解压后把 ffmpeg.exe、ffprobe.exe 放进本程序的 bin\\ 目录，"
                        "或加入系统 PATH，然后重启本程序。\n\n"
                        "现在要打开下载页吗？"):
                    try:
                        import webbrowser
                        webbrowser.open(FFMPEG_DOWNLOAD)
                    except Exception:
                        pass
            else:
                messagebox.showerror("缺少 numpy", "需要 numpy 才能做分离：pip install numpy")
            return

        if same_dir.get():
            groups = {}
            for j in jobs:
                groups.setdefault(str(Path(j.src).parent), []).append(j)
        else:
            base = out_var.get().strip()
            if not base:
                messagebox.showinfo("没有输出目录", "请选择输出文件夹。")
                return
            os.makedirs(base, exist_ok=True)
            groups = {base: jobs}

        for j in jobs:
            j.status = "等待"
            j.outputs = []
            try:
                tree.item(str(id(j)), values=_row(j))
            except tk.TclError:
                pass

        btn_start["state"] = "disabled"
        btn_stop["state"] = "normal"
        prog["value"] = 0
        log("=" * 60)
        opts = ["分离人声" if sep_var.get() else "不分离人声（整段当伴奏）"]
        if full_var.get():
            opts.append("完整伴奏")
        if bands_var.get():
            opts.append("伴奏低/中/高")
        log(f"开始：{len(jobs)} 个文件 · {' + '.join(opts)} · 导出 {fmt_var.get()}")

        pending = list(groups.items())
        state["pending"] = pending

        def next_group():
            if not pending:
                events.put(("done", None))
                return
            outdir, jl = pending.pop(0)
            try:
                c1 = float(cross1.get())
                c2 = float(cross2.get())
            except ValueError:
                c1, c2 = 250.0, 4000.0
            if not (20 < c1 < c2 < 20000):
                c1, c2 = 250.0, 4000.0
            w = Worker(jl, outdir, fmt_var.get(), br_var.get(), sr_var.get(),
                       str_var.get(), silent_var.get(),
                       bool(demucs_var.get() and has_demucs), events,
                       sep_vocals=bool(sep_var.get()),
                       want_full=bool(full_var.get()),
                       split_bands=bool(bands_var.get()),
                       cross=(c1, c2))
            state["worker"] = w
            w.start()

            def watch():
                w.join()
                next_group()

            threading.Thread(target=watch, daemon=True).start()

        next_group()

    def stop():
        if state["worker"]:
            state["worker"].stop()
            log("正在停止…")

    btn_start.configure(command=start)
    btn_stop.configure(command=stop)

    def on_double(_e):
        sel = tree.selection()
        if not sel:
            return
        for j in state["jobs"]:
            if str(id(j)) == sel[0] and j.outputs:
                os.startfile(str(Path(j.outputs[0]).parent))  # noqa: S606
                return
        messagebox.showinfo("还没有输出", "这个文件还没处理完。")

    tree.bind("<Double-1>", on_double)
    root.after(120, poll)
    root.mainloop()


def run_selftest():
    """环境自检：把结果写到程序目录下的 自检结果.txt（用于换电脑/U盘时确认能不能跑）。"""
    lines = [f"{APP_NAME} v{APP_VER} 环境自检",
             f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"程序目录：{APP_DIR}",
             f"打包运行：{'是' if getattr(sys, 'frozen', False) else '否（源码模式）'}",
             ""]
    lines.append(f"numpy   ：{'OK ' + np.__version__ if np is not None else '✗ 缺失'}")
    lines.append(f"ffmpeg  ：{FFMPEG or '✗ 未找到'}")
    lines.append(f"ffprobe ：{FFPROBE or '✗ 未找到'}")

    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        r.update()
        r.destroy()
        lines.append("tkinter ：OK（图形界面可用）")
        gui_ok = True
    except Exception as exc:  # noqa: BLE001
        lines.append(f"tkinter ：✗ {exc}")
        gui_ok = False

    demucs_ok = (PYLIBS / "demucs").is_dir()
    lines.append(f"Demucs  ：{'已安装（可选 AI 分离）' if demucs_ok else '未安装（只能用内置算法）'}")
    lines.append(f"Python  ：{_demucs_python() if demucs_ok else '—'}")

    if FFMPEG:
        try:
            out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 creationflags=CREATE_NO_WINDOW, timeout=15)
            lines.append("ffmpeg 版本：" + (out.stdout.splitlines() or ["?"])[0])
        except Exception as exc:  # noqa: BLE001
            lines.append(f"ffmpeg 版本：读取失败 {exc}")

    lines.append("")
    if np is not None and FFMPEG and FFPROBE and gui_ok:
        lines.append("结论：✓ 可以正常使用")
    elif FFMPEG and FFPROBE:
        lines.append("结论：⚠ 能用命令行，但图形界面有问题")
    else:
        lines.append("结论：✗ 缺少 ffmpeg/ffprobe，请把 bin\\ffmpeg.exe 和 bin\\ffprobe.exe 放好")

    out_path = APP_DIR / "自检结果.txt"
    try:
        out_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        out_path = Path.home() / "自检结果.txt"
        out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if "--selftest" in sys.argv:
        p = run_selftest()
        try:
            print(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        sys.exit(0)
    run_gui()
