#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面_数据.py — 数据查看面板（搬自 `baxiadb-v1.6/config_gui/ui/data_viewer.py`）

**界面一个字没动。** 布局、控件、文案（含 emoji 和全角符号）、颜色
（`#2563EB`/`#DC2626`/`#16A34A`/`#F59E0B`/`#64748B`/`#94A3B8` 那一套）、tooltip、
列头文字、按钮次序、分页条的文案、`setMinimumWidth`/`setMinimumHeight`、样式表
字符串——全部逐字照抄。**改的只有数据通路**，下面一条一条说。

改了什么
========

1. **取数换成投活。** `Planner` / `Optimizer` / `Executor` 那一套删了，拼好的
   中文语句交给 `引擎.取` / `引擎.发`——非阻塞，回话在主线程里被叫。
   语句写错**不算"活炸了"**：那时候 `结果.ok` 是 False，`结果.error` 里的中文
   原话照原样摆到界面上（一个字不加工）。

2. **值一律走 `?` 绑。** 参照项目那个 `_escape_sql_string`（把值转义成 SQL
   字面量）连同它整套拼字面量的写法一起没了用场：霸下这边值与条件都走参数，
   只有 `限制` / `偏移` 后面必须是字面量整数（这一层没有那两样）。

3. **分页换成"一次取回来排好、在手里那份上切"**（用户当面强调过的那条）。
   点开一张表投**一件**活，把这张表整个取回来
   （``查询 * 从 `表` [其中 …] 排序 按 `主键` ``，**不带 `限制`**），存进内存：
   `self._全量行` 放**显示**用的那串字，`self._真值行` 放**真值**
   （一格一个 `(类型号, 值)`，见 `syntax.Result.rows`）。
   **翻页 / 改页大小 / 点表头排序都在内存那份上切，一件活都不投**——参照项目
   每翻一页问一次引擎、每页付一次全量排序，那正是用户否掉的。只有「刷新」
   （F5 / 刷新按钮 / 主窗的 `refresh()`）才重新投活取一遍。

4. **超过 20 万行引擎会拒**（`结果上限`，`syntax.py` 里 `max_result_rows` 的
   默认值，是硬上限）。第一次不限量的取被拒、而且拒的是"结果超上限"时，退一步
   用 `限制 200000` 再取一次，并在底栏**如实写明"只取了前 20 万行"**——不假装
   是全量。（别的几条上限——扫描 / 连接 / 并集——退让救不了，照原样报错，见
   `_是超上限`。）

5. **目录是"同步读缓存、异步刷新"**（`界面_接驳.目录`）。所以 `_refresh_db_list`
   / `_load_schema` 那几处同步读法一个字不用改，但**刷新目录要投出去、回到
   回话里才读**（`_刷新目录` / `_收到目录`）。

6. **`current_database` 那一套"存旧的、改新的、完事改回来"删了**：霸下开一个
   内核就是一个库，写它是摆设（`界面_接驳.引擎`）。

7. **「💾 保存数据库」在霸下是空转。** 控件、文案、tooltip、快捷键全保留，
   但 `save_all()` 是空壳（返回 `{'errors': []}`）——霸下每条语句写完就落盘，
   没有"攒着再存"这一步。点完给的那句话改成实话（见 `_on_save_database`）。

8. **没有信号往外发。** 参照项目这一个面板没定义过 `Signal`（`Signal` 是 import
   进来没用上的），这里也不发——主窗那边不需要从这个面板接东西。

两处自己拍的板（都摆在这儿，等裁决）
====================================

* **点表头排序接上了。** 参照项目把 `_sort_column` / `_sort_order` 两个字段留着
  却**没有任何一处写它们**，点表头什么都不会发生。要求是"按列点表头排序也走
  内存那条"，所以把 `sectionClicked` 接到了 `_on_header_clicked`——这一句写在
  `__init__` 里，**`_setup_ui` 一个字节没动**。
* **「💾 保存数据库」点完那句提示改成实话了**（见第 7 条）：参照项目那句
  "数据库已保存"在霸下是假的。要照旧说那句假的，改 `_on_save_database` 里
  那两行即可。

这一页没有的东西
================

