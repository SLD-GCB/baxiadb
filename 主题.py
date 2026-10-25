#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主题.py — 浅色 / 深色两套色板 + 全局 QSS

**原样搬自 `baxiadb-v1.6/config_gui/utils/theme.py`**，一个字没改（连字典的英文
键都留着——各面板里写的是 `theme["primary"]` 这种取法，改名全得跟着动）。

各面板里那些写死的颜色（`#2563EB` 主操作 / `#DC2626` 危险 / `#16A34A` 成功 /
`#F59E0B` 警告 / `#64748B` 中性 / `#94A3B8` 空值 / `#F0FDF4` 存成 /
`#FEF2F2` 存败）**不从这里取**，它们是各面板自己的约定，照原样留在原处。
"""

from __future__ import annotations

from PySide6.QtGui import QPalette, QColor

# ---------------------------------------------------------------------------
# Color palettes
# ---------------------------------------------------------------------------

LIGHT_THEME = {
    "primary": "#2563EB",
    "primary_light": "#60A5FA",
    "primary_dark": "#1D4ED8",
    "success": "#16A34A",
    "warning": "#F59E0B",
    "danger": "#DC2626",
    "background": "#F8FAFC",
    "surface": "#FFFFFF",
    "text": "#1E293B",
    "text_secondary": "#64748B",
    "border": "#E2E8F0",
    "hover": "#F1F5F9",
    "active": "#DBEAFE",
}

DARK_THEME = {
    "primary": "#60A5FA",
    "primary_light": "#93C5FD",
    "primary_dark": "#2563EB",
    "success": "#22C55E",
    "warning": "#FBBF24",
    "danger": "#EF4444",
    "background": "#0F172A",
    "surface": "#1E293B",
    "text": "#F1F5F9",
    "text_secondary": "#94A3B8",
    "border": "#334155",
    "hover": "#1E293B",
    "active": "#1D4ED8",
}


def get_theme(name: str = "light") -> dict:
    """Return a theme palette by name."""
    if name == "dark":
        return DARK_THEME
    return LIGHT_THEME


def apply_theme(app, name: str = "light") -> None:
    """Apply theme to the application."""
    theme = get_theme(name)

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(theme["background"]))
    palette.setColor(QPalette.WindowText, QColor(theme["text"]))
    palette.setColor(QPalette.Base, QColor(theme["surface"]))
    palette.setColor(QPalette.AlternateBase, QColor(theme["hover"]))
    palette.setColor(QPalette.Text, QColor(theme["text"]))
    palette.setColor(QPalette.Button, QColor(theme["surface"]))
    palette.setColor(QPalette.ButtonText, QColor(theme["text"]))
    palette.setColor(QPalette.Highlight, QColor(theme["primary"]))
    palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))

    app.setPalette(palette)

    # Apply global stylesheet
    stylesheet = f"""
    QMainWindow {{
        background-color: {theme["background"]};
    }}
    QTabWidget::pane {{
        border: 1px solid {theme["border"]};
        background-color: {theme["surface"]};
    }}
    QTabBar::tab {{
        padding: 8px 16px;
        border: 1px solid {theme["border"]};
        border-bottom: none;
        background-color: {theme["hover"]};
    }}
    QTabBar::tab:selected {{
        background-color: {theme["surface"]};
        border-bottom: 2px solid {theme["primary"]};
    }}
    QTreeWidget {{
        border: 1px solid {theme["border"]};
        background-color: {theme["surface"]};
    }}
    QTableWidget {{
        border: 1px solid {theme["border"]};
        gridline-color: {theme["border"]};
        background-color: {theme["surface"]};
    }}
    QPushButton {{
        padding: 6px 12px;
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background-color: {theme["surface"]};
        color: {theme["text"]};
    }}
    QPushButton:hover {{
        background-color: {theme["hover"]};
    }}
    QPushButton:pressed {{
        background-color: {theme["active"]};
    }}
    QLineEdit {{
        padding: 4px 8px;
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background-color: {theme["surface"]};
    }}
    QTextEdit {{
        border: 1px solid {theme["border"]};
        background-color: {theme["surface"]};
    }}
    QComboBox {{
        padding: 4px 8px;
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background-color: {theme["surface"]};
    }}
    QStatusBar {{
        border-top: 1px solid {theme["border"]};
        background-color: {theme["surface"]};
    }}
    """
    app.setStyleSheet(stylesheet)
