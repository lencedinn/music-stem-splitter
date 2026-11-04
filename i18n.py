# -*- coding: utf-8 -*-
"""
i18n — 中英文切换（只给打包后的便携版用）

设计原则：
  · 界面控件上的原文是中文，这里维护 中文 → 英文 的映射；
  · 设置时遍历控件树批量替换，所以**不用改构建界面那部分的代码**；
  · 内部用的键（格式名、强度名）保持中文，界面显示时再翻译，
    读回来时用 to_zh() 转回去，保证字典查找不会 KeyError。
"""

import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------- 控件文字

ZH2EN = {
    # 顶部按钮
    "添加文件": "Add files",
    "添加文件夹": "Add folder",
    "移除所选": "Remove selected",
    "清空": "Clear",
    "音色分析（推测）": "Analyze timbre (guess)",

    # 设置区
    "分离设置": "Separation settings",
    "分离人声": "Separate vocals",
    "（纯音乐 / 纯 BGM 请取消）": "(uncheck for instrumental / BGM only)",
    "输出完整伴奏": "Export full instrumental",
    "伴奏切「低/中/高」（人声不切）": "Split instrumental into low/mid/high (vocals untouched)",
    "分离强度：": "Strength:",
    "导出格式：": "Format:",
    "码率：": "Bitrate:",
    "采样率：": "Sample rate:",
    "视频文件同时导出「无声视频」（音视频分离）":
        "Also export muted video (demux)",
    "分频点": "Crossover",
    "Hz  /": "Hz  /",
    "Hz": "Hz",
    "（低于第一个值=低，中间=中，高于第二个=高）":
        "(below first = low, between = mid, above second = high)",
    "分离引擎：": "Engine:",
    "用 Demucs（AI，质量高但慢）": "Use Demucs (AI, slower but much cleaner)",
    "用 Demucs（未安装，当前用内置算法）":
        "Use Demucs (not installed, using built-in)",
    "输出目录：": "Output folder:",
    "浏览…": "Browse…",
    "输出到源文件同目录": "Same folder as source",
    "保持原样": "Keep original",
    "开始分离": "Start",
    "停止": "Stop",
    "日志": "Log",

    # 表格列头
    "文件": "File",
    "类型": "Type",
    "时长": "Duration",
    "源格式": "Source",
    "状态": "Status",

    # 状态词
    "音频": "audio",
    "视频": "video",
    "未知": "unknown",
    "等待": "waiting",
    "读取中": "reading",
    "解码中": "decoding",
    "分离中": "separating",
    "AI 分离中": "separating (AI)",
    "切分频段": "splitting bands",
    "完成": "done",
    "失败": "failed",
    "已停止": "stopped",
}

# ---------------------------------------------------------------- 格式名

FORMAT_EN = {
    "MP3（兼容最好）": "MP3 (most compatible)",
    "FLAC（无损）": "FLAC (lossless)",
    "WAV（24bit 无损）": "WAV 24-bit",
    "WAV（16bit）": "WAV 16-bit",
    "M4A / AAC（通用）": "M4A / AAC",
    "ALAC（无损，苹果）": "ALAC (Apple lossless)",
    "OGG Vorbis": "OGG Vorbis",
    "OPUS（体积最小）": "OPUS (smallest)",
    "WMA": "WMA",
    "AIFF（无损）": "AIFF (lossless)",
}

STRENGTH_EN = {"温和": "Gentle", "标准": "Standard", "强力": "Strong"}

# ---------------------------------------------------------------- 参考文案

TITLE_EN = "Stem Splitter v{ver} — Vocals / Instrumental"

DROP_TITLE_EN = "Add audio / video"

ZH2EN.update(FORMAT_EN)
ZH2EN.update(STRENGTH_EN)

EN2ZH = {v: k for k, v in ZH2EN.items()}


def to_zh(value):
    """英文界面值 → 内部中文键（保证字典查找不炸）。"""
    return EN2ZH.get(value, value)


def to_display(value, lang):
    """内部中文键 → 当前语言显示。"""
    return ZH2EN.get(value, value) if lang == "en" else EN2ZH.get(value, value)


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def apply_lang(window, lang, title=None, exclude=()):
    """把窗口里所有控件的中文/英文互换到指定语言。可反复调用。

    exclude：不参与翻译的控件（比如语言选择框自己）。
    """
    def conv(s):
        return ZH2EN.get(s, s) if lang == "en" else EN2ZH.get(s, s)

    for w in _walk(window):
        if w in exclude:
            continue
        try:
            keys = w.keys()
        except Exception:
            continue
        if "text" in keys:
            try:
                cur = w.cget("text")
                new = conv(cur)
                if new != cur:
                    w.configure(text=new)
            except Exception:
                pass
        try:
            if isinstance(w, ttk.Combobox):
                vals = [str(v) for v in w.cget("values")]
                new = [conv(v) for v in vals]
                if new != vals and new:
                    w.configure(values=new)
                # 选中项存在 textvariable 里，也得跟着换，否则界面显示的还是旧语言
                var = str(w.cget("textvariable"))
                if var:
                    cur = window.getvar(var)
                    new_cur = conv(str(cur))
                    if new_cur != str(cur):
                        window.setvar(var, new_cur)
        except Exception:
            pass

    if title:
        try:
            window.title(title)
        except Exception:
            pass