**没有自检、没有 `__main__`、没有断言、没有探针**——这个文件跑起来就是界面。
"""

from __future__ import annotations

import csv
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QComboBox, QLineEdit, QMessageBox,
    QSpinBox, QHeaderView, QFrame, QAbstractItemView,
    QMenu, QFileDialog, QDialog, QFormLayout, QScrollArea,
)

import schema


# ══ 一、格子里那一格字：显示用 / 导出用 ════════════════════════════════


def _格文本(值) -> str:
    """
    一格在表格里显示成什么字。

    **二进制不往文本上凑**：`Result.as_lists()` 给的是 `bytes`，参照项目里
    这一格直接 `str()`，于是显示成 `b'\\x01…'` 那种东西。这里给 `<N 字节>`。
    空值是 `"NULL"`（颜色和斜体由 `_populate_grid` 上，和参照项目一样）。
    """
    if 值 is None:
        return "NULL"
    if isinstance(值, (bytes, bytearray)):
        return f"<{len(值)} 字节>"
    return str(值)


def _导出文本(类型号, 值) -> str:
    """
    导出一格写成什么。

    **走真值，不走界面上那串字**：二进制在界面上是 `<N 字节>`，导出也写这个
    的话，导出文件里就没有数据了——这里给 `repr`，一眼看得出是二进制。
    时间列反过来：真值是毫秒数，导成毫秒数没人看得懂，用 `schema.render` 的
    格式化文本。其余（整数 / 实数 / 文本）本来就跟界面上那串字一样。

    空值写 `"NULL"`，和界面上一致（也免得跟空串分不开）。
    """
    if 值 is None:
        return "NULL"
    if isinstance(值, (bytes, bytearray)):
        return repr(值)
    if 类型号 == schema.TIME:
        return schema.render(类型号, 值)
    if isinstance(值, float):
        return repr(值)
    return str(值)


def _排序键(值):
    """
    排序用的键。**空值在升序里排最后**——和引擎是同一个约定
    （`schema.compare`：空比任何值都大），所以降序时它排在**最前**。

    `None` 不能直接参与比大小（`None < None` 当场 `TypeError`），先用一个
    布尔位把它分出去，真值那一位只在同类之间比（一列一个类型，不会串）。
    """
    return (值 is None, 0 if 值 is None else 值)


def _null_item_font() -> QFont:
    """返回用于显示 NULL 值的斜体字体。"""
    f = QFont()
    f.setItalic(True)
    return f


class DataViewer(QWidget):
    """数据查看与自由编辑器 — 选择数据库和表即可查看、编辑数据。"""

    ROWS_PER_PAGE = 100

    #: 引擎的单次结果上限（`syntax.py` 里 `max_result_rows` 的默认值）。
    #: 一次取数超过它会被拒，退让的第二步按这个数取。**这是个硬上限，
    #: 不是个可调的愿望**；内核要是用别的上限开的，这里落不准，界面会
    #: 把引擎那句原话摆出来（不猜）。
    单次上限 = 200000

    def __init__(self, engine, database_name: str = "", table_name: str = "", parent=None):
        super().__init__(parent)
        self.engine = engine          # `界面_接驳.引擎`：取 / 发 / 投 + catalog
        self.database_name = database_name
        self.table_name = table_name
        self._page = 0
        self._total_rows = 0
        self._sort_column = ""
        self._sort_order = "ASC"
        self._filter_text = ""
        self._pk_columns: list[str] = []
        self._columns: list[str] = []
        self._editing_row = -1
        self._pending_new_rows: list[int] = []

        # ── "一次取回来、在手里那份上切"的本钱 ──────────────────────────
        # 两份按下标对齐：`_全量行` 只用来填表格，`_真值行` 管排序、比大小、
        # 回写（时间列的真值是毫秒数，拿显示文本去比大小会排错——`'10' < '9'`）。
        self._全量行: list[list] = []
        self._真值行: list[list] = []
        self._全量列: list[str] = []
        self._列类型: list[int] = []
        # 当前页那一段（是上面两份的切片，**里面的行是同一批对象**，改了就是
        # 改了全量那份，翻页回来不会又变回旧值）
        self._页行: list[list] = []
        self._页真值行: list[list] = []
        self._页对: list[tuple] = []
        self._取数号 = 0        # 取数的号：回来的那份不是最新一次投的就丢掉
        self._截断 = False      # 这一份是不是"只取到了前 20 万行"

        self._setup_ui()
        self._setup_shortcuts()
        # 点表头就按那一列排——**在内存那份上排**。参照项目把排序状态留着却
        # 没人写它，点表头没反应；要求里点名了"按列点表头排序也走内存这条"。
        # 接在这一句里，`_setup_ui` 原样未动。
        self._table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        # 初始加载数据库列表（霸下这边是异步的：投给工人去拉，回来了才换）
        self._刷新目录()
        # 定时自动刷新（仅在可见时）
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._auto_refresh)
        self._refresh_timer.start(10000)

    def _auto_refresh(self) -> None:
        """
        定时刷新数据库列表。

        参照项目这里读的是活的目录（同步、不花钱）；霸下目录是一份缓存，
        所以要**投一次目录刷新**，回来了才换下拉框。**只换列表，不重取
        数据**——每 10 秒把当前表整个重取一遍不是这个定时器该干的事。
        """
        if self.isVisible():
            self._刷新目录(连数据=False)

    # ===================================================================
    # UI 构建
    # ===================================================================

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # ====== 第一行：数据库/表选择器 ======
        selector_bar = QHBoxLayout()
        selector_bar.setSpacing(8)

        # 数据库标签和下拉框
        # （霸下只有一个库，这个下拉框里永远只有一项——"一个内核就是一个库"。
        #   控件保留是因为主窗是照参照项目那一套造的，别的地方不用改。）
        selector_bar.addWidget(QLabel("数据库:"))
        self._db_combo = QComboBox()
        self._db_combo.setMinimumWidth(160)
        self._db_combo.setToolTip("选择要查看的数据库")
        self._db_combo.currentTextChanged.connect(self._on_db_changed)
        selector_bar.addWidget(self._db_combo)

        # 表标签和下拉框
        selector_bar.addWidget(QLabel("表:"))
        self._table_combo = QComboBox()
        self._table_combo.setMinimumWidth(180)
        self._table_combo.setToolTip("选择要查看的数据表")
        self._table_combo.currentTextChanged.connect(self._on_table_changed)
        selector_bar.addWidget(self._table_combo)

        # 刷新按钮
        refresh_btn = QPushButton("刷新")
        refresh_btn.setToolTip("刷新数据库和表列表 (F5)")
        refresh_btn.clicked.connect(self._on_refresh_all)
        refresh_btn.setFixedWidth(50)
        selector_bar.addWidget(refresh_btn)

        selector_bar.addSpacing(16)

        # 过滤器
        selector_bar.addWidget(QLabel("过滤:"))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("列名 = 值 ...")
        self._filter_edit.setMaximumWidth(160)
        self._filter_edit.returnPressed.connect(self._apply_filter)
        selector_bar.addWidget(self._filter_edit)

        filter_apply_btn = QPushButton("应用")
        filter_apply_btn.setFixedWidth(45)
        filter_apply_btn.clicked.connect(self._apply_filter)
        selector_bar.addWidget(filter_apply_btn)

        filter_clear_btn = QPushButton("清除")
        filter_clear_btn.setFixedWidth(45)
        filter_clear_btn.clicked.connect(self._clear_filter)
        selector_bar.addWidget(filter_clear_btn)

        selector_bar.addStretch()
        layout.addLayout(selector_bar)

        # ====== 第二行：数据表格 ======
        self._table = QTableWidget()
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 编辑触发器：双击 / 选中后单击 / F2 / 直接输入
        self._table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.EditKeyPressed |
            QAbstractItemView.AnyKeyPressed
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(True)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.itemChanged.connect(self._on_cell_changed)
        self._table.setToolTip("单击选中 → 再单击编辑 | F2编辑 | 直接输入编辑 | 右键查看更多操作")
        layout.addWidget(self._table, 1)

        # ====== 第三行：底栏（状态 + 分页 + 操作按钮）======
        footer = QHBoxLayout()
        footer.setSpacing(6)

        # 状态信息
        self._info_label = QLabel("请选择数据库和表")
        self._info_label.setStyleSheet("color: #64748B; font-size: 12px;")
        footer.addWidget(self._info_label, 1)

        # 分页区域
        footer.addWidget(QLabel("每页:"))
        self._page_spin = QSpinBox()
        self._page_spin.setRange(10, 5000)
        self._page_spin.setValue(self.ROWS_PER_PAGE)
        self._page_spin.setMaximumWidth(70)
        self._page_spin.setToolTip("每页显示行数")
        self._page_spin.valueChanged.connect(self._on_page_size_changed)
        footer.addWidget(self._page_spin)

        self._page_label = QLabel("")
        self._page_label.setMinimumWidth(80)
        footer.addWidget(self._page_label)

        # 分页按钮（紧凑）
        # （这几个按钮**一直是能按的**，和参照项目一样：越界由方法自己拦，
        #   不靠禁用。翻页现在只在内存那份上切，按一下不花钱。）
        for label, slot in [
            ("◀◀", self._first_page), ("◀", self._prev_page),
            ("▶", self._next_page), ("▶▶", self._last_page),
        ]:
            btn = QPushButton(label)
            btn.setFixedSize(28, 24)
            btn.setToolTip({"◀◀":"首页","◀":"上一页","▶":"下一页","▶▶":"末页"}[label])
            btn.clicked.connect(slot)
            footer.addWidget(btn)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setMaximumWidth(2)
        footer.addWidget(sep)

        # 操作按钮（设置足够宽度确保中文完整显示）
        add_btn = QPushButton("＋添加行")
        add_btn.setToolTip("在末尾添加空行 (Ctrl+N)")
        add_btn.setMinimumWidth(78)
        add_btn.clicked.connect(self._on_add_row)
        footer.addWidget(add_btn)

        del_btn = QPushButton("－删除行")
        del_btn.setToolTip("删除选中的行 (Del)")
        del_btn.setMinimumWidth(78)
        del_btn.clicked.connect(self._on_delete_row)
        del_btn.setStyleSheet("QPushButton { color: #DC2626; }")
        footer.addWidget(del_btn)

        save_btn = QPushButton("保存新行")
        save_btn.setToolTip("保存所有新添加的行到数据库 (Ctrl+S)")
        save_btn.setMinimumWidth(78)
        save_btn.clicked.connect(self._on_save_new_rows)
        save_btn.setStyleSheet("QPushButton { color: #16A34A; font-weight: bold; }")
        footer.addWidget(save_btn)

        write_btn = QPushButton("写入数据")
        write_btn.setToolTip("打开表单直接写入一行数据到数据库 (Ctrl+W)")
        write_btn.setMinimumWidth(78)
        write_btn.clicked.connect(self._on_write_data)
        write_btn.setStyleSheet("QPushButton { color: #2563EB; font-weight: bold; }")
        footer.addWidget(write_btn)

        export_btn = QPushButton("导出CSV")
        export_btn.setToolTip("导出当前页数据为CSV文件")
        export_btn.setMinimumWidth(72)
        export_btn.clicked.connect(self._export_csv)
        footer.addWidget(export_btn)

        # 分隔线
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.VLine)
        sep2.setMaximumWidth(2)
        footer.addWidget(sep2)

        # 全局保存数据库按钮
        # （**霸下这一句是空转**，见 `_on_save_database` 的说明。控件照留。）
        save_db_btn = QPushButton("💾 保存数据库")
        save_db_btn.setObjectName("save_db_btn")
        save_db_btn.setToolTip("全局保存：将 catalog、用户、配置、缓冲池一并写入磁盘 (Ctrl+Shift+D)")
        save_db_btn.setMinimumWidth(110)
        save_db_btn.clicked.connect(self._on_save_database)
        save_db_btn.setStyleSheet(
            "QPushButton { color: #FFFFFF; background-color: #2563EB; "
            "font-weight: bold; border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: #1D4ED8; }"
        )
        footer.addWidget(save_db_btn)

        layout.addLayout(footer)

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("F5"), self, activated=self._on_refresh_all)
        QShortcut(QKeySequence("Ctrl+C"), self, activated=self._copy_cell)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._on_save_new_rows)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._on_add_row)
        QShortcut(QKeySequence("Delete"), self, activated=self._on_delete_row)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self._on_write_data)
        QShortcut(QKeySequence("Ctrl+Shift+D"), self, activated=self._on_save_database)

    # ===================================================================
    # 数据库 / 表选择逻辑
    # ===================================================================

    def _refresh_db_list(self) -> None:
        """
        刷新数据库下拉列表。

        **读的是内存缓存，同步读、不卡**（`界面_接驳.目录`）。缓存怎么变新
        由 `_刷新目录` 负责——投出去、回来了再叫这一句。
        """
        current = self._db_combo.currentText()
        self._db_combo.blockSignals(True)
        try:
            self._db_combo.clear()
            self._db_combo.addItem("-- 选择数据库 --")
            for db_name in self.engine.catalog.database_names:
                self._db_combo.addItem(db_name)
            # 恢复之前的选择
            if current and self._db_combo.findText(current) >= 0:
                self._db_combo.setCurrentText(current)
            elif self.database_name and self._db_combo.findText(self.database_name) >= 0:
                self._db_combo.setCurrentText(self.database_name)
        except Exception:
            pass
        finally:
            self._db_combo.blockSignals(False)

    def _on_db_changed(self, db_name: str) -> None:
        """数据库下拉框改变时刷新表列表。"""
        if db_name == "-- 选择数据库 --" or not db_name:
            self._table_combo.blockSignals(True)
            self._table_combo.clear()
            self._table_combo.addItem("-- 选择表 --")
            self._table_combo.blockSignals(False)
            self._clear_table_display()
            return

        self.database_name = db_name
        self._table_combo.blockSignals(True)
        try:
            self._table_combo.clear()
            self._table_combo.addItem("-- 选择表 --")
            db = self.engine.catalog.get_database(db_name)
            for tname in db.table_names:
                self._table_combo.addItem(tname)
            # 如果之前有选中的表且仍存在则恢复
            if self.table_name and self._table_combo.findText(self.table_name) >= 0:
                self._table_combo.setCurrentText(self.table_name)
        except Exception:
            self._table_combo.clear()
            self._table_combo.addItem("-- (无法加载) --")
        finally:
            self._table_combo.blockSignals(False)

    def _on_table_changed(self, table_name: str) -> None:
        """表下拉框改变时自动加载数据。"""
        if table_name == "-- 选择表 --" or not table_name:
            self._clear_table_display()
            return

        self.table_name = table_name
        self._page = 0
        self._pending_new_rows.clear()
        self.load_data()

    def _clear_table_display(self) -> None:
        """清空表格显示。**内存里那一份也一起清**（两份得一起走）。"""
        self.table_name = ""
        self._table.clear()
        self._table.setRowCount(0)
        self._table.setColumnCount(0)
        self._pk_columns = []
        self._columns = []
        self._pending_new_rows.clear()
        self._info_label.setText("请选择数据库和表")
        self._info_label.setStyleSheet("color: #64748B; font-size: 12px;")
        self._page_label.setText("")
        # 内存那份作废，还在路上的取数回话也别再往界面上贴
        self._取数号 += 1
        self._全量行 = []
        self._真值行 = []
        self._全量列 = []
        self._列类型 = []
        self._页行 = []
        self._页真值行 = []
        self._页对 = []
        self._total_rows = 0
        self._截断 = False

    def _on_refresh_all(self) -> None:
        """
        刷新所有内容。

        **目录刷新是异步的**：投出去，回到回话里才读——"投出去之后不能立刻
        接着读目录"（`界面_接驳.目录`）。数据也在那个回话里重取一遍。
        """
        self._刷新目录(连数据=True)

    def _刷新目录(self, 连数据: bool = False) -> None:
        """投一件活去拉目录，回来了换下拉框（`连数据` 时顺带把当前表重取一遍）。"""
        def 回话(错):
            self._收到目录(错, 连数据)

        # `目录.刷新` 在一头正在刷的时候会把回话**立刻**叫一遍（错=None）。
        # 那时候读到的还是上一次那份缓存——它是**整份换**的、不是就地改，
        # 所以读到的一定是一份自洽的目录，只是可能不是最新。不碍事。
        self.engine.catalog.刷新(回话)

    def _收到目录(self, 错, 连数据: bool = False) -> None:
        """目录刷新的回话。**在主线程里被叫。** `错` 是 `None` 就是成功。"""
        if 错 is not None:
            self._报错(错, 前缀='目录刷新失败')
            return
        self._refresh_db_list()
        if 连数据 and self.database_name and self.table_name:
            self.load_data()

    # ===================================================================
    # 数据加载
    # ===================================================================

    def load_data(self, db_name: str = "", table_name: str = "") -> None:
        if db_name:
            self.database_name = db_name
            if self._db_combo.findText(db_name) >= 0:
                self._db_combo.setCurrentText(db_name)
        if table_name:
            self.table_name = table_name
            if self._table_combo.findText(table_name) >= 0:
                self._table_combo.setCurrentText(table_name)

        if not self.database_name or not self.table_name:
            return

        self.ROWS_PER_PAGE = self._page_spin.value()
        self._load_schema()
        try:
            self._取全量()
        except Exception as e:
            self._报错(e)

    def _load_schema(self) -> None:
        """加载表结构（主键列、列名）。**同步读目录缓存，不卡。**"""
        try:
            db = self.engine.catalog.get_database(self.database_name)
            tbl = db.table(self.table_name)
            self._columns = tbl.column_names
            self._pk_columns = tbl.primary_key
        except Exception:
            self._columns = []
            self._pk_columns = []

    # ── 取全量：一次取回来，存进内存 ────────────────────────────────────

    def _拼查询(self, 表名: str, 限制=None) -> str:
        """
        拼这条查询。

        `其中` 后面那一段是用户在过滤框里填的**条件原文**——霸下 `其中`
        后面的写法（`` `列` > 3 `` 这种中文的，不是 MySQL 那句）。`过滤` 那个
        框子的文案和 tooltip 是参照项目的，没动；这里只是把填进来的东西
        原样放进语句交给引擎（引擎认不了会当场报错，错误照原样显示）。
        """
        语句 = '查询 * 从 `%s`' % 表名
        if self._filter_text:
            语句 += ' 其中 %s' % self._filter_text
        # 定序给主键：页是**切**出来的，页和页之间要有确定的次序。主键一定有
        # （霸下不给建没主键的表），拿不到（目录缓存还是旧的）就退到第一列。
        定序 = self._pk_columns or self._columns[:1]
        if 定序:
            语句 += ' 排序 按 %s' % ', '.join('`%s`' % c for c in 定序)
        if 限制 is not None:
            # `限制` / `偏移` 后面**只能是字面量整数**，`?` 在这儿是错的。
            语句 += ' 限制 %d' % int(限制)
        return 语句

    def _取全量(self) -> None:
        """
        把当前表**一次**取回来（不带 `限制`），取回来的那份存在内存里，
        翻页在它上面切——**这是这一页唯一一种取数**。

        不带限制又超过引擎的 `结果上限` 时，`_收到全量` 里退一步再取一次
        （带 `限制`），并在界面上写明只取到了一部分。
        """
        if not self.database_name or not self.table_name:
            return
        self._取数号 += 1
        号, 表名 = self._取数号, self.table_name
        语句 = self._拼查询(表名)
        self.engine.取(语句, lambda 结果, 错: self._收到全量(结果, 错, 表名, 号, False))

    @staticmethod
    def _是超上限(文本) -> bool:
        """
        引擎拒的这句是不是"结果超过上限"。

        **这是认字，不是认码。** `Result` 上没有错误码，只有引擎那一句中文
        原话。两个词都得在才算数：`结果有 … 超过上限 …` 是结果集那一条
        （`加个 限制` 能救）；扫描 / 连接 / 并集那几条说的也是"超过上限"，
        **那几种退让救不了**——退让只会把一句真的错误盖成一句看着像成功的
        提示，那比报错难查得多。
        """
        if not 文本:
            return False
        return '结果有' in 文本 and '超过上限' in 文本

    def _收到全量(self, 结果, 错, 表名: str, 号: int, 已退让: bool) -> None:
        """全量取数的回话。**在主线程里被叫。**"""
        if 号 != self._取数号 or 表名 != self.table_name:
            return                      # 用户已经换过表 / 刷过新的一份，这份不算数
        if 错 is not None:
            self._报错(错)
            return
        if 结果 is None:
            self._报错('取数没有回结果')
            return
        if not 结果.ok:
            if not 已退让 and self._是超上限(结果.error):
                # 超过引擎的单次结果上限。退一步：带 `限制` 再取一次，并**在
                # 界面上写明只取到了前 20 万行**——不假装是全量。
                self.engine.取(
                    self._拼查询(表名, 限制=self.单次上限),
                    lambda r, e: self._收到全量(r, e, 表名, 号, True))
                return
            # 语句错了不算"活炸了"：`结果.error` 是引擎的中文原话，照原样显示
            self._报错(结果.error)
            return

        # ── 到手了。真值一份、显示一份，两份按下标对齐 ────────────────
        self._截断 = 已退让
        self._全量列 = [名 for 名, _型 in 结果.columns]
        if self._全量列:
            self._columns = list(self._全量列)
        self._列类型 = [型 for _名, 型 in 结果.columns]
        self._真值行 = [list(行) for 行 in 结果.rows]
        self._全量行 = 结果.as_lists()
        self._total_rows = len(self._全量行)
        self._重排()                    # 带着排序状态来的，立刻按它排
        self._渲染页()

    # ── 在内存那份上排 / 切 ─────────────────────────────────────────────

    def _重排(self) -> None:
        """
        按 `_sort_column` / `_sort_order` 在内存里重排那两份。**一件活都不投。**

        `None`（空值）在升序里排最后——和引擎同一个约定（空比任何值都大），
        所以降序时它排最前。
        """
        if not self._sort_column or not self._全量行:
            return
        if self._sort_column not in self._全量列:
            return
        列 = self._全量列.index(self._sort_column)
        降 = (self._sort_order == 'DESC')
        try:
            序 = sorted(range(len(self._真值行)),
                        key=lambda i: _排序键(self._真值行[i][列][1]),
                        reverse=降)
        except TypeError:
            # 同一列里混着比不了的类型（一列一个类型，理论上到不了这儿）。
            # 宁可不排，也不许把行排成一个错的次序。
            return
        self._全量行 = [self._全量行[i] for i in 序]
        self._真值行 = [self._真值行[i] for i in 序]

    def _渲染页(self) -> None:
        """
        把当前页填进表格。**数据来自内存里那份全量，一件活都不投。**

        切出来的是**同一批行对象**（不是拷一份）：在格子上改了一格，改的就
        是内存那份，翻页回来不会又变回旧值。
        """
        起 = self._page * self.ROWS_PER_PAGE
        止 = 起 + self.ROWS_PER_PAGE
        self._页行 = self._全量行[起:止]
        self._页真值行 = self._真值行[起:止]
        self._页对 = list(zip(self._页真值行, self._页行))
        self._populate_grid(self._全量列, self._页对)
        self._报状态()

    def _报状态(self) -> None:
        """
        底栏那两段字：分页那一段（文案照参照项目）+ 状态那一行。

        "只取到了前 20 万行"就写在这一行里（`#F59E0B` 那个警告色），
        **明说这不是全量**。
        """
        if self._total_rows > 0:
            总页 = max(1, (self._total_rows - 1) // self.ROWS_PER_PAGE + 1)
            self._page_label.setText(f"第{self._page+1}/{总页}页")
        else:
            self._page_label.setText("无数据")

        列数 = len(self._全量列)
        if self._截断:
            self._info_label.setText(
                f"{self.database_name}.{self.table_name} — "
                f"只取了前 {self._total_rows} 行 × {列数}列"
                f"（超过引擎单次结果上限 {self.单次上限} 行，剩下的没取回来）")
            self._info_label.setStyleSheet("color: #F59E0B; font-size: 12px;")
        else:
            self._info_label.setText(
                f"{self.database_name}.{self.table_name} — "
                f"{self._total_rows}行 × {列数}列")
            self._info_label.setStyleSheet("color: #16A34A; font-size: 12px;")

    def _报错(self, 文本, 前缀: str = '加载失败') -> None:
        """
        把一句错误摆到状态行上。**原样摆**：语句写错时 `结果.error` 里是引擎
        的中文原话，一个字都不加工（参照项目那句 `SQL错误: …` 的位置）。
        """
        self._info_label.setText(f"{前缀}: {文本}")
        self._info_label.setStyleSheet("color: #DC2626; font-size: 12px;")

    def _populate_grid(self, columns, rows) -> None:
        """
        把一页填进表格。

        `rows` 一行一个 `(真值行, 显示行)`：**显示行**是填表格用的那串字
        （`Result.as_lists()`，空是 `None`、二进制是 `bytes`），**真值行**是
        `(类型号, 值)`，挂在格子的 `Qt.UserRole` 上——编辑 / 删除时拼 `其中`
        用的是它（时间列的真值是毫秒数，拿显示文本去比是另一回事）。
        """
        self._table.blockSignals(True)
        try:
            self._table.setColumnCount(len(columns))
            self._table.setHorizontalHeaderLabels(columns)
            self._table.setRowCount(len(rows))

            for r, (真值行, 显示行) in enumerate(rows):
                for c, 值 in enumerate(显示行):
                    item = QTableWidgetItem(_格文本(值))
                    if 值 is None:
                        item.setForeground(QColor("#94A3B8"))
                        item.setFont(_null_item_font())
                    item.setData(Qt.UserRole,
                                 真值行[c][1] if c < len(真值行) else None)
                    self._table.setItem(r, c, item)
        finally:
            self._table.blockSignals(False)

    # ===================================================================
    # 内联编辑 — 自动保存
    # ===================================================================

    def _主键条件(self, 行: int):
        """
        按主键拼 `其中` 那一段，返回 `(条件原文, 参数)`。

        **空值不能用 `=` 比**（引擎当场会拒），判空要写 `为空`。值一律走 `?`
        绑。主键为空的表霸下不给建，所以主键一定有；万一目录缓存还是旧的、
        主键没读着，退到第一列（和参照项目一样）——那条路走没走对，回话里
        那个"影响了几行"会说出来（`_收到更新` 里那条 `affected != 1`）。
        """
        段, 参 = [], []
        for pk in (self._pk_columns or self._columns[:1]):
            if pk in self._columns:
                pk_列 = self._columns.index(pk)
                项 = self._table.item(行, pk_列)
                if 项 is None:
                    continue
                值 = 项.data(Qt.UserRole)
                if 值 is None:
                    段.append('`%s` 为空' % pk)
                else:
                    段.append('`%s` = ?' % pk)
                    参.append(值)
        return ' 与 '.join(段), tuple(参)

    def _on_cell_changed(self, item) -> None:
        """
        用户编辑单元格后自动通过 `更新` 保存。

        **异步**：语句投出去这一句就返回，回话（`_收到更新`）在主线程里到，
        黄 → 绿/红是那时候上的色（黄色就是"修改中"）。参照项目是同步执行完
        当场改色——异步之后中间多了一段"在路上"的时间，颜色正好把那段摆出来。
        """
        if item is None or self._table.signalsBlocked():
            return
        if not self.database_name or not self.table_name:
            return

        row = item.row()
        col = item.column()
        new_text = item.text().strip()
        col_name = self._table.horizontalHeaderItem(col).text() if self._table.horizontalHeaderItem(col) else ""

        old_value = item.data(Qt.UserRole)

        # 新加的那些行（还没进库）不在内存那份里，`_页对` 里没有它们的下标。
        # 那些格子改了就在格子上放着，等「保存新行」走 `插入`——**不能**在这儿
        # 去 `更新`：那种行按主键在库里根本找不到，发出去就是一条打空的语句，
        # 结果还只能报"影响 0 行"，白吵一趟。
        if row >= len(self._页对):
            return
        真值行, 显示行 = self._页对[row]
        if col >= len(显示行) or col >= len(真值行):
            return
        旧显示 = 显示行[col]
        列类型号 = self._列类型[col] if col < len(self._列类型) else 0

        # NULL 文本 ↔ None 值视为等价，跳过（防止新行触发无限 itemChanged
        # 递归导致栈溢出闪退）。**这儿比的是界面上那一格的字**：真值里时间列
        # 是毫秒数、二进制是 bytes，拿真值去比永远不相等，回写一次就再触发
        # 一次 itemChanged，一层层递归下去。
        is_new_null = (new_text.upper() == "NULL" or new_text == "") and old_value is None
        if is_new_null or new_text == _格文本(旧显示):
            return

        # 防止 IME 组合输入中间态导致中文被清空：
        # 如果新文本为空但旧值不为 NULL，可能是 IME 尚未提交，跳过保存
        if new_text == "" and old_value is not None:
            return

        new_value = None if new_text.upper() == "NULL" or new_text == "" else new_text

        # 黄色 = 修改中（参照项目是同步存完当场改色；这边这件活在**路上**，
        # 黄色正好把"在路上"那一段摆出来）
        item.setBackground(QColor("#FFFBEB"))

        # 二进制列不许在格子里当文本改：引擎那边 `schema.coerce` 会把用户敲的
        # 字按 utf-8 编成字节存下去，看着"改成了"，其实存的是那句字。判空
        # （设成 NULL）不算，那个是明说的。
        if 列类型号 == schema.BLOB and new_value is not None:
            self._更新失败(item, row, col, 显示行,
                           "更新失败: 二进制列不能在表格里当文本改（显示的是 <N 字节>）")
            return

        # 按列类型把用户敲的字规整成真值。**用的是引擎自己那把尺子**
        # （`syntax._fit` 也是调 `schema.coerce`），所以本地认得出的引擎一定收、
        # 本地认不出的引擎也一定拒——不会出现"界面上绿了、库里其实没变"。
        # 列类型号没拿到（`结果.columns` 里没有这一列）时不猜，把字原样绑过去
        # 让引擎自己判——`coerce(0, …)` 只会抛一句"认不出的类型号"，那是误报。
        if 列类型号:
            try:
                新真值 = schema.coerce(列类型号, new_value)
            except Exception as 错:
                self._更新失败(item, row, col, 显示行, f"更新失败: {错}")
                return
        else:
            新真值 = new_value

        # 构建 WHERE (基于主键)
        where_text, 参 = self._主键条件(row)
        if not where_text:
            return

        语句 = '更新 `%s` 设 `%s` = ? 其中 %s' % (self.table_name, col_name, where_text)

        # 值绑用户敲的那串字，让引擎按列类型规整；本地那份真值用同一把尺子
        # 先算好了，两边不会分家。
        self.engine.发(
            语句,
            lambda 结果, 错: self._收到更新(结果, 错, row, col, item, 真值行,
                                       显示行, col_name, new_value, 新真值,
                                       列类型号),
            (new_value,) + 参)

    def _收到更新(self, 结果, 错, row: int, col: int, item, 真值行, 显示行,
                col_name: str, new_value, 新真值, 列类型号) -> None:
        """`更新` 的回话。**在主线程里被叫。**"""
        if 错 is not None:
            self._更新失败(item, row, col, 显示行, f"更新失败: {错}")
            return
        if 结果 is None:
            self._更新失败(item, row, col, 显示行, "更新失败: 没有回结果")
            return
        if not 结果.ok:
            # 语句错了不算"活炸了"：引擎的中文原话，照原样显示
            self._更新失败(item, row, col, 显示行, f"更新失败: {结果.error}")
            return
        if 结果.affected != 1:
            # 按主键定位本该正好一行。0 行是那一行没了（别处删了）；多行是
            # `其中` 没落在主键上——两种都**不许报成功**：粉饰过去就是
            # "库里改了、界面上看着没改"，或者反过来的那种更糟的。
            self._更新失败(item, row, col, 显示行,
                           f"更新失败: 影响 {结果.affected} 行（按主键本该 1 行）")
            return

        # 成了：内存那一份得跟着改，不然翻页回来还是旧值
        真值行[col] = (列类型号, 新真值)
        显示行[col] = new_value
        # 界面这一格：**只在它还是原来那一格的时候**动它。翻过页 / 刷过新，
        # 这一格早就不在表里了（Qt 那边可能已经把它删了，碰它就是 RuntimeError）。
        if self._table.item(row, col) is item:
            item.setData(Qt.UserRole, 新真值)
            item.setBackground(QColor("#F0FDF4"))
        self._info_label.setText(f"已更新 {col_name} 第{row+1}行")
        self._info_label.setStyleSheet("color: #16A34A; font-size: 12px;")

    def _更新失败(self, item, row: int, col: int, 显示行, 消息: str) -> None:
        """更新没成：把那一格退回改之前的字，并把引擎的原话摆出来。"""
        try:
            if self._table.item(row, col) is item:
                item.setText(_格文本(显示行[col]))
                item.setBackground(QColor("#FEF2F2"))
        except RuntimeError:
            pass                        # 那一格已经被 Qt 删了，没什么可还原的
        self._info_label.setText(消息)
        self._info_label.setStyleSheet("color: #DC2626; font-size: 12px;")

    # ===================================================================
    # 分页 / 过滤
    # ===================================================================

    # 翻页、改页大小、点表头：**都在内存那份上切，一件活都不投**。
    # 参照项目每翻一页问一次引擎、每页付一次全量排序，那正是用户否掉的。

    def _first_page(self): self._page = 0; self._渲染页()
    def _prev_page(self):
        if self._page > 0: self._page -= 1; self._渲染页()
    def _next_page(self): self._page += 1; self._渲染页()
    def _last_page(self):
        if self._total_rows > 0:
            self._page = (self._total_rows - 1) // self.ROWS_PER_PAGE
            self._渲染页()

    def _on_page_size_changed(self, size: int):
        self.ROWS_PER_PAGE = size; self._page = 0
        if self.database_name and self.table_name:
            self._渲染页()

    def _on_header_clicked(self, 列号: int) -> None:
        """
        点表头按那一列排。**在内存那份上排**（`_重排`），不投活——点一次表头
        去问一次引擎就是"点一次付一次全表排序"。同一个列头点第二下反向。
        """
        if 列号 < 0 or 列号 >= len(self._全量列):
            return
        列名 = self._全量列[列号]
        if self._sort_column == 列名:
            self._sort_order = "DESC" if self._sort_order == "ASC" else "ASC"
        else:
            self._sort_column, self._sort_order = 列名, "ASC"
        self._page = 0
        self._重排()
        self._渲染页()

    def _apply_filter(self):
        """
        应用过滤。

        **这里是要重取一遍的**（过滤不是翻页）：框子里填的是霸下 `其中` 后面
        那一段条件原文，只有引擎认它。取回来的那份就是"过滤之后的全量"，
        翻页仍然在它上面切。
        """
        self._filter_text = self._filter_edit.text().strip(); self._page = 0
        if self.database_name and self.table_name:
            self._取全量()

    def _clear_filter(self):
        self._filter_edit.clear(); self._filter_text = ""; self._page = 0
        if self.database_name and self.table_name:
            self._取全量()

    # ===================================================================
    # 行 CRUD 操作
    # ===================================================================

    def _on_add_row(self) -> None:
        """在表格末尾添加空白行。（**只在表格里**，点了「保存新行」才进库。）"""
        if not self.database_name or not self.table_name:
            QMessageBox.information(self, "提示", "请先选择数据库和表")
            return
        if self._table.columnCount() == 0:
            return

        r = self._table.rowCount()
        self._table.insertRow(r)
        for c in range(self._table.columnCount()):
            item = QTableWidgetItem("NULL")
            item.setForeground(QColor("#94A3B8"))
            item.setFont(_null_item_font())
            item.setData(Qt.UserRole, None)
            self._table.setItem(r, c, item)
        self._table.editItem(self._table.item(r, 0))
        self._pending_new_rows.append(r)
        self._info_label.setText(f"新行已添加在第{r+1}行 — 填写数据后点击「保存新行」")
        self._info_label.setStyleSheet("color: #F59E0B; font-size: 12px;")

    def _on_delete_row(self) -> None:
        """删除选中的行。**一行一件活**（参照项目也是一行一条 `删除`）。"""
        rows = set(i.row() for i in self._table.selectedItems())
        if not rows:
            return QMessageBox.information(self, "提示", "请先用鼠标选中要删除的行（可多选）")

        # 过滤掉待保存的新行（直接移除即可）
        db_rows = [r for r in rows if r not in self._pending_new_rows]
        new_rows_to_remove = [r for r in rows if r in self._pending_new_rows]

        if db_rows:
            reply = QMessageBox.question(
                self, "确认删除",
                f"确定要删除数据库中 {len(db_rows)} 行数据吗？\n此操作不可撤销！",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        # 一件一件投。记事那个字典是几件活的公账：删成了几行、还有几件在外面飞。
        记事 = {'成': 0, '在飞': 0}
        for row in sorted(db_rows, reverse=True):
            if row >= len(self._页对):
                continue
            真值行, 显示行 = self._页对[row]
            where_text, 参 = self._主键条件(row)
            if not where_text:
                continue
            语句 = '删除 从 `%s` 其中 %s' % (self.table_name, where_text)
            记事['在飞'] += 1
            self.engine.发(
                语句,
                lambda 结果, 错, 显示行=显示行: self._收到删除(
                    结果, 错, 显示行, 记事),
                参)

        # 删除待保存的新行（没进过库，从格子上拿掉就算数）
        for row in sorted(new_rows_to_remove, reverse=True):
            self._table.removeRow(row)
            if row in self._pending_new_rows:
                self._pending_new_rows.remove(row)
            # 新行的下标跟着往上挪一格——不挪的话，接着「保存新行」会读到
            # 别的行，**把一行真数据当新行再插一遍**
            self._pending_new_rows = [
                (i - 1 if i > row else i) for i in self._pending_new_rows]
            记事['成'] += 1

        self._报删除数(记事)

    def _收到删除(self, 结果, 错, 显示行, 记事) -> None:
        """`删除` 的回话。**在主线程里被叫。**"""
        try:
            if 错 is not None:
                QMessageBox.warning(self, "删除失败", str(错))
                return
            if 结果 is None or not 结果.ok:
                QMessageBox.warning(self, "删除失败",
                                    "没有回结果" if 结果 is None else 结果.error)
                return
            if 结果.affected <= 0:
                QMessageBox.warning(self, "删除失败", "未找到要删除的行，数据可能已被修改。")
                return

            # 成了：内存里那一行也得去掉，不然翻页回来它还在。**按行对象认**——
            # 删了几行之后下标会挪位，拿下标去认会删错行。
            页内号 = None
            for i, r in enumerate(self._页行):
                if r is 显示行:
                    页内号 = i
                    break

            self._移除行(显示行)
            if 页内号 is not None:
                del self._页行[页内号]
                del self._页真值行[页内号]
                del self._页对[页内号]
                self._table.removeRow(页内号)
                self._pending_new_rows = [
                    (i - 1 if i > 页内号 else i) for i in self._pending_new_rows]
            记事['成'] += 1
            self._total_rows = len(self._全量行)
            self._报状态()
        finally:
            记事['在飞'] -= 1
            self._报删除数(记事)

    def _报删除数(self, 记事) -> None:
        """删除的活都回来了才报数（参照项目也是在收尾那处报一次）。"""
        if 记事['在飞'] > 0 or 记事['成'] <= 0:
            return
        self._info_label.setText(f"已删除 {记事['成']} 行")
        self._info_label.setStyleSheet("color: #16A34A; font-size: 12px;")

    def _移除行(self, 显示行) -> None:
        """把一行从内存那份里去掉（**按下标对齐的两份一起去**）。"""
        for i, r in enumerate(self._全量行):
            if r is 显示行:
                del self._全量行[i]
                del self._真值行[i]
                return

    def _on_save_new_rows(self) -> None:
        """通过 `插入` 保存所有新添加的行。**一行一件活**，回话到齐了再收尾。"""
        if not self._pending_new_rows:
            QMessageBox.information(self, "提示", "没有待保存的新行。请先点击「＋添加行」")
            return

        # 强制关闭当前编辑器，确保 IME 输入的中文已提交到 model
        idx = self._table.currentIndex()
        if idx.isValid():
            editor = self._table.indexWidget(idx)
            if editor:
                self._table.commitData(editor)
                self._table.closePersistentEditor(self._table.item(idx.row(), idx.column()))

        columns = [
            self._table.horizontalHeaderItem(c).text()
            for c in range(self._table.columnCount())
        ]
        # 公账：成了几行、哪几行败了、还有几件活在外面没回来
        记事 = {'成': 0, '败': [], '余': 0}
        for row in list(self._pending_new_rows):
            if row >= self._table.rowCount():
                self._pending_new_rows.remove(row)
                continue

            vals = []
            has_data = False
            for c in range(self._table.columnCount()):
                item = self._table.item(row, c)
                if not item:
                    vals.append(None)
                    continue
                t = item.text().strip()
                if t.upper() == "NULL" or t == "":
                    vals.append(None)       # 空值绑 None，不拼 "NULL"
                else:
                    vals.append(t)          # 值走 `?`，不拼字面量
                    has_data = True

            if not has_data:
                continue

            语句 = '插入 `%s` (%s) 值 (%s)' % (
                self.table_name,
                ', '.join(f'`{c}`' for c in columns),
                ', '.join('?' for _ in columns),
            )
            记事['余'] += 1
            self.engine.发(
                语句,
                lambda 结果, 错, row=row: self._收到新行(结果, 错, row, 记事),
                tuple(vals))

    def _收到新行(self, 结果, 错, row: int, 记事) -> None:
        """`插入` 的回话。**在主线程里被叫。**到齐了才收尾。"""
        if 错 is not None or 结果 is None or not 结果.ok:
            记事['败'].append(str(row + 1))
        else:
            记事['成'] += 1
            if row in self._pending_new_rows:
                self._pending_new_rows.remove(row)
        记事['余'] -= 1
        if 记事['余'] > 0:
            return

        if 记事['成'] > 0:
            self._info_label.setText(f"已保存 {记事['成']} 行新数据")
            self._info_label.setStyleSheet("color: #16A34A; font-size: 12px;")
            # 新行真进了库，重新取一遍（和参照项目一样：它这儿也调了 load_data）
            self.load_data()
        if 记事['败']:
            QMessageBox.warning(self, "部分失败", f"以下行保存失败: {', '.join(记事['败'])}")

    def _on_write_data(self) -> None:
        """打开表单对话框写入一行新数据。"""
        if not self.database_name or not self.table_name:
            QMessageBox.information(self, "提示", "请先选择数据库和表")
            return

        # 获取列信息
        try:
            db = self.engine.catalog.get_database(self.database_name)
            tbl = db.table(self.table_name)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法加载表结构: {e}")
            return

        dlg = InsertDataDialog(self.engine, self.database_name, self.table_name, tbl, self)
        if dlg.exec_():
            self._info_label.setText("数据写入成功！")
            self._info_label.setStyleSheet("color: #16A34A; font-size: 12px;")
            self.load_data()

    # ===================================================================
    # 全局保存数据库
    # ===================================================================

    def _on_save_database(self) -> None:
        """
        全局保存 —— **在霸下这一句是空转**。

        控件、文案、tooltip、快捷键全照参照项目留着（用户要求别删）。但底下
        这条路霸下没有：`引擎.save_all()` 是个空壳，返回 `{'errors': []}`；
        霸下每条语句写完就落盘了，没有"攒着再存"这一步。所以参照项目那句
        "数据库已保存 — …"在霸下是句假话，这里改成说实话的那一句。
        （要照旧说那句假的，改下面 `else:` 里那两行即可。）
        """
        save_btn = self.findChild(QPushButton, "save_db_btn")
        if save_btn:
            save_btn.setEnabled(False)
            save_btn.setText("保存中...")

        try:
            result = self.engine.save_all()

            # 汇总结果
            parts = []
            for label, key in [("Catalog", "catalog"), ("用户", "users"),
                              ("配置", "config"), ("存储", "storage")]:
                status = result.get(key, "-")
                if status is True:
                    parts.append(f"{label} ✓")
                elif status is False:
                    parts.append(f"{label} ✗")

            if result["errors"]:
                err_detail = "\n".join(result["errors"])
                self._info_label.setText(f"保存部分失败: {' '.join(parts)}")
                self._info_label.setStyleSheet("color: #F59E0B; font-size: 12px;")
                QMessageBox.warning(
                    self, "全局保存 — 部分失败",
                    f"以下模块保存失败:\n\n{err_detail}\n\n"
                    f"已成功: {' '.join(p for p in parts if '✓' in p)}"
                )
            else:
                # **这一句和参照项目不一样，是有意的。** 霸下没有可保存的东西：
                # 每条语句一交出去就落盘了。照原样说"数据库已保存"就是撒谎。
                self._info_label.setText(
                    "霸下没有『保存数据库』这一步（每条语句写完就落盘了）——这个按钮是空转")
                self._info_label.setStyleSheet("color: #64748B; font-size: 12px;")
                # 在主窗口状态栏显示一句实话
                mw = self.window()
                if hasattr(mw, 'status') and mw.status:
                    mw.status.showMessage("霸下：数据早已落盘，没有需要保存的东西", 3000)
        except Exception as e:
            self._info_label.setText(f"全局保存失败: {e}")
            self._info_label.setStyleSheet("color: #DC2626; font-size: 12px;")
            QMessageBox.critical(self, "全局保存失败", str(e))
        finally:
            if save_btn:
                save_btn.setEnabled(True)
                save_btn.setText("💾 保存数据库")

    # ===================================================================
    # 右键菜单
    # ===================================================================

    def _on_context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("复制单元格 (Ctrl+C)", self._copy_cell)
        menu.addAction("复制整行", self._copy_row)
        menu.addSeparator()
        menu.addAction("写入数据 (表单)", self._on_write_data)
        menu.addAction("＋ 插入空行", self._on_add_row)
        menu.addAction("－ 删除选中行", self._on_delete_row)
        menu.addSeparator()
        menu.addAction("设为 NULL", self._set_null)
        menu.addSeparator()
        menu.addAction("刷新 (F5)", self._on_refresh_all)
        menu.addAction("导出当前页到 CSV...", self._export_csv)
        menu.exec_(self._table.viewport().mapToGlobal(pos))

    def _copy_cell(self) -> None:
        from PySide6.QtWidgets import QApplication
        items = self._table.selectedItems()
        if items:
            QApplication.clipboard().setText(items[0].text())

    def _copy_row(self) -> None:
        from PySide6.QtWidgets import QApplication
        rows = set(i.row() for i in self._table.selectedItems())
        if not rows:
            return
        row = min(rows)
        vals = [
            self._table.item(row, c).text() if self._table.item(row, c) else ""
            for c in range(self._table.columnCount())
        ]
        QApplication.clipboard().setText("\t".join(vals))

    def _set_null(self) -> None:
        """把选中的格子设成空。改完由 `_on_cell_changed` 投活去落盘。"""
        for item in self._table.selectedItems():
            item.setText("NULL")
            item.setForeground(QColor("#94A3B8"))
            item.setFont(_null_item_font())
            item.setData(Qt.UserRole, None)

    def _export_csv(self) -> None:
        """
        导出 CSV。

        **从内存里那一份导**（当前页那一段），一件活都不投——参照项目也是从
        表里读的，只是它读的是格子上那串字。这里读的是**真值**：
        二进制导出成 `repr`（界面上那串 `<N 字节>` 导出去等于没导），时间列
        导出成格式化文本（真值是毫秒数）。
        """
        if not self.database_name or not self.table_name:
            QMessageBox.information(self, "提示", "请先选择数据库和表")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", os.getcwd(), "CSV 文件 (*.csv);;所有文件 (*.*)")
        if not path:
            return
        try:
            # `utf-8-sig` = BOM + UTF-8，Excel 才不会把中文认成一堆乱码
            # （参照项目那句 `safe_write_text(..., bom=True)` 干的就是这个）
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(list(self._全量列))
                for i, 显示行 in enumerate(self._页行):
                    真值行 = self._页真值行[i] if i < len(self._页真值行) else []
                    w.writerow([
                        _导出文本(真值行[c][0] if c < len(真值行) else None,
                                 真值行[c][1] if c < len(真值行) else 值)
                        for c, 值 in enumerate(显示行)
                    ])
            QMessageBox.information(self, "完成", f"已导出到:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # ===================================================================
    # 刷新
    # ===================================================================

    def refresh(self) -> None:
        """
        公开的刷新口（无参数）：主窗的全局刷新和 `data_changed` 都叫这一句。

        语义就是"**重新投活把当前表全量取一遍**"（顺带换一遍目录列表）。
        **投完就返回**——这里不泵事件、不循环等结果，回话在主线程里到。
        """
        self._on_refresh_all()


# ===================================================================
# 写入数据对话框
# ===================================================================

class InsertDataDialog:
    """
    写入数据表单对话框 — 为每个列生成输入控件，一次写入一行。

    **界面原样照抄**（标题、提示、标签、按钮、样式、`setMinimumWidth` /
    `setMinimumHeight` 一个没动）。底下那一步从"当场拼 INSERT 再同步执行"
    换成"投一件活 + 回话"：按下「写入」不会立刻关窗，等引擎回话了才关
    ——回话是在 `exec_()` 那个嵌套事件循环里被叫的，窗口这边看着跟原来一样。
    """

    def __init__(self, engine, db_name: str, table_name: str, table, parent=None):
        self.engine = engine
        self.db_name = db_name
        self.table_name = table_name
        self.table = table
        self._inputs: list[tuple[str, QLineEdit]] = []
        self._dlg = self._build(parent)

    def _build(self, parent) -> QDialog:
        dlg = QDialog(parent)
        dlg.setWindowTitle(f"写入数据 — {self.db_name}.{self.table_name}")
        dlg.setMinimumWidth(520)
        dlg.setMinimumHeight(400)
        dlg.setModal(True)

        main_layout = QVBoxLayout(dlg)

        # 标题
        title = QLabel(f"向 {self.db_name}.{self.table_name} 写入一行新数据")
        title.setStyleSheet("font-size: 14px; font-weight: bold; padding: 8px 0;")
        main_layout.addWidget(title)

        hint = QLabel("填写各列的值，留空或输入 NULL 表示空值。自增列无需填写。")
        hint.setStyleSheet("color: #64748B; font-size: 11px; padding-bottom: 4px;")
        hint.setWordWrap(True)
        main_layout.addWidget(hint)

        # 可滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        form_container = QWidget()
        form = QFormLayout(form_container)
        form.setLabelAlignment(Qt.AlignRight)

        for col in self.table.columns:
            label_text = col.name
            extras = []
            if col.primary_key:
                extras.append("主键")
            if not col.nullable:
                extras.append("必填")
            # 霸下不做自增（`界面_接驳.列` 里这一项恒为 False），所以这一格
            # 永远不进；留着是因为列模型上有这个名，别的地方读法一致。
            if col.auto_increment:
                extras.append("自增")
            if extras:
                label_text += f"  [{', '.join(extras)}]"

            # 参照项目这里写的是 `col.sql_type.to_sql()`；霸下的列模型上
            # `sql_type` 已经是那个类型名（`整数` / `文本` …），没有 to_sql。
            type_hint = f"类型: {col.sql_type}"

            edit = QLineEdit()
            edit.setPlaceholderText(type_hint)
            # 确保中文输入法正常工作
            edit.setInputMethodHints(Qt.InputMethodHint.ImhNone)

            if col.auto_increment:
                # 自增列默认留空
                edit.setPlaceholderText(f"{type_hint} — 自动生成，无需填写")
                edit.setStyleSheet("background: #F1F5F9;")
                edit.setReadOnly(True)
            elif col.default_value() is not None:
                # **这一处和参照项目不一样，是有意的**：它写的是
                # `col.default_value`（没加括号），那是**方法对象**，永远不是
                # None——于是每一列都被塞进一句 "<bound method …>"。
                # 霸下的列模型上 `default_value()` 才是那个默认值。
                default_text = str(col.default_value())
                edit.setText(default_text)
                edit.setPlaceholderText(f"{type_hint} — 默认值: {default_text}")

            label_widget = QLabel(label_text)
            form.addRow(label_widget, edit)
            self._inputs.append((col.name, edit))

        scroll.setWidget(form_container)
        main_layout.addWidget(scroll, 1)

        # 按钮 — 使用独立按钮避免 IME 组合输入被 QDialogButtonBox 的自动 Enter 拦截
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("写入")
        ok_btn.setStyleSheet("QPushButton { font-weight: bold; color: #2563EB; }")
        ok_btn.clicked.connect(lambda: self._on_accept(dlg))
        btn_layout.addWidget(ok_btn)

        main_layout.addLayout(btn_layout)

        return dlg

    def exec_(self) -> bool:
        """显示对话框，返回是否成功写入。"""
        return self._dlg.exec_() == QDialog.DialogCode.Accepted

    def _on_accept(self, dlg: QDialog) -> None:
        """收集输入并**投一件活**执行 `插入`（不进不等，回话在主线程里到）。"""
        # 收集非自增列的值
        col_names = []
        col_values = []
        for col_name, edit in self._inputs:
            # 跳过自增列
            col = self.table.column(col_name)
            if col and col.auto_increment:
                continue

            text = edit.text().strip()
            if text == "" or text.upper() == "NULL":
                if not col.nullable and not col.primary_key:
                    QMessageBox.warning(dlg, "输入错误",
                                        f"列 '{col_name}' 为必填项（NOT NULL），请填写值。")
                    return
                col_names.append(col_name)
                col_values.append(None)     # 空值绑 None，不拼 "NULL"
            else:
                col_names.append(col_name)
                col_values.append(text)     # 值走 `?`，不拼字面量

        if not col_names:
            QMessageBox.warning(dlg, "输入错误", "没有可写入的列。")
            return

        语句 = '插入 `%s` (%s) 值 (%s)' % (
            self.table_name,
            ', '.join(f'`{c}`' for c in col_names),
            ', '.join('?' for _ in col_names),
        )

        self.engine.发(
            语句,
            lambda 结果, 错: self._收到写入(dlg, 结果, 错),
            tuple(col_values))

    def _收到写入(self, dlg: QDialog, 结果, 错) -> None:
        """
        `插入` 的回话。**在主线程里被叫**（`exec_()` 那个嵌套事件循环里）。

        参照项目在这儿还有一段"存储层异常 → 自动诊断修复 → 重试"：那是它
        存储层的东西（`engine.storage_engine.repair_manager`），**霸下没有
        这一层**——所以那一段删了，错误一律原样摆出来。
        """
        if 错 is not None:
            QMessageBox.critical(dlg, "写入失败", f"数据写入失败:\n{错}")
            return
        if 结果 is None:
            QMessageBox.critical(dlg, "写入失败", "数据写入失败:\n没有回结果")
            return
        if not 结果.ok:
            # 引擎的中文原话，照原样显示
            QMessageBox.critical(dlg, "写入失败", f"数据写入失败:\n{结果.error}")
            return
        dlg.accept()
