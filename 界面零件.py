#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面零件.py — 几个面板都要用的小控件

搬自 `baxiadb-v1.6/config_gui/ui/widgets/`（`result_table.py` + `status_bar.py`），
外加 `utils/lifecycle.py` 里那一个 `safe_stop_timer`。

改动只有两处，都是因为霸下和参照项目的后端不一样：
  * `ResultTable.display_result` 原来收的是 `[{列名: 值}, ...]`（字典行），
    霸下的 `Result.as_lists()` 给的是**按位置排的列表**——加了一个按列名
    索引的入口，两个都收。
  * 状态栏原来的 `engine.is_running` / `engine.current_database` 在霸下这边
    由 `界面_接驳.引擎` 提供，读法没变。
"""

from __future__ import annotations

from typing import Any, Dict, List

from PySide6.QtCore import QRegularExpression, Qt, QTimer
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QSpinBox, QStatusBar,
                               QTableWidget, QTableWidgetItem)


# ══ 〇、能装大数的整数框 ═══════════════════════════════════════════════

#: `QSpinBox` 里装的是 **C 的 `int`（4 字节）**。上下限只要有一个越出这个
#: 范围，Qt 那段 C++ 就会 `Overflow`，**`setRange` 当场抛 `OverflowError`**。
#: 而它是在搭界面的时候抛的 —— 整个页面连同它上面那几个标签一起没了，
#: 报错指的还是 `setRange` 那一行，跟是哪个设置项一点关系都没有。
#:
#: 踩过两次，都是"这个值天生比 int32 大"：
#:
#:     `玄武饵纪元`  毫秒时间戳，1700000000000，十三个数字
#:     `自增起始值`  BIGINT 的起点，9999999999
整数上限 = 2 ** 31 - 1


def 装得下(低, 高):
    """这一格的上下限 `QSpinBox` 装不装得下。装不下才退回文本框那条路。"""
    try:
        return -整数上限 <= int(低) and int(高) <= 整数上限
    except (TypeError, ValueError):
        return False


def 整数框(低, 高, 宽=160):
    """
    一个装整数的控件。**回 `(控件, 读, 写)`**，`读()` 回 `int`，`写(值)` 摆进去。

    ⛔ **装得下就用 `QSpinBox`（有上下箭头、夹得死），装不下才退回文本框。**
    不能"夹到 int32 了事" —— 那等于把这一格的上限偷偷改小，人要填一个
    2038 年之后的毫秒时间戳就填不进去，而且**界面上看不出被改过**。

    两条路对调用方是一个东西：`读()` 一律回 `int`，**一律夹在 `[低, 高]`
    里**（和 `QSpinBox` 的行为对齐）。文本框那条路上**空框算 0，不抛** ——
    人把框清干净的时候是不想填，不是想让它当场报错。
    """
    if 装得下(低, 高):
        件 = QSpinBox()
        件.setRange(int(低), int(高))
        件.setMinimumWidth(宽)
        return 件, (lambda: int(件.value())), (lambda v: 件.setValue(int(v)))

    件 = QLineEdit()
    件.setValidator(QRegularExpressionValidator(
        QRegularExpression(r'-?\d{0,19}'), 件))
    件.setMinimumWidth(宽)

    def 读():
        try:
            数 = int(件.text().strip())
        except (TypeError, ValueError):
            数 = 0
        if 低 is not None:
            数 = max(int(低), 数)
        if 高 is not None:
            数 = min(int(高), 数)
        return 数

    return 件, 读, (lambda v: 件.setText(str(int(v))))


# ══ 一、查询结果表 ═════════════════════════════════════════════════════


class ResultTable(QTableWidget):
    """专门用于展示查询结果的表格控件。

    功能:
      - 自动调整列宽
      - NULL 值显示为 "NULL" 并特殊样式
      - 可排序
      - 交替行颜色
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

    def display_result(self, columns: List[str], rows: List[Any]) -> None:
        """展示查询结果数据。

        `rows` 一行一个，**字典行**（`{列名: 值}`）或**按位置排的列表**都收
        ——霸下的 `Result.as_lists()` 给的是后者。
        """
        self.clear()
        self.setColumnCount(len(columns))
        self.setHorizontalHeaderLabels(columns)
        self.setRowCount(len(rows))

        for r, row in enumerate(rows):
            if isinstance(row, dict):
                取 = row.get
            else:
                def 取(名, _行=row, _列=columns):
                    try:
                        return _行[_列.index(名)]
                    except (ValueError, IndexError):
                        return None
            for c, col_name in enumerate(columns):
                value = 取(col_name)
                if value is None:
                    item = QTableWidgetItem("NULL")
                    item.setForeground(Qt.gray)
                    f = QFont()
                    f.setItalic(True)
                    item.setFont(f)
                elif isinstance(value, bool):
                    item = QTableWidgetItem("1" if value else "0")
                elif isinstance(value, bytes):
                    item = QTableWidgetItem(f"<{len(value)} 字节>")
                else:
                    item = QTableWidgetItem(str(value))
                self.setItem(r, c, item)

    def display_message(self, message: str) -> None:
        """展示单列消息。"""
        self.clear()
        self.setColumnCount(1)
        self.setHorizontalHeaderLabels(["结果"])
        self.setRowCount(1)
        item = QTableWidgetItem(message)
        self.setItem(0, 0, item)


