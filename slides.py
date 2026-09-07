#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SlideBox 進入點：啟動 PySide6 桌面視窗。

    uv run slides.py
"""
import os
import sys

# 讓 src 版面配置可被 import
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from slidebox.presentation.app import run

if __name__ == "__main__":
    run()