# ══ 二、定时器收尾 ═════════════════════════════════════════════════════


def safe_stop_timer(timer) -> None:
    """停一个定时器，已经没了也不报错。

    搬自 `utils/lifecycle.py:100`。控件被 Qt 删掉之后 `destroyed` 还会响一次，
    那时候定时器的 C++ 对象可能已经没了——直接 `timer.stop()` 会抛
    `RuntimeError`。
    """
    if timer is None:
        return
    try:
        timer.stop()
    except RuntimeError:
        pass


# ══ 三、状态栏 ═════════════════════════════════════════════════════════


# ══ 四、问口令（备份那把钥匙 / 新库那把锁）═══════════════════════════════
#
# 两个框摆在这儿，是因为它们**两个面板都要用**：备份按钮在「数据库管理」
# 页（`界面_浏览`），「从备份恢复」在第 6 页（`玄武_界面`）。而那两个模块
# **互相不 import**（各自 import 主窗是环）。放主窗 `霸下之躯.py` 也一样
# 是环（主窗 import 这两个面板）。所以落在这个谁都不 import 的零件模块里。
#
# 两个框都**只回答"人输的那句话"**，不碰盘、不碰引擎、不判断该不该问 ——
# 那两件事留给调用方（判据在 `备份.会换钥匙吗` 和 `备份.读备份钥匙`）。


def 问备份口令(父=None, 第一次=False, 份名=''):
    """
    问一句**备份口令** —— 给这一份备份另配的那把钥匙用的。回口令或 `None`。

    ⛔ **它和库口令是两句独立的话，别把它俩混成一句。** 库口令开的是库，
    备份口令开的是**第三处那个 `.钥匙.json`**。同一句也行、两句也行，
    但这个框里问到的字**只往 `备份.造备份钥匙` 那一处去**。

    `第一次=True`（第三处里还一把钥匙都没有）**连输两遍**，并且把
    「没有找回」印在框里 —— 备份口令丢了，那一份备份就**永远是乱码**，
    而这话只有第一次有机会说清楚。之后每份问一遍就够：那时候这个人
    手上已经有一句在用了，再让他对一遍只是多敲一遍，对不出什么。

    ⚠️ **口令不落盘、不进配置、不进日志。** 它在这一路上只活几秒：
    框里 → `造备份钥匙` → `pbkdf2` → 主密钥。之后手里只剩 `密文`。
    """
    名 = ('这一份备份（%s）' % 份名) if 份名 else '这一份备份'
    说明 = ('这是第一次给备份另配钥匙。下面这句话**只用来开备份钥匙**，'
            '和这个库的口令是两句话（写一样的也行）。\n\n'
            '⚠ 输两遍。**没有找回这一说**：这句话忘了，'
            '配上它的那些备份**永远是乱码**（备份目录里躺着的字节一个不少，'
            '只是再没人打得开）。\n\n'
            '⛔ 别把它写在库文件旁边，也别写在备份目录里 ——'
            '那等于把钥匙和锁摆一起。') if 第一次 else (
        '给%s另配一把钥匙。\n\n'
        '（和上一份用同一句就行 —— 每一份的盐和随机数都是新抽的，'
        '同一句话配出来的钥匙也是几把不同的。）' % 名)
    while True:
        口, 好 = QInputDialog.getText(
            父, '备份口令（第一次）' if 第一次 else '这一份的备份口令',
            说明 + '\n\n备份口令：', QLineEdit.Password, '')
        if not 好:
            return None
        if not 口:
            QMessageBox.information(父, '口令是空的',
                                    '空口令等于没上锁。重输一个。')
            continue
        if not 第一次:
            return 口
        再, 好 = QInputDialog.getText(
            父, '再输一遍', '刚才那句备份口令再输一遍：',
            QLineEdit.Password, '')
        if not 好:
            return None
        if 再 != 口:
            QMessageBox.information(父, '两遍不一样',
                                    '两遍输得不一样，重来吧。')
            continue
        return 口


def 问新库口令(父=None, 备份路=''):
    """
    恢复时**给新库定一句口令**。回口令或 `None`（人点了取消）。

    ⛔ **不是把备份那把锁搬过来。** 搬过来的话新库的口令就变成那句
    **备份口令**，而"改口令结构上做不到"（`玄武_钥` 那条：换口令 = 换
    主密钥 = 换密钥流 = 全库作废）—— 那句口令就被钉死成这个库的口令了。
    恢复出来的该是**一个正经的库**，不是一份"用别人的口令活着"的副本。

    所以这个框问出来的话，交给 `备份.恢复一份` 当 `新口令`：那边会
    **新抽盐、新抽随机数**给它造一把全新的锁，和那份备份彻底解绑。
    """
    说明 = ('恢复出来的是一个**新库**，它有自己的口令 ——'
            '和那份备份的口令是两句独立的话。\n\n'
            '这一份备份：\n%s\n\n'
            '⚠ 输两遍，**没有找回这一说**。\n'
            '备份留在原地不动，这句忘了还能拿备份再来一次。') % (备份路 or '')
    while True:
        口, 好 = QInputDialog.getText(
            父, '新库的口令', 说明 + '\n\n新库的口令：',
            QLineEdit.Password, '')
        if not 好:
            return None
        if not 口:
            QMessageBox.information(父, '口令是空的',
                                    '空口令等于没上锁。重输一个。')
            continue
        再, 好 = QInputDialog.getText(
            父, '再输一遍', '刚才那句新库口令再输一遍：',
            QLineEdit.Password, '')
        if not 好:
            return None
        if 再 != 口:
            QMessageBox.information(父, '两遍不一样',
                                    '两遍输得不一样，重来吧。')
            continue
        return 口


# ══ 五、状态栏 ═════════════════════════════════════════════════════════


class baxiaStatusBar(QStatusBar):
    """
    显示连接状态、当前数据库、配置信息。

    原样搬自 `ui/widgets/status_bar.py`。

    **`_config_label` 那一格保留但是空的**：参照项目里它显示的是配置管理那一
    层的状态，霸下没有那一层。留着一个空的"配置: 默认"比编一个假状态强——
    但你要是嫌它占地方，删掉它是删这一行的事。
    """

    def __init__(self, engine, host: str = "127.0.0.1", port: int = 3306,
                 parent=None):
        super().__init__(parent)
        self.engine = engine
        self._host = host
        self._port = port

        self._conn_label = QLabel(f"{host}:{port}")
        self._conn_label.setStyleSheet(
            "color: #2563EB; font-weight: bold; padding: 0 12px;")
        self.addPermanentWidget(self._conn_label)

        self._status_label = QLabel("就绪")
        self._status_label.setStyleSheet(
            "color: #16A34A; font-weight: bold;")
        self.addPermanentWidget(self._status_label)

        self._db_label = QLabel("数据库: (无)")
        self.addPermanentWidget(self._db_label)

        # MySQL 兼容口那一格。**它跟"连到哪儿"是两回事**：上面那个
        # `host:port` 说的是界面自己这条腿连到哪儿，这一格说的是"外面还有
        # 一个 MySQL 口在听没有"。两个都摆着才看得出分别。
        self._mysql_label = QLabel("MySQL 口: 关")
        self._mysql_label.setStyleSheet("color: #64748B; padding: 0 12px;")
        self.addPermanentWidget(self._mysql_label)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(2000)
        self.destroyed.connect(lambda: safe_stop_timer(self._timer))

    def refresh(self) -> None:
        if self.engine is None:
            return
        if self.engine.is_running:
            self._status_label.setText("运行中")
            self._status_label.setStyleSheet(
                "color: #16A34A; font-weight: bold;")
        else:
            self._status_label.setText("已停止")
            self._status_label.setStyleSheet(
                "color: #DC2626; font-weight: bold;")

        db = self.engine.current_database or "(无)"
        self._db_label.setText(f"数据库: {db}")

        self._conn_label.setText(f"{self._host}:{self._port}")

        # **引擎壳上没有 `MySQL口` 这一格就照实说"没有"**，不假装"关着"
        # ——那两件事在用户眼里差很远（一个是"这一版没这功能"，一个是
        # "有这功能，现在没开"）。
        门 = getattr(self.engine, 'MySQL口', None)
        if 门 is None:
            self._mysql_label.setText("MySQL 口: (这一版没有)")
            self._mysql_label.setStyleSheet("color: #64748B; padding: 0 12px;")
        elif 门.听着():
            在哪 = getattr(self.engine, 'MySQL地址', '') or ''
            self._mysql_label.setText(
                f"MySQL 口: {在哪}:{门.端口()} 听着 {门.连接数()} 条")
            self._mysql_label.setStyleSheet(
                "color: #16A34A; font-weight: bold; padding: 0 12px;")
        else:
            self._mysql_label.setText("MySQL 口: 关")
            self._mysql_label.setStyleSheet("color: #64748B; padding: 0 12px;")
