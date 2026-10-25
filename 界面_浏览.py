#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面_浏览.py — 数据库浏览器面板（搬自 `baxiadb-v1.6/config_gui/ui/db_explorer.py`）

左侧 库/表/列 一棵树，右侧"对象详情 + 数据预览"。类名 `DBExplorerPanel` 一个字
没动，布局、控件、文案（含 emoji 和全角符号）、颜色、tooltip、快捷键、确认框
措辞、列头文字、按钮顺序、`setMinimumWidth` 全部照抄。**改的只有底下那条数据
通路**——参照项目那套东西霸下这边没有：

    参照项目                              霸下这边
    ─────────────────────────────────    ──────────────────────────────
    Planner / Optimizer / Executor        → 引擎.取 / 发 / 取多 / 发多（投活）
    _exec_sql(engine, sql)                → 删掉了，一次不留
    SELECT * FROM `t` LIMIT 100           → `查询 * 从 `t` 限制 100`
    UPDATE t SET c='v' WHERE pk='1'       → `更新 `t` 设 `c` = 'v' 其中 `pk` = 1`
    DELETE FROM t WHERE pk='1'            → `删除 从 `t` 其中 `pk` = 1`
    INSERT INTO t (...) VALUES (...)      → `插入 `t` (...) 值 (...)`
    catalog.drop_table + catalog.save()   → `删表 `t`` + 目录.刷新
    _escape_sql_value（反斜杠转义）        → 界面_设计表.字面量（单引号写成两个）
    is_alive / safe_stop_timer            → safe_stop_timer 从界面零件拿；is_alive 见下

五处要说明白的地方
==================

1. **取数一律投活，不阻塞。** 参照项目是同步 `_exec_sql`——在霸下这边照抄就是把
   窗口冻住（`计数(*)` 是全表扫，预览那 100 行也要过闸口）。所以 `取 / 发 / 发多`
   都是"投完就回去重绘"，回话在**主线程**里被叫。

2. **`refresh()` 是个公开的无参方法，而且不阻塞**（主窗那边的全局刷新和
   `data_changed` 都连它）。它做两件事：先拿手里这份**目录缓存**把树重画一遍
   （读的全是内存，当场就画出来了），再把"拉一份新目录"投给工人；目录回来了
   再画一遍。所以那个 2 秒的定时器不会把界面卡住，`_select_item_by_name` 在
   对话框返回之后也照样能定位——对话框是**先刷新目录再关**的。

3. **预览格的编辑是"真值"驱动的。** 格子里显示的是 `schema.render` 渲染出来的
   文本（和 `结果.as_lists()` 同一把尺子），**真值（`(类型号, 值)` 里那个值）塞在
   `Qt.UserRole` 里**：比"变没变"、拼条件、回写用的都是它。拿显示文本当值用会
   踩到静默错答案——时间列的显示是 `2026-09-13 10:00:00` 那样的串，真值是毫秒
   整数；文本列显示 `5` 真值是 `'5'`。

4. **改一格的 `其中` 用的是格子里的老真值。** 正在改的那一格**自己就是主键**时
   尤其是命门：条件里写新值的话，那条 `更新` 一行也匹配不上，而界面会绿给你看
   ——"存进去了"和"什么都没发生"长得一模一样。所以条件是先拼好再发的，回写
   `UserRole` 是**成功了以后**的事。

5. **二进制格子只读。** 它渲染出来是 `b'\x00…'` 那串样子，按文本存回去存的是
   另一串字节。所以那一格不给编辑（显示 `<N 字节>`，和 `界面零件.ResultTable`
   一个写法），`_on_preview_cell_changed` 里另有一道拦——`设为 NULL` 那条路是
   走的（空值对任何类型都是空值）。

做不到 / 只能摆样子的，如实列在这儿
====================================

* **新建库 / 删库**：**两样都是真做的**（见下）。参照项目那套多库是靠
  `engine.catalog`，搬到霸下时收成了单库；后来解绑了——**库 = 一块永久存储
  区 = 一个根目录（一个 `catalog` 加一个 `r/`）**，同一个父目录下可以摆好几
  块。面板这一侧能建、能删、能切。
* **一次只开一个库。** 树上一眼看得到好几个库，但引擎那头**同一时刻只有一个
  Arena**（写者只有一个，见 `storage.py:31-35`）。所以别的库画成"未打开"的
  灰样子，**双击才切过去**——切库是整条腿重建，不是把这一个也开着。
* **完整数据查看器**：参照项目那个 `data_viewer` 是另一个界面文件，没搬过来。
  菜单项留着，点了说实话。
* **注释 / 字符集 / 排序规则 / 自增 / 唯一**：霸下没有这几样，树里和详情里显示的
  是诚实的空值（不是"关着"）。
* `database_changed` 这个信号**参照项目里就声明了没发过**，这边也一样——它一次
  都不发。主窗要跟进的话，请连 `refresh()`。
"""

from __future__ import annotations

import logging
import os
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QPushButton, QLabel, QMenu, QMessageBox, QSplitter, QTextEdit,
    QHeaderView, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QInputDialog, QDialog, QDialogButtonBox, QListWidget, QListWidgetItem,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QKeySequence,
                           QShortcut)

from 界面_设计表 import CreateTableDialog, EditTableDialog, 字面量
import 界面零件
from 界面零件 import safe_stop_timer

import schema as _schema
import 备份

_log = logging.getLogger(__name__)


# ══ 一、几个小工具 ════════════════════════════════════════════════════
#
# 参照项目这几样是从 `baxia.sql.*` 和 `config_gui.utils.lifecycle` 拿的；
# 霸下这边：语句是引擎自己的事（不用拼 SQL 字符串了），lifecycle 里有用的
# 只剩这一个"这个 C++ 对象还在吗"。


def is_alive(obj) -> bool:
    """这个 Qt 对象还在吗。

    参照项目的 `config_gui.utils.lifecycle` 里有一整套，这边只用到这一个。
    面板被 Qt 删掉之后定时器还会响一次，那时候 `self._tree` 已经是个空壳，
    碰它就抛 RuntimeError——照着参照项目那句 `if not is_alive(self._tree)`
    原样留着，是为了那个分支还在。
    """
    if obj is None:
        return False
    try:
        import shiboken6
    except Exception:            # 拿不到就当它活着，别把刷新整个掐掉
        return True
    try:
        return shiboken6.isValid(obj)
    except Exception:
        return True


def _get_table_pk(engine, db_name: str, table_name: str) -> list[str]:
    """表的主键列名。**读的是目录缓存**（同步读，不卡）。

    主键编在每一行的行键里，表一定下来主键就定了，不用去问引擎。参照项目
    那边是 `db.table(name).primary_key`，这边的模型上也是这个名字。
    """
    try:
        db = engine.catalog.get_database(db_name)
        return db.table(table_name).primary_key
    except Exception:
        return []


def _get_table_pk_remote(client, db_name: str, table_name: str) -> list[str]:
    """表的主键列名（远程：问服务端）。照抄，走不到——同 `_refresh_remote`。"""
    try:
        result = client.query(
            f"SHOW KEYS FROM `{db_name}`.`{table_name}` "
            f"WHERE Key_name = 'PRIMARY'"
        )
        if result.get("error"):
            return []
        pk_cols = []
        for row in result.get("rows", []):
            col = row.get("Column_name") or row.get("column_name", "")
            if col:
                pk_cols.append(col)
        return pk_cols
    except Exception:
        return []


class 挑旧份框(QDialog):
    """
    备份之前问一句：**旧的那几份，有哪几份不要了。**

    ⛔ **这个框只能在主线程上开。** 备份那件活是跑在工人线程上的
    （`工位.干`），在那边开 Qt 对话框是不作兴的——所以分法是：**界面线程
    先把"要删哪些"问清楚，再把这个答案当参数投进活里**。反过来做（活跑到
    一半回头问界面）在 Qt 里是行不通的。

    **默认一份都不勾。** 删备份不可撤销，而勾选框界面最容易出的就是"随手
    全勾上再点确定"；默认不勾的话，最坏结果只是"这份没删成、下次再删一遍"，
    而不是"少了两份备份"。

    这里**只是问**，一个字都不删。真删在 `备份.删几份`，而它是在**新那份
    备份拷好、也验过之后**才被叫的。
    """

    def __init__(self, 旧份, 新份名, parent=None):
        super().__init__(parent)
        self.setWindowTitle('备份 —— 旧的那几份还留着吗')
        self.setModal(True)
        self.setMinimumWidth(470)

        布局 = QVBoxLayout(self)
        说明 = QLabel(
            '新的这一份会存成：\n%s\n\n'
            '下面是这个库已经有的旧备份。**勾上的会在新那份备好之后删掉**'
            '——新的没成，一份都不动。\n\n'
            '**默认一个都没勾**，也就是全都留着。' % 新份名)
        说明.setWordWrap(True)
        说明.setStyleSheet('color: #64748B;')
        布局.addWidget(说明)

        self._清单 = QListWidget()
        for 名, 路, 时刻, 字节 in 旧份:
            尾巴 = ('　[%s]' % time.strftime('%Y-%m-%d %H:%M:%S',
                                            time.localtime(时刻))
                    if 时刻 is not None else '　[名字认不出时间]')
            条 = QListWidgetItem('%s%s　%s' % (名, 尾巴, 备份.字节文案(字节)))
            条.setFlags(条.flags() | Qt.ItemIsUserCheckable)
            条.setCheckState(Qt.Unchecked)
            条.setData(Qt.UserRole, 路)
            条.setToolTip(路)
            self._清单.addItem(条)
        布局.addWidget(self._清单)

        按钮 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        按钮.button(QDialogButtonBox.Ok).setText('就按这个来')
        按钮.button(QDialogButtonBox.Cancel).setText('先不备了')
        # 回车落到「先不备了」上：回车一下不该删掉几份备份。要删得用鼠标
        # 亲手点那个写着"就按这个来"的按钮。
        按钮.button(QDialogButtonBox.Ok).setAutoDefault(False)
        按钮.button(QDialogButtonBox.Cancel).setDefault(True)
        按钮.accepted.connect(self.accept)
        按钮.rejected.connect(self.reject)
        布局.addWidget(按钮)

    def 要删的(self):
        """勾上的那几份的完整路径。"""
        return [self._清单.item(i).data(Qt.UserRole)
                for i in range(self._清单.count())
                if self._清单.item(i).checkState() == Qt.Checked]


class DBExplorerPanel(QWidget):
    """数据库浏览器 -- 左侧树 + 右侧详情面板。

    支持两种模式:
      - 本地模式: 使用 Engine.catalog 直接访问
      - 远程模式: 使用 SHOW DATABASES / SHOW TABLES / SHOW COLUMNS 等 SQL 查询
                    （霸下这边只有"一个内核一个库"，主窗永远传 client=None，
                     远程那一半照抄留着，但走不到）
    """

    database_changed = Signal(str)  # 当选择数据库时发出

    DB_ITEM = 1000
    TABLE_ITEM = 1001
    COLUMN_ITEM = 1002

    def __init__(self, engine=None, client=None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._client = client
        self._is_remote = client is not None
        # 预览查询的代次 + 每列的**类型号**（`(类型号, 值)` 里那个号）。
        # 这两样都是霸下这边才需要的状态；放在 `_setup_ui()` 之前，是因为
        # `_setup_ui()` 末尾那一句 `self.refresh()` 就要用到它们。
        self._预览号 = 0
        self._预览类型: list[int] = []
        # 备份那两样（见 `手动备份`）：**正在备份**是个闸——主窗关软件那一路
        # 要拿它拦人（备着的时候关窗口会留半份备份、还会把工人线程掐断）。
        self._备份中 = False
        self._备份前听着 = False        # 备份前 MySQL 口在不在听，完事照这个还原
        self._备份起钟 = 0.0            # 这一份是什么时候开的工，报耗时用
        self._setup_ui()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._safe_refresh)
        self._refresh_timer.start(2000)  # 2s real-time refresh
        self.destroyed.connect(lambda: safe_stop_timer(self._refresh_timer))

        # Keyboard shortcuts
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._on_create_database)
        QShortcut(QKeySequence("Ctrl+T"), self, activated=self._on_create_table)
        QShortcut(QKeySequence("Ctrl+E"), self, activated=self._on_edit_table)
        QShortcut(QKeySequence("Delete"), self, activated=self._on_drop)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.refresh)

    # --------------- 库管不管得着 ---------------

    @property
    def _可管库(self) -> bool:
        """
        这台机器上管不管得了库（新建 / 切换 / 删除）。

        **判据是"库在不在这台机器上"，不是形态。** 三种腿：

            `--库` 开本机库        库文件路径有值 → 管得了
            `--连` 到别人那台      库文件路径是 None → 管不了
            本机库 + 自起服务       **形态是"远程"，但库就在本机** → 管得了

        第三种最容易看错：它进出走 socket，看着像远程，其实库是本机上那个
        真实路径（`霸下之躯.开库` 里换腿时 `路` 没动）。按形态判的话，配置
        里勾了「开界面就起服务」的人会发现"新建库"莫名其妙灰着。

        环回那条腿切库要停掉旧库的服务、给新库重新起——那是 `开库` 自己
        的事，这一层不用管。
        """
        return getattr(self.engine, '库文件路径', None) is not None

    def _选中库开着(self) -> bool:
        """
        树上**选中的那个库**是不是正开着的那个。

        多库之后，"选中的库"和"引擎手上那个库"是两个东西：树上摆着好几个
        库，引擎同一时刻只开着其中一个（`storage.py:31-35` 那条单写者）。
        凡是**往库里写**的动作都得先问一句，不然后果是**写错库**——语句发给
        引擎，落在开着的那个库上，而界面上显示的是选中的那个名字。

        选中态是表或列时，顺着树往上找到它那个库再问。
        """
        名 = self._get_selected_database()
        if not 名:
            return True                      # 没选中什么，不拦
        for db in self.engine.catalog.databases:
            if db.name == 名:
                return not bool(getattr(db, '未打开', False))
        return True                          # 目录里没这个名，交给引擎去报错

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 标题栏
        header = QHBoxLayout()
        title = QLabel("数据库浏览器")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()

        refresh_btn = QPushButton("刷新")
        refresh_btn.setToolTip("刷新数据库列表 (F5)")
        refresh_btn.setMinimumWidth(60)
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        # 按钮栏
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        self._create_db_btn = QPushButton("＋ 新建库")
        self._create_db_btn.setToolTip("新建一个库（一块新的永久存储区）(Ctrl+N)")
        self._create_db_btn.setMinimumWidth(90)
        self._create_db_btn.clicked.connect(self._on_create_database)
        self._create_db_btn.setStyleSheet(
            "QPushButton { font-weight: bold; color: #2563EB; }"
        )
        # **建得了库才亮**：库在这台机器上（`_可管库`）就亮，`--连` 到别人
        # 那台就灰着（库在对面，这边连人家有几个库都不知道）。
        self._create_db_btn.setEnabled(self._可管库)
        btn_layout.addWidget(self._create_db_btn)

        self._create_table_btn = QPushButton("＋ 新建表")
        self._create_table_btn.setToolTip("创建新表 (Ctrl+T)")
        self._create_table_btn.setMinimumWidth(90)
        self._create_table_btn.clicked.connect(self._on_create_table)
        self._create_table_btn.setEnabled(False)
        btn_layout.addWidget(self._create_table_btn)

        self._edit_table_btn = QPushButton("编辑结构")
        self._edit_table_btn.setToolTip("编辑表结构 (Ctrl+E)")
        self._edit_table_btn.setMinimumWidth(90)
        self._edit_table_btn.clicked.connect(self._on_edit_table)
        self._edit_table_btn.setEnabled(False)
        btn_layout.addWidget(self._edit_table_btn)

        self._drop_btn = QPushButton("删除")
        self._drop_btn.setToolTip("删除选中的库或表 (Del)")
        self._drop_btn.setMinimumWidth(70)
        self._drop_btn.clicked.connect(self._on_drop)
        self._drop_btn.setEnabled(False)
        self._drop_btn.setStyleSheet(
            "QPushButton { color: #DC2626; }"
        )
        btn_layout.addWidget(self._drop_btn)

        self._backup_btn = QPushButton("🗄 备份当前库")
        self._backup_btn.setToolTip(
            "把这个库整个复制一份存到库目录下的 `备份/` 里。\n"
            "**备的是当前开着的这个库**，不是树上选中的那个。\n"
            "备份期间 MySQL 口会先停一下，备完自动开回来。")
        self._backup_btn.setMinimumWidth(110)
        # **必须包一层 lambda**：`clicked` 带一个 `checked` 参数，而
        # `手动备份` 头一个形参是 `说明`——直接连上去的话，按钮一点就把
        # `False` 当"说明"塞进去了，成功提示的开头会多出一行 `False`。
        # （`_on_create_database` 那几条能直连，是因为它们一个形参都不收，
        #   PySide 看签名就不传那个 `checked`。）
        self._backup_btn.clicked.connect(lambda: self.手动备份())
        # **库在这台机器上才亮**：`--连` 到别人那台，库在对面那个进程里，
        # 这边拷不了（和「新建库」同一条判据，见 `_可管库`）。
        self._backup_btn.setEnabled(self._可管库)
        btn_layout.addWidget(self._backup_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 分割器：左树右详情
        splitter = QSplitter(Qt.Horizontal)

        # ---- 左侧：数据库树 ----
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["名称", "类型", "行数/值"])
        self._tree.setColumnWidth(0, 200)
        self._tree.setColumnWidth(1, 80)
        self._tree.setColumnWidth(2, 100)
        self._tree.setAlternatingRowColors(True)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.setMinimumWidth(300)
        splitter.addWidget(self._tree)

        # ---- 右侧：详情面板 ----
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(4, 4, 4, 4)

        detail_title = QLabel("对象详情")
        detail_title.setStyleSheet("font-size: 14px; font-weight: bold;")
        right_layout.addWidget(detail_title)

        self._detail_text = QTextEdit()
        self._detail_text.setReadOnly(True)
        self._detail_text.setFont(QFont("Consolas", 10))
        self._detail_text.setPlaceholderText("选择左侧的数据库或表查看详情，点击数据表格单元格即可编辑...")
        right_layout.addWidget(self._detail_text)

        # 数据预览表
        preview_header = QHBoxLayout()
        preview_label = QLabel("数据预览（前100行）— 点击单元格即可编辑")
        preview_label.setStyleSheet("font-weight: bold; padding-top: 8px;")
        preview_header.addWidget(preview_label)
        preview_header.addStretch()

        self._preview_add_btn = QPushButton("＋ 添加行")
        self._preview_add_btn.setToolTip("添加空白行")
        self._preview_add_btn.clicked.connect(self._on_preview_add_row)
        self._preview_add_btn.setMinimumWidth(80)
        self._preview_add_btn.setMaximumHeight(26)
        self._preview_add_btn.setVisible(False)
        preview_header.addWidget(self._preview_add_btn)

        self._preview_del_btn = QPushButton("－ 删除行")
        self._preview_del_btn.setToolTip("删除选中行")
        self._preview_del_btn.clicked.connect(self._on_preview_del_row)
        self._preview_del_btn.setMinimumWidth(80)
        self._preview_del_btn.setMaximumHeight(26)
        self._preview_del_btn.setStyleSheet("QPushButton { color: #DC2626; }")
        self._preview_del_btn.setVisible(False)
        preview_header.addWidget(self._preview_del_btn)

        self._preview_save_btn = QPushButton("保存新行")
        self._preview_save_btn.setToolTip("保存新添加的行到数据库")
        self._preview_save_btn.clicked.connect(self._on_preview_save_row)
        self._preview_save_btn.setMinimumWidth(80)
        self._preview_save_btn.setMaximumHeight(26)
        self._preview_save_btn.setStyleSheet("QPushButton { color: #16A34A; font-weight: bold; }")
        self._preview_save_btn.setVisible(False)
        preview_header.addWidget(self._preview_save_btn)

        right_layout.addLayout(preview_header)

        self._data_table = QTableWidget()
        self._data_table.setAlternatingRowColors(True)
        self._data_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        # 单击即可编辑：点击已选中的单元格立即进入编辑模式
        self._data_table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.EditKeyPressed |
            QAbstractItemView.AnyKeyPressed
        )
        self._data_table.itemChanged.connect(self._on_preview_cell_changed)
        self._data_table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self._data_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._data_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._data_table.customContextMenuRequested.connect(self._on_preview_context_menu)
        self._data_table.setToolTip("单击选中单元格后再单击即可编辑；按F2或直接输入也可开始编辑；右键查看更多操作")
        right_layout.addWidget(self._data_table)

        # Track current selection context for editing
        self._preview_db_name = ""
        self._preview_table_name = ""
        self._preview_pk: list[str] = []
        self._preview_columns: list[str] = []
        self._preview_new_row = -1

        splitter.addWidget(right_widget)
        splitter.setSizes([350, 650])
        layout.addWidget(splitter, 1)

        # 初始加载
        self.refresh()

    def _safe_refresh(self) -> None:
        """定时器回调 -- 仅在面板可见时刷新，防止交互中清空。"""
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        """刷新数据库树。

        **公开、无参、不阻塞。**（主窗那边的全局刷新和 `data_changed` 连的就是
        这个方法，签名叫 `widget.refresh()`。）里面干两件事：

          1. 拿手里这份**目录缓存**把树重画一遍——读的全是内存，当场就画完；
          2. 把"拉一份新目录"投给工人，目录回来了再画一遍。

        所以这个方法是"投活就返回"，不 `processEvents`、不转圈等。参照项目
        那句 `blockSignals` 防重入照旧——重画树的时候别让点击事件再回打进来。
        """
        self._重扫库清单()
        self._画树()
        if not self._is_remote:
            # 远程那一半是 SQL 现查的，没有一份缓存可刷（见文件头）。
            self.engine.catalog.刷新(self._收目录)

    def _重扫库清单(self) -> None:
        """
        重新数一遍库目录。

        **摆在 `refresh()` 头上**，所以那个 2 秒的定时刷新也带着它——于是
        有人拿资源管理器在库目录里删一个、拷一个进来，树上两秒内自己跟上。
        这份清单是**盘的影子**，不是一份自己维护的账：影子就该定期跟盘对
        一次，对不上以盘为准。

        数的动作在装配层（`霸下之躯.扫库`），这一层只管把结果递给目录。
        远程腿没有这个口（`扫库` 是装配层挂上来的，远程腿没挂）。
        """
        扫 = getattr(self.engine, '扫库', None)
        if 扫 is None:
            return
        try:
            self.engine.catalog.设清单(扫())
        except Exception as 错:
            # 数不动就当没数到——树上少几个库，比整个刷新炸掉强。
            _log.debug("库清单重扫失败: %s", 错)

    def _收目录(self, 错) -> None:
        """目录刷新完了（**在主线程里被叫**）。拉到了就再画一遍。"""
        if 错 is None:
            self._画树()

    def _画树(self) -> None:
        """用手里这份目录把树重画一遍（同步，读的全是内存）。"""
        if not is_alive(self._tree):
            return
        self._tree.blockSignals(True)
        try:
            self._tree.clear()
            if self._is_remote:
                self._refresh_remote()
            else:
                self._refresh_local()
        except Exception as e:
            _log.debug("DB explorer refresh skipped: %s", e)
        finally:
            self._tree.blockSignals(False)

    def _refresh_local(self) -> None:
        """本地模式：使用 Engine.catalog 刷新树。

        读法跟参照项目一模一样；变的是底下那份目录：它现在是**一份缓存**，
        由 `目录.刷新` 在工人线程里装配好、整份换掉（见 `界面_接驳.目录`）。
        """
        for db in self.engine.catalog.databases:
            db_item = QTreeWidgetItem(self._tree)
            db_item.setText(0, db.name)
            db_item.setText(1, "数据库")
            # **没打开的那个不能写"0 张表"。** 它的表这次根本没去看（一个
            # 存储区同一时刻只开得起一个），写 0 就是在说"这个库是空的"。
            # 写"未打开"，再配一句双击能切过去的话。
            未开 = bool(getattr(db, '未打开', False))
            db_item.setText(2, "未打开" if 未开 else f"{db.table_count} 张表")
            db_item.setData(0, Qt.UserRole, self.DB_ITEM)
            db_item.setData(0, Qt.UserRole + 1, db.name)
            # 盘上那条路径——切库、删库都靠它认人（库名只是给人看的，
            # 两个库同名是可能的）。
            db_item.setData(0, Qt.UserRole + 3, getattr(db, '路', None))
            db_item.setData(0, Qt.UserRole + 4, 未开)
            if 未开:
                db_item.setToolTip(
                    0, '这个库这次没打开。**双击切过去**就能看它的表。')
            else:
                db_item.setToolTip(0, db.comment if db.comment else db.name)

            # 加粗数据库项（开着的那个用蓝底标出来，一眼看得出在哪个库里）
            font = db_item.font(0)
            font.setBold(True)
            db_item.setFont(0, font)
            if not 未开:
                db_item.setForeground(0, QBrush(QColor("#2563EB")))

            # 添加表
            # （参照项目写的是 `for table in db:`，霸下的 `库` 没有迭代器，
            #   给的是 `tables` 这个列表。读出来的东西一模一样。）
            for table in db.tables:
                table_item = QTreeWidgetItem(db_item)
                table_item.setText(0, table.name)
                table_item.setText(1, "表")
                table_item.setText(2, f"{table.column_count} 列")
                table_item.setData(0, Qt.UserRole, self.TABLE_ITEM)
                table_item.setData(0, Qt.UserRole + 1, db.name)
                table_item.setData(0, Qt.UserRole + 2, table.name)
                table_item.setToolTip(0, table.comment if table.comment else table.name)

                # 添加列
                for col in table.columns:
                    col_item = QTreeWidgetItem(table_item)
                    col_item.setText(0, col.name)
                    # 霸下的模型上 `sql_type` 就是**类型名字**（'整数' 这种），
                    # 不是参照项目那个带 `.to_sql()` 的类型对象。
                    type_str = col.sql_type
                    pk_str = " PK" if col.primary_key else ""
                    # 自增 / 唯一：霸下没有这两样，模型上恒为 False，所以这两段
                    # 永远拼不出东西来。留着是因为这是参照项目那句原话。
                    ai_str = " AI" if col.auto_increment else ""
                    nn_str = " NN" if not col.nullable else ""
                    col_item.setText(1, f"{type_str}{pk_str}{ai_str}{nn_str}")
                    # 默认值在霸下是**方法**，要带括号（参照项目那边是属性）。
                    默认 = col.default_value()
                    col_item.setText(2, 默认 if 默认 is not None else "")
                    col_item.setData(0, Qt.UserRole, self.COLUMN_ITEM)
                    col_item.setToolTip(0, col.comment if col.comment else col.name)

            # 数据库默认展开
            db_item.setExpanded(True)

    def _refresh_remote(self) -> None:
        """远程模式：使用 SQL 查询刷新树。

        **这一半照抄，一个字没动，也走不到**：霸下只有"一个内核一个库"，主窗
        永远用位置参数把 `client=None` 传进来。留着是因为它是参照项目那句话，
        而且哪天要接一个真的远程库，这一半是现成的。
        """
        # 获取数据库列表
        result = self._client.query("SHOW DATABASES")
        if result.get("error"):
            return

        dbs = []
        for row in result.get("rows", []):
            if isinstance(row, dict):
                dbs.append(next(iter(row.values()), ""))
            elif row:
                dbs.append(str(row[0]) if row else "")

        for db_name in dbs:
            if not db_name:
                continue

            db_item = QTreeWidgetItem(self._tree)
            db_item.setText(0, db_name)
            db_item.setText(1, "数据库")
            db_item.setText(2, "")
            db_item.setData(0, Qt.UserRole, self.DB_ITEM)
            db_item.setData(0, Qt.UserRole + 1, db_name)
            db_item.setToolTip(0, db_name)

            font = db_item.font(0)
            font.setBold(True)
            db_item.setFont(0, font)

            # 获取表列表
            tables_result = self._client.query(
                f"SHOW TABLES FROM `{db_name}`"
            )
            if tables_result.get("error"):
                db_item.setExpanded(True)
                continue

            tables = []
            for row in tables_result.get("rows", []):
                if isinstance(row, dict):
                    tables.append(next(iter(row.values()), ""))
                elif row:
                    tables.append(str(row[0]) if row else "")

            table_count = 0
            for table_name in tables:
                if not table_name:
                    continue
                table_count += 1

                table_item = QTreeWidgetItem(db_item)
                table_item.setText(0, table_name)
                table_item.setText(1, "表")
                table_item.setData(0, Qt.UserRole, self.TABLE_ITEM)
                table_item.setData(0, Qt.UserRole + 1, db_name)
                table_item.setData(0, Qt.UserRole + 2, table_name)

                # 获取列信息
                cols_result = self._client.query(
                    f"SHOW COLUMNS FROM `{db_name}`.`{table_name}`"
                )
                col_count = 0
                if not cols_result.get("error"):
                    for col_row in cols_result.get("rows", []):
                        if isinstance(col_row, dict):
                            col_name = col_row.get("Field") or col_row.get("field", "")
                            col_type = col_row.get("Type") or col_row.get("type", "")
                            col_key = col_row.get("Key") or col_row.get("key", "")
                            col_null = col_row.get("Null") or col_row.get("null", "YES")
                            col_default = col_row.get("Default") or col_row.get("default", "")
                            col_extra = col_row.get("Extra") or col_row.get("extra", "")
                        elif isinstance(col_row, (list, tuple)) and len(col_row) >= 2:
                            col_name = str(col_row[0]) if col_row[0] else ""
                            col_type = str(col_row[1]) if len(col_row) > 1 else ""
                            col_key = str(col_row[3]) if len(col_row) > 3 else ""
                            col_null = str(col_row[2]) if len(col_row) > 2 else "YES"
                            col_default = str(col_row[4]) if len(col_row) > 4 else ""
                            col_extra = str(col_row[5]) if len(col_row) > 5 else ""
                        else:
                            continue

                        if not col_name:
                            continue

                        col_count += 1
                        col_item = QTreeWidgetItem(table_item)
                        col_item.setText(0, col_name)

                        # Build type info string
                        pk_str = " PK" if col_key.upper() == "PRI" else ""
                        ai_str = " AI" if "auto_increment" in col_extra.lower() else ""
                        nn_str = " NN" if col_null.upper() == "NO" else ""
                        col_item.setText(1, f"{col_type}{pk_str}{ai_str}{nn_str}")
                        col_item.setText(2, col_default if col_default and col_default != "None" else "")
                        col_item.setData(0, Qt.UserRole, self.COLUMN_ITEM)
                        col_item.setToolTip(0, col_name)

                table_item.setText(2, f"{col_count} 列")
                table_item.setToolTip(0, table_name)

            db_item.setText(2, f"{table_count} 张表")
            db_item.setExpanded(True)

    # --------------- 按钮事件 ---------------

    def _请求切库(self, 路) -> None:
        """
        把"我要切到那个库"往上报。**这一层自己不动手。**

        切库要停工人线程、关内核、开新内核、重新登录、重建五个面板，而这一
        串**必须在没有面板活着的时候做**。所以往上报给主窗，由它关掉窗口、
        让外面那个主循环重装一套（见 `霸下之躯.主窗._要切库`）。

        **后两个参数永远给空。** 那一格口令和一格钥匙都是加密页的路数
        （含义写在 `霸下之躯.主窗.请求切库` 那段），这一页切库从来不带——
        它建的库、它列的库，一律是明文库。碰上一个上了锁的库时，口令由
        `开库` 里那个框现问，这一页不掺和。**要建加密库、要从备份恢复的
        是第 6 页**，两处入口各管一头：这一页管"盘上有哪些库"，加密页管
        "锁怎么上"。
        """
        窗 = self.window()
        if 窗 is not None and hasattr(窗, '请求切库'):
            窗.请求切库.emit(路, '', None)
            return
        QMessageBox.information(
            self, "切换不了", "上面那一层没接切库这件事。")

    def _on_create_database(self) -> None:
        """
        新建一个库——**在库目录下新圈一块永久存储区**。

        圈地这一步走 `霸下之躯.建一个库`（建完就关，不占着 Arena），圈完
        **立刻切过去**。刚建出来的库一张表都没有、一个用户都没有，所以切
        过去那一轮 `开库` 会要求设第一个管理员（`界面_用户.装身份` 那一支）
        ——这是有意排的：不切过去的话，用户会看到一个"建好了但进不去"的库。

        重名和非法字符在**这儿**拦，不是在引擎里——引擎压根不认识"库名"
        这个概念，对它来说库就是一个目录。
        """
        if not self._可管库:
            QMessageBox.information(
                self, "做不到",
                "这是连到对面服务端的腿，库在对面那个进程里，这边建不了。")
            return
        目录 = getattr(self.engine, '库目录', None)
        if not 目录:
            QMessageBox.warning(
                self, "做不到", "不知道库都放在哪个目录，建不了。")
            return

        名, 好 = QInputDialog.getText(
            self, "新建库",
            "新库叫什么名字？\n\n会在下面这个位置圈一块新的永久存储区：\n%s"
            % 目录)
        if not 好:
            return
        名 = (名 or "").strip()
        if not 名:
            QMessageBox.warning(self, "没建成", "名字不能是空的。")
            return
        if any(c in 名 for c in '\\/:*?"<>|'):
            QMessageBox.warning(
                self, "没建成",
                '名字里不能有  \\  /  :  *  ?  "  <  >  |  这几个字符。')
            return

        import 霸下之躯 as _躯
        路 = _躯.新库路径(目录, 名)
        if os.path.exists(路):
            QMessageBox.warning(
                self, "没建成", "已经有一个叫「%s」的库了。" % 名)
            return

        try:
            _躯.建一个库(路)
        except Exception as 错:
            QMessageBox.critical(self, "没建成", "建库没成：%s" % 错)
            return

        # 先重数一遍库清单再切。多半是白数（切过去之后整个窗口重建，这份
        # 缓存本来就作废）——但**切库是有可能失败的**：工人线程三秒没停
        # 干净的话 `closeEvent` 会把这次切库取消掉（见那里），窗口原样留着。
        # 那时候树得看得见刚建的这个库，不然用户会以为"建了没建上"。
        self.refresh()
        self._请求切库(路)

    def _on_create_table(self) -> None:
        db_name = self._get_selected_database()
        if not db_name:
            QMessageBox.warning(self, "提示", "请先在左侧选择一个数据库。")
            return
        if not self._选中库开着():
            # **这道拦必须有，光把按钮灰掉不算数。** Ctrl+T 和右键菜单都
            # 直接叫到这里，绕过按钮。真漏过去，`建表` 会发给引擎手上**开着
            # 的那个库**——表就建到别的库去了，而界面上写的是这个名字。
            QMessageBox.information(
                self, "先切过去",
                "「%s」这次没打开，表建不进去。\n\n"
                "**双击它**切过去（或者双击别的库切回来），再建表。"
                % db_name)
            return

        dlg = CreateTableDialog(self.engine, db_name, self)
        if dlg.exec_():
            table_name = dlg.get_table_name()
            self.refresh()
            self._select_item_by_name(db_name, table_name)
            QMessageBox.information(
                self, "成功",
                f"表 '{table_name}' 在数据库 '{db_name}' 中创建成功！"
            )

    def _on_edit_table(self) -> None:
        db_name, table_name = self._get_selected_table()
        if not table_name:
            QMessageBox.warning(self, "提示", "请先在左侧选择一个表。")
            return

        dlg = EditTableDialog(self.engine, db_name, table_name, self)
        if dlg.exec_():
            self.refresh()
            QMessageBox.information(
                self, "成功",
                f"表 '{table_name}' 已更新。"
            )

    def _删库(self, 名, 路) -> None:
        """
        删掉一整个库。**不可撤销。**

        删的是**盘上那个根目录**（`catalog` 和 `r/` 一起）。引擎里没有这
        条原语——区一级只有"标死一个区"、表一级只有"写墓碑"，没有任何一
        条路能抹掉一整个存储区（见 `霸下之躯.删一个库`）。所以这一下是这
        层亲手做的，也正因为这样，确认框里得把话说全：

            删的是哪一块        写全路径。光写名字不够——两个库同名是可能的
            删了会没掉什么       里面所有表 + 所有用户
            删了就回不来         没有回收站、没有回滚、没有备份

        **开着的那个库这儿删不了**，得先切走。文件句柄在引擎手里，Windows
        上开着就删不掉，硬删会删一半抛 `PermissionError`，留下一个半死不活
        的目录（`arena.py:186-188` 那段注释专门在说这个）。所以不让人走到
        那一步——直接拦下来说清楚去哪儿切。
        """
        if not self._可管库:
            QMessageBox.information(
                self, "做不到",
                "这是连到对面服务端的腿，库在对面那个进程里，这边删不了。")
            return

        现在的 = getattr(self.engine, '库文件路径', None)
        if 现在的 and os.path.abspath(路) == os.path.abspath(现在的):
            QMessageBox.information(
                self, "先切走",
                "「%s」正开着，删不了。\n\n"
                "文件句柄在引擎手里——删一个开着的库，在 Windows 上会删到"
                "一半报错，留下一个半死不活的目录。\n\n"
                "先在树上**双击另一个库**切过去，再回来删它。\n"
                "（要是只剩这一个库，先「＋ 新建库」建一个，切过去，再删。）"
                % 名)
            return

        import 霸下之躯 as _躯
        正文 = ("删的是盘上这一整块：\n%s\n\n"
                "里面的**所有表**和**所有用户**会一起没掉。\n\n"
                "**删了就回不来**——霸下没有回收站，也没有回滚。" % 路)
        try:
            是启动库 = (os.path.abspath(_躯.配置默认库())
                        == os.path.abspath(路))
        except Exception:
            是启动库 = False
        if 是启动库:
            # 配置里那条 `库路径` 指着它，也就是**下次开界面要进的那个**。
            # 不说的话，下次开界面进来一看不是自己那个库，会以为删错了。
            正文 += ("\n\n（这也是配置里指定的**启动库**。删完之后，下次开"
                     "界面会进「%s」——就是现在开着的这个。）"
                     % _躯.库名从路径(现在的))

        框 = QMessageBox(self)
        框.setIcon(QMessageBox.Warning)
        框.setWindowTitle("确认删库")
        框.setText("要把库「%s」整个删掉吗？" % 名)
        框.setInformativeText(正文)
        框.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        框.setDefaultButton(QMessageBox.No)
        框.button(QMessageBox.Yes).setText("删掉")
        框.button(QMessageBox.No).setText("算了")
        if 框.exec_() != QMessageBox.Yes:
            return

        try:
            _躯.删一个库(路, 留哪个=现在的)
        except Exception as 错:
            QMessageBox.critical(
                self, "没删掉",
                "删库没成：%s\n\n（盘上那个目录可能删了一半，去看一眼 %s）"
                % (错, 路))
            self.refresh()
            return

        self.refresh()                 # 重扫库清单 + 重画树
        尾 = ("\n\n下一次开界面进的是「%s」。"
              % _躯.库名从路径(现在的)) if 是启动库 else ""
        QMessageBox.information(
            self, "删掉了",
            "库「%s」已经删掉了。盘上那个目录没了。%s" % (名, 尾))

    def _on_drop(self) -> None:
        """删除选中的数据库或表。

        **两样现在都是真删**：删表发一条 `删表`（引擎的事），删库抹掉盘上
        那个根目录（这一层亲手做，见 `_删库`）。
        """
        item = self._tree.currentItem()
        if not item:
            return

        item_type = item.data(0, Qt.UserRole)

        if item_type == self.DB_ITEM:
            self._删库(item.data(0, Qt.UserRole + 1),
                       item.data(0, Qt.UserRole + 3))

        elif item_type == self.TABLE_ITEM:
            db_name = item.data(0, Qt.UserRole + 1)
            table_name = item.data(0, Qt.UserRole + 2)
            reply = QMessageBox.question(
                self, "确认删除",
                f"确定要删除表 '{db_name}.{table_name}' 及其所有数据吗？\n\n"
                "此操作不可撤销！",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            # 霸下没有"改内存模型再保存"这回事：一条 `删表` 发出去，回话了
            # 再刷新目录、再重画树。
            self.engine.发(
                '删表 `%s`' % table_name,
                lambda 结果, 错: self._收删表(结果, 错, db_name, table_name))

    def _收删表(self, 结果, 错, db_name: str, table_name: str) -> None:
        if 错 is not None:
            QMessageBox.critical(self, "错误", f"删除表失败: {错}")
            return
        if 结果 is None or not 结果.ok:
            QMessageBox.critical(
                self, "错误",
                f"删除表失败: {结果.error if 结果 is not None else '(没有回结果)'}")
            return
        # 发出去之后不能立刻读目录（工人还在干），要在回话里读——而且得等
        # 目录刷新的回话到了，才能说"删掉了"。
        self.engine.catalog.刷新(
            lambda 错: self._收删表刷新(错, db_name, table_name))

    def _收删表刷新(self, 错, db_name: str, table_name: str) -> None:
        if 错 is not None:
            QMessageBox.critical(self, "错误", f"删除表失败: {错}")
            return
        self.refresh()
        self._show_detail(None)
        QMessageBox.information(
            self, "成功",
            f"表 '{db_name}.{table_name}' 已删除。"
        )

    # --------------- 备份 ---------------
    #
    # 三件事各在各的线程上，别混：
    #
    #     界面线程   问旧份删不删（Qt 对话框只能在主线程开）、停 MySQL 口
    #     工人线程   落一次盘 → copytree → 开一次副本读表清单
    #     界面线程   还原 MySQL 口、报体积、删旧份
    #
    # 中间那段**必须**在工人线程上，理由不是"不卡界面"：工人线程是全进程
    # 唯一碰 Storage 的地方，在那上面跑完整个复制，期间不可能有第二个写者
    # —— 一致性就是这么来的（见 `备份.py` 模块头）。

    @property
    def 正在备份(self) -> bool:
        """备着没有。**主窗关软件那一路拿它拦人**（见 `霸下之躯.closeEvent`）。"""
        return self._备份中

    def 需要备份吗(self):
        """
        关软件之前问一句：**这个库现在这个样子，备份里有没有一份一样的。**

        回 `(要不要提醒, 匹配到的那份名或 None, 凭什么)`。**第三格是一句
        摆给用户看的话**，只有"要提醒、而且理由不是明摆着的"才有——见
        `备份.已备份过吗` 的第三格。空串就是老话（"之后又写过东西"）。

        - 纯远程腿（`引擎壳.库文件路径` 是 None）：库在对面那台机器上，这边
          既看不着也拷不了 → 不提醒（提醒了人也做不了）。
        - **盘读不动就一律"要提醒"**：判据是"和某一份逐字节一致"，算不出来
          的时候不能当成"已经备过了"——方向永远是多提醒一次。

        ⛔ **两格都得跟配置走**（`备份根按配置` + `钥匙根按配置`），不能
        只有备份按钮走。少哪一格，症状都是"按钮说备过了、这儿说没备过"
        （或者反过来，**拿一句空话把人拦在关窗口那一步上**）：
        `备份根` 漏了 → 去老位置找，配置挪过的那几份一个都看不见；
        `钥匙根` 漏了 → 换过钥匙的那些份**只能比字节**，而它们和库
        永远不可能逐字节一样，于是**每一份都判成"没备过"**。
        """
        路 = getattr(self.engine, '库文件路径', None)
        if not 路:
            return False, None, ''
        try:
            有, 名, 缘故 = 备份.已备份过吗(路, 备份.备份根按配置(路),
                                          备份.钥匙根按配置(路))
        except Exception as 错:
            # ⛔ **warning，不是 debug。** 这一句是"为什么它每次都弹"的
            # **唯一线索**：走到这儿的人拿到的只是一句"从上次备份之后又
            # 写过东西"，而真相全在 `错` 里。压在 debug 上等于没有 ——
            # 用户报"老是弹"的时候，日志里一个字都找不着。
            _log.warning('比对备份没过：%s', 错)
            return True, None, '比对没做成：%s' % 错
        return (not 有), 名, 缘故

    def 手动备份(self, 说明=None, 备完还听=None) -> bool:
        """
        备份**当前开着的那个库**。回 True 表示"已经投出去在做了"。

        **这是唯一的入口**——按钮和关软件那个提醒都走这儿，所以两条路的
        行为一个字都不会不一样。`说明` 是给提醒那条路用的一句话，摆在成功
        提示的开头。

        `备完还听`：**备份跑完之后 MySQL 口该不该听着。**
        不给（`None`）就是"照备份之前那个样子还回去"——按钮那条路走的就是
        这一支。`_拦一下关窗口` 那条路**必须显式给**：那边 `closeEvent` 早就
        按次序把口停掉了，可用户点了「去备份」人还留在这儿，口不能替他关着
        （停着的口在爬虫那头是 2003，"根本没人接"，看不出是为什么）。给的
        也不算"替他开"——给的是**关窗口之前它本来在不在听**，
        `closeEvent` 在停它之前把那个值记下来了。

        **备完不替人关窗口。** 关窗口这件事只由人自己按那一下 X 说了算：
        备份中途要弹勾选框、完了要报价，这中间把窗口关掉会把这几个框一起
        带走。所以关软件那条路是「弹提醒 → 去备份 → 窗口留着 → 人再按一次
        X」，那一次指纹已经对上了，一声不响地关。

        顺序是有讲究的，四条：

          1. **先问旧份删不删**（主线程）。问不出结果（人点了取消）就整个
             不备了——问完了才发现不备，比不问更烦人。
          2. **再停 MySQL 口**。`门.停()` 会**先把攒着没落的那批写落下去**
             —— 不停的话，那批写还在这边内存里，`copytree` 抄不到它，备出来
             的少最后一批。
          3. **投活**：落一次盘 → 拷 → 开一次副本核对表清单。
          4. **回话里**（主线程）：还原 MySQL 口 → 删旧份 → 报体积。

        **拷到一半失败的话，旧的一份都不删**（第 4 步才删）。反过来的话，
        盘满或者中途出错就是"旧的没了、新的又没成"——那一下是净损失。
        """
        if self._备份中:
            QMessageBox.information(
                self, "还在备份", "上一份还没备完，等它跑完再说。")
            return False

        路 = getattr(self.engine, '库文件路径', None)
        if not 路:
            QMessageBox.information(
                self, "做不到",
                "这是连到对面服务端的腿，库在对面那个进程里，这边拷不了。")
            return False

        路 = os.path.abspath(路)
        # ⛔ **走 `备份根按配置`，不是 `备份根从库`** —— 后者只算老位置，
        # 配置里那条 `备份目录` 填了的话，备出来的东西和恢复页找的地方
        # 就不是同一处。
        备份根 = 备份.备份根按配置(路)
        # 份名用**引擎壳记的那个库名**（`库名从路径` 的产物，`霸下.db` → `霸下`）
        # ——自己拿 basename 再剥一次后缀就是第二把尺子，哪天两边不一致，
        # 表现是"树上写霸下、备份叫霸下.db-…"。
        库名 = getattr(self.engine, '库名', None) or os.path.basename(路)

        # ⓪ 两个目录**先试着建一遍**，建不出来当场拦住。
        #
        #    ⛔ 摆在①之前、摆在停口和拷几百 MB 之前。理由见 `备份.能写进`
        #    的 docstring：配置里那一格写错（盘符不存在、有非法字符、指的
        #    是个文件）的话，不拦的结果是**停了一趟口、干等了半天**，
        #    最后收到一句 `[WinError 3] 系统找不到指定的路径。: 'Q:\'`。
        钥匙根 = 备份.钥匙根按配置(路)
        try:
            备份.能写进(备份根, '「备份放在哪儿」那一格')
            if 钥匙根:
                备份.能写进(钥匙根, '「备份钥匙放在哪儿」那一格')
        except ValueError as 错:
            QMessageBox.warning(self, "地方不对", str(错))
            return False

        # ⓪′ 这两格还**不许落在库自己里面**。理由见 `备份.装在库里吗`：
        #     落进去的话 `指纹(库根)` 就跟着变（它摘的是库底下所有文件），
        #     `已备份过吗` 永远回"没备过" ⇒ **关窗口那个提醒一直弹，人点
        #     几次「去备份」都是白跑**；`备份目录` 落在库上更糟，会
        #     `备份/甲-001/备份/甲-001/…` 一层套一层拷进自己里面去。
        #     两种都是"设错了"不是"库坏了"，所以拦在这儿，当场说清是哪一格。
        for 叫法, 指的 in (('「备份放在哪儿」那一格', 备份根),
                           ('「备份钥匙放在哪儿」那一格', 钥匙根)):
            if 备份.装在库里吗(路, 指的):
                QMessageBox.warning(
                    self, '地方不对',
                    '%s 指的是这个库**自己里面**：\n%s\n\n'
                    '备份和钥匙都不能放在库目录里 —— 库"备没备过"是拿库'
                    '底下所有文件的摘要算的，落进去就等于**备份自己改了库**，'
                    '关窗口那个提醒会一直弹。\n\n'
                    '去「设置」页把那一格改成库外面的一个目录。'
                    % (叫法, 指的))
                return False

        # ① 旧份：先问清楚要删哪些。**没有旧份就不问**——第一次备份弹一个
        #    空问题，人只会以为坏了。
        要删 = []
        旧份 = 备份.列备份(备份根)
        if 旧份:
            框 = 挑旧份框(旧份, 备份.份名(库名), self)
            if 框.exec_() != QDialog.Accepted:
                return False
            要删 = 框.要删的()

        # ② 备份口令（**配了第三处才问**）。摆在停口之前问：人点了取消的话，
        #    口还没停过，这一趟什么都没动过。
        #
        #    ⛔ **判据是 `备份.会换钥匙吗`**，不是"界面自己看配置里那格空没空"
        #    —— 两个条件（配了第三处 + 库是加密的）分在两个模块里，这儿
        #    自己 `and` 一遍就是第二把尺子，漏一个的症状是"问了口令、
        #    `做备份` 根本没用它"（备出来的还是自足的那一份，看着一切正常）。
        密文 = getattr(self.engine, '密文', None)
        备份口令 = None
        if 备份.会换钥匙吗(钥匙根, 密文):
            备份口令 = 界面零件.问备份口令(
                self, 第一次=not 备份.列钥匙(钥匙根), 份名=备份.份名(库名))
            if 备份口令 is None:
                return False

        # ③ 停口。**现在**是不是在听，由 `管MySQL口(False)` 的返回值说了算；
        #    完事恢复到什么样子，由 `备完还听` 说了算（不给就是"照原来的"）。
        窗 = self.window()
        现在听着 = False
        if 窗 is not None and hasattr(窗, '管MySQL口'):
            现在听着 = bool(窗.管MySQL口(False))
        self._备份前听着 = 现在听着 if 备完还听 is None else bool(备完还听)

        self._备份中 = True
        self._备份起钟 = time.time()     # 只为回话里报一句"用了多久"
        self._摆备份钮()

        # 份名定在**问完口令之后**：`做备份` 撞名会加 `-2` 尾巴，所以
        # `问备份口令` 里那个份名只是给人看的"大概是哪一份"，
        # 真正落盘的份名由 `做备份` 说了算（它回来的是完整路径）。
        份 = 备份.份名(库名)
        # **钥匙在投活这一刻取下来。** 上了工人线程之后再回头读
        # `self.engine.密文` 也行（就是个属性），但那是**跨线程读一个
        # 会变的东西**——切库会重建窗口、换一个壳，读到的可能是下一把
        # 钥匙。这里的 `路` / `备份根` / `份` 本来就是投活时抄下来的，
        # 钥匙照同一个办：**抄一份此刻的**。
        self.engine.投(
            lambda 闸: self._备份的活(闸, 路, 备份根, 份, 密文,
                                      钥匙根, 备份口令, 库名),
            lambda 值, 错: self._备份回话(值, 错, 备份根, 要删, 说明,
                                          钥匙根))
        return True

    @staticmethod
    def _备份的活(闸, 路, 备份根, 份, 密文=None, 钥匙根='', 备份口令=None,
                  库名=None):
        """
        **跑在工人线程上**：落盘 → 拷贝 → 核对。返回值原样交回 `_备份回话`。

        落盘那一步发的是 `显示 表列表`，一条语句两用：它的收尾是
        `_flush(force=True)`（每条语句的收尾都落盘，`syntax.py` 那句
        `execute_batch` 的末尾），而且它回来那份表清单就是**核对时要对的
        东西**——刚从引擎嘴里问出来的，比界面手里那份两秒一刷的缓存新。

        `密文` 是**本会话手里的钥匙**。拷贝那一步拿它当"源密文"（换钥匙
        那一路要边解边重新加密）；核对那一步的钥匙**另算**，见下。

        ⛔ **核对用的钥匙是从第三处现读回来的，不是手上这把。**
        换过钥匙的那一份，密文是**备份钥匙**加的，拿库钥匙去开只会回
        "这一份开不起来"。而在这儿 `开备份钥匙` 重推一遍（约 0.3 秒）
        反而正合用：它证明的是**"只拿着这份备份 + 第三处那个钥匙文件 +
        那句口令的人，打得开它"** —— 那正是将来恢复时的处境。
        少这一步的话，"钥匙文件落是落了、内容却是坏的"这件事要等到
        真去恢复的那天才发现，而那时候没有第二份可退。
        """
        首 = 闸.fetch('显示 表列表')
        if not 首.ok:
            raise RuntimeError(
                '备份前那一步落盘没成：%s' % 首.error)
        表们 = [行[0] for 行 in 首.as_lists() if 行 and 行[0]]

        新路 = 备份.做备份(路, 备份根, 份, 库密文=密文, 钥匙根=钥匙根,
                           备份口令=备份口令, 库名=库名)
        验的钥匙 = 密文
        if 备份.会换钥匙吗(钥匙根, 密文):
            # 份名以**盘上真落下来的那个**为准（撞名会变成 `-2`）。
            验的钥匙 = 备份.开备份钥匙(钥匙根, os.path.basename(新路),
                                       备份口令)
        说 = 备份.做完验一遍(新路, 表们, 密文=验的钥匙)
        return {'路': 新路, '字节': 备份.目录字节(新路),
                '说': 说, '表数': len(表们), '换钥匙': bool(钥匙根)}

    def _备份回话(self, 值, 错, 备份根, 要删, 说明, 钥匙根='') -> None:
        """**在主线程里被叫。** 还原 MySQL 口 → 删旧份 → 报价。

        `钥匙根` 从 `手动备份` 那一头**递过来**，不在这儿重新读配置：
        这一趟"备的"和"删的"必须是同一批钥匙（用户要是趁着备份那几秒
        去设置页改了 `备份钥匙目录`，重新读一遍就会去另一个目录里删钥匙
        —— 而那个目录里压根没有这几份的钥匙）。
        """
        self._备份中 = False
        self._摆备份钮()
        用了 = time.time() - getattr(self, '_备份起钟', time.time())

        窗 = self.window()
        if self._备份前听着 and 窗 is not None and hasattr(窗, '管MySQL口'):
            窗.管MySQL口(True)
            self._备份前听着 = False

        if 错 is not None:
            尾巴 = ''
            if 要删:
                尾巴 = ('\n\n（你勾的那 %d 份旧备份**一份都没删**——新的这份'
                        '没成，就不动旧的。腾地方的话，看看盘的剩余空间。）'
                        % len(要删))
            QMessageBox.critical(
                self, "没备成", "备份没成：%s%s" % (错, 尾巴))
            return

        删掉 = 0
        if 要删:
            try:
                # 钥匙根一起递进去：删掉的那几份，第三处的钥匙也跟着收掉
                # （次序和理由在 `备份.删几份` 的 docstring 里）。
                删掉 = 备份.删几份(备份根, 要删, 钥匙根)
            except Exception as 删错:
                # 新那份**已经备好也验过了**，删旧份没成是另一件事，别把
                # 成功说成失败。
                QMessageBox.warning(
                    self, "旧份没删掉",
                    "新的那一份已经备好了。旧的那几份没删成：%s" % 删错)

        QMessageBox.information(
            self, "备好了",
            "%s\n\n新的一份：\n%s\n\n大小 %s（%d 张表）。%s\n"
            "拷贝用了 %.1f 秒。%s%s"
            % (说明 + '\n\n' if 说明 else '',
               值['路'], 备份.字节文案(值['字节']), 值['表数'], 值['说'],
               用了,
               ('\n顺手删掉了 %d 份旧的。' % 删掉) if 删掉 else '',
               # 换过钥匙的那一份要**当面说一句它不自足了** —— 不说的话，
                # 人以为备份还是"拷到哪儿都能开"的那个东西，
                # 而真相是它离了第三处那个钥匙文件就永远是乱码。
               ('\n\n⚠ 这一份用的是**另配的那把钥匙**（不是库口令）：'
                '以后恢复它，要拿那句备份口令，再加上一句给新库现定的口令。'
                '\n库口令打不开它，**那把钥匙文件丢了它也就没了**。')
               if 值.get('换钥匙') else ''))

    def _摆备份钮(self) -> None:
        """按钮跟着 `_备份中` 走。**备着的时候灰掉**，不让人再点一下。"""
        钮 = getattr(self, '_backup_btn', None)
        if 钮 is None:
            return
        if self._备份中:
            钮.setText("⏳ 正在备份…")
        else:
            钮.setText("🗄 备份当前库")
        钮.setEnabled((not self._备份中) and self._可管库)

    # --------------- 树选择事件 ---------------

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        self._show_detail(item)

        item_type = item.data(0, Qt.UserRole)

        # 远程模式：创建/编辑/删除操作不可用（服务端管理）
        if self._is_remote:
            self._create_db_btn.setEnabled(False)
            self._create_table_btn.setEnabled(False)
            self._edit_table_btn.setEnabled(False)
            self._drop_btn.setEnabled(False)
            self._drop_btn.setText("删除")
            return

        if item_type == self.DB_ITEM:
            未开 = bool(item.data(0, Qt.UserRole + 4))
            有路 = item.data(0, Qt.UserRole + 3) is not None
            开着的 = (not 未开) and getattr(self.engine, '库文件路径', None) is not None
            # 表建在**开着的那个库**里。选到一个没打开的库（灰的那种），
            # 按钮不亮——不亮比亮着点下去报一句错要诚实。
            self._create_table_btn.setEnabled(开着的)
            self._edit_table_btn.setEnabled(False)
            self._drop_btn.setText("删库")
            self._drop_btn.setEnabled(有路 and self._可管库)
            if 开着的:
                self._drop_btn.setToolTip(
                    "删除整个数据库及其所有表 (Del)。\n"
                    "这个是当前开着的库——**得先双击别的库切过去**，"
                    "再回来删它。")
            else:
                self._drop_btn.setToolTip("删除整个数据库及其所有表 (Del)")
        elif item_type == self.TABLE_ITEM:
            self._create_table_btn.setEnabled(True)
            self._edit_table_btn.setEnabled(True)
            self._drop_btn.setEnabled(True)
            self._drop_btn.setText("删表")
            self._drop_btn.setToolTip("删除当前选中的表 (Del)")
        elif item_type == self.COLUMN_ITEM:
            self._create_table_btn.setEnabled(True)
            self._edit_table_btn.setEnabled(True)
            self._drop_btn.setEnabled(False)
        else:
            self._create_table_btn.setEnabled(False)
            self._edit_table_btn.setEnabled(False)
            self._drop_btn.setEnabled(False)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """双击表时进行编辑，双击**没打开的那个库**时切过去。"""
        item_type = item.data(0, Qt.UserRole)
        if item_type == self.TABLE_ITEM:
            self._on_edit_table()
        elif item_type == self.DB_ITEM:
            if item.data(0, Qt.UserRole + 4):
                self._请求切库(item.data(0, Qt.UserRole + 3))

    def _show_detail(self, item: QTreeWidgetItem | None) -> None:
        """在右侧面板显示选中对象的详细信息."""
        self._detail_text.clear()
        self._data_table.clear()
        self._data_table.setRowCount(0)
        self._data_table.setColumnCount(0)

        # Hide preview buttons by default
        self._preview_db_name = ""
        self._preview_table_name = ""
        self._preview_new_row = -1
        # 换对象了：手上那份还没回来的预览查询就此作废（回话里会对这个号），
        # 不然它回来会把**上一张表**的数据画到这一页上。
        self._预览号 += 1
        self._preview_add_btn.setVisible(False)
        self._preview_del_btn.setVisible(False)
        self._preview_save_btn.setVisible(False)

        if not item:
            return

        item_type = item.data(0, Qt.UserRole)

        if self._is_remote:
            self._show_detail_remote(item, item_type)
        else:
            self._show_detail_local(item, item_type)

    def _show_detail_local(self, item: QTreeWidgetItem, item_type: int) -> None:
        """本地模式：通过 engine.catalog 显示详情。"""

        if item_type == self.DB_ITEM:
            db_name = item.data(0, Qt.UserRole + 1)
            try:
                db = self.engine.catalog.get_database(db_name)
                detail = f"数据库: {db.name}\n"
                detail += f"{'-' * 50}\n"
                detail += f"表数量:  {db.table_count}\n"
                # 字符集 / 排序规则：霸下没有这两样，模型上给的是空串——所以
                # 这两行显示出来是空的。**是"没有"，不是读坏了。**
                detail += f"字符集:  {db.charset}\n"
                detail += f"排序规则: {db.collation}\n"
                detail += f"注释:    {db.comment or '(无)'}\n"
                detail += f"\n表列表:\n"
                for tname in db.table_names:
                    tbl = db.table(tname)
                    detail += f"  * {tname} ({tbl.column_count} 列)\n"
                self._detail_text.setText(detail)
            except Exception as e:
                self._detail_text.setText(f"加载数据库信息失败: {e}")

        elif item_type == self.TABLE_ITEM:
            db_name = item.data(0, Qt.UserRole + 1)
            table_name = item.data(0, Qt.UserRole + 2)
            self._preview_db_name = db_name
            self._preview_table_name = table_name
            self._preview_pk = _get_table_pk(self.engine, db_name, table_name)
            self._preview_add_btn.setVisible(True)
            self._preview_del_btn.setVisible(True)

            try:
                db = self.engine.catalog.get_database(db_name)
                tbl = db.table(table_name)

                detail = f"表: {db_name}.{tbl.name}\n"
                detail += f"{'-' * 50}\n"
                detail += f"列数: {tbl.column_count}\n"
                detail += f"主键: {', '.join(tbl.primary_key) if tbl.primary_key else '(无)'}\n"
                detail += f"引擎: {tbl.engine}\n"
                detail += f"注释: {tbl.comment or '(无)'}\n"
                self._detail_text.setText(detail)

                # Load and display table data
                self._load_preview_data(db_name, table_name)
            except Exception as e:
                self._detail_text.setText(f"加载失败: {e}")
                self._data_table.setRowCount(0)
                self._data_table.setColumnCount(0)

        elif item_type == self.COLUMN_ITEM:
            # 获取父表
            parent = item.parent()
            if parent and parent.data(0, Qt.UserRole) == self.TABLE_ITEM:
                db_name = parent.data(0, Qt.UserRole + 1)
                table_name = parent.data(0, Qt.UserRole + 2)
                col_name = item.text(0)
                try:
                    db = self.engine.catalog.get_database(db_name)
                    tbl = db.table(table_name)
                    col = tbl.column(col_name)
                    detail = f"列: {db_name}.{table_name}.{col.name}\n"
                    detail += f"{'-' * 50}\n"
                    # 霸下的 `sql_type` 是类型名字符串（'整数' 这种），没有
                    # `.to_sql()` 那一层。
                    detail += f"类型:       {col.sql_type}\n"
                    detail += f"可空:       {'是' if col.nullable else '否 (NOT NULL)'}\n"
                    detail += f"主键:       {'是' if col.primary_key else '否'}\n"
                    # 唯一 / 自增：霸下没有，这里是诚实的"否"。
                    detail += f"唯一:       {'是' if col.unique else '否'}\n"
                    detail += f"自增:       {'是' if col.auto_increment else '否'}\n"
                    detail += f"默认值:     {col.default_value() if col.default_value() is not None else '(无)'}\n"
                    detail += f"序号:       {col.ordinal}\n"
                    detail += f"注释:       {col.comment or '(无)'}\n"
                    self._detail_text.setText(detail)
                except Exception as e:
                    self._detail_text.setText(f"加载列信息失败: {e}")

    def _show_detail_remote(self, item: QTreeWidgetItem, item_type: int) -> None:
        """远程模式：通过 SQL 查询显示详情。（照抄，走不到——同 `_refresh_remote`。）"""
        if item_type == self.DB_ITEM:
            db_name = item.data(0, Qt.UserRole + 1)
            # Get table list for this database
            result = self._client.query(f"SHOW TABLES FROM `{db_name}`")
            table_names = []
            if not result.get("error"):
                for row in result.get("rows", []):
                    if isinstance(row, dict):
                        table_names.append(next(iter(row.values()), ""))
                    elif row:
                        table_names.append(str(row[0]) if row else "")

            detail = f"数据库: {db_name}\n"
            detail += f"{'-' * 50}\n"
            detail += f"表数量:  {len(table_names)}\n"
            detail += f"\n表列表:\n"
            for tname in table_names:
                detail += f"  * {tname}\n"
            self._detail_text.setText(detail)

        elif item_type == self.TABLE_ITEM:
            db_name = item.data(0, Qt.UserRole + 1)
            table_name = item.data(0, Qt.UserRole + 2)
            self._preview_db_name = db_name
            self._preview_table_name = table_name
            self._preview_pk = _get_table_pk_remote(
                self._client, db_name, table_name
            )

            # Get column info
            cols_result = self._client.query(
                f"SHOW COLUMNS FROM `{db_name}`.`{table_name}`"
            )
            col_count = 0
            col_names = []
            if not cols_result.get("error"):
                for row in cols_result.get("rows", []):
                    if isinstance(row, dict):
                        cn = row.get("Field") or row.get("field", "")
                        if cn:
                            col_names.append(cn)
                            col_count += 1

            detail = f"表: {db_name}.{table_name}\n"
            detail += f"{'-' * 50}\n"
            detail += f"列数: {col_count}\n"
            detail += f"主键: {', '.join(self._preview_pk) if self._preview_pk else '(无)'}\n"
            self._detail_text.setText(detail)

            # Load preview data
            self._load_preview_data(db_name, table_name)

        elif item_type == self.COLUMN_ITEM:
            parent = item.parent()
            if parent and parent.data(0, Qt.UserRole) == self.TABLE_ITEM:
                db_name = parent.data(0, Qt.UserRole + 1)
                table_name = parent.data(0, Qt.UserRole + 2)
                col_name = item.text(0)

                # Get specific column info
                cols_result = self._client.query(
                    f"SHOW COLUMNS FROM `{db_name}`.`{table_name}`"
                )
                col_info = None
                if not cols_result.get("error"):
                    for row in cols_result.get("rows", []):
                        if isinstance(row, dict):
                            if row.get("Field") == col_name or row.get("field") == col_name:
                                col_info = row
                                break
                        elif row and str(row[0]) == col_name and len(row) >= 6:
                            col_info = {
                                "Field": str(row[0]),
                                "Type": str(row[1]),
                                "Null": str(row[2]),
                                "Key": str(row[3]),
                                "Default": str(row[4]),
                                "Extra": str(row[5]),
                            }
                            break

                if col_info:
                    detail = f"列: {db_name}.{table_name}.{col_name}\n"
                    detail += f"{'-' * 50}\n"
                    detail += f"类型:       {col_info.get('Type', '?')}\n"
                    detail += f"可空:       {'是' if col_info.get('Null', 'YES').upper() == 'YES' else '否 (NOT NULL)'}\n"
                    detail += f"主键:       {'是' if col_info.get('Key', '').upper() == 'PRI' else '否'}\n"
                    detail += f"自增:       {'是' if 'auto_increment' in col_info.get('Extra', '').lower() else '否'}\n"
                    detail += f"默认值:     {col_info.get('Default') or '(无)'}\n"
                    self._detail_text.setText(detail)
                else:
                    self._detail_text.setText(f"未找到列 '{col_name}' 的信息")

    # --------------- 数据预览编辑 ---------------

    def _显示文本(self, 类型号: int, 真值):
        """一格里显示成什么。

        空显示成 `NULL`（参照项目就这个字样）；别的走 `schema.render`——**和
        引擎 `结果.as_lists()` 用的是同一把尺子**，所以格子里的字和引擎自己
        打印出来的一模一样。
        """
        if 真值 is None:
            return "NULL"
        if 类型号 == _schema.BLOB:
            # 二进制：渲染出来是 `b'\x00…'`，那串字不是它的字节。这一格照
            # `界面零件.ResultTable` 的写法显示成 `<N 字节>`，而且不给编辑。
            return '<%d 字节>' % len(真值)
        显示 = _schema.render(类型号, 真值)
        return "" if 显示 is None else str(显示)

    def _做格子(self, 类型号: int, 真值) -> QTableWidgetItem:
        """造一格：显示文本给人看，**真值塞在 `Qt.UserRole` 里**。

        比"变没变"、拼 `其中` 条件、回写，用的都是 UserRole 里那个真值——
        显示文本是渲染出来的，拿它当值会踩到静默错答案（时间列显示成
        `2026-09-13 10:00:00` 那种串，真值是毫秒整数）。
        """
        item = QTableWidgetItem(self._显示文本(类型号, 真值))
        if 真值 is None:
            item.setForeground(QColor("#94A3B8"))
            f = QFont(); f.setItalic(True)
            item.setFont(f)
        if 类型号 == _schema.BLOB:
            # 二进制格子只读：文字表达不了字节，能编辑就等于能静默存错。
            # （`设为 NULL` 那条路还是通的：空值对哪种类型都是空值。）
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        item.setData(Qt.UserRole, 真值)
        return item

    def _文本取值(self, 文本: str, 类型号: int):
        """格子里的文字 -> 该列类型的真值（引擎那把 `coerce` 的尺子）。

        转不动就把原文返回：**让引擎去报错**，别在界面这边先判一个"不行"。
        """
        if 文本.upper() == "NULL" or 文本 == "":
            return None
        try:
            return _schema.coerce(类型号, 文本)
        except Exception:
            return 文本

    def _load_preview_data(self, db_name: str, table_name: str) -> None:
        """把表数据装进预览网格。**投活，不阻塞**——回话在主线程里被叫。

        参照项目在这儿拼的是 `SELECT * FROM t LIMIT 100`，还是同步跑的。霸下
        这边一条 `查询 * 从 `t` 限制 100` 投出去就返回。
        """
        self._data_table.clear()
        # 换一次查询换一个号：换表了、手快连点了几下刷新，回来的是谁都对不上
        # 了——老回话一律作废，不然它会把上一张表的数据画到这一页上。
        self._预览号 += 1
        号 = self._预览号
        self.engine.取(
            '查询 * 从 `%s` 限制 %d' % (table_name, 100),
            lambda 结果, 错: self._收预览(号, db_name, table_name, 结果, 错))

    def _收预览(self, 号: int, db_name: str, table_name: str, 结果, 错) -> None:
        """预览数据的回话（**在主线程里被叫**）。"""
        if 号 != self._预览号:
            return                       # 老回话，作废
        if 错 is not None:
            self._detail_text.setText(f"加载失败: {错}")
            return
        if 结果 is None or not 结果.ok:
            self._detail_text.setText(
                f"加载失败: {结果.error if 结果 is not None else '(没有回结果)'}")
            return
        if (self._preview_db_name != db_name
                or self._preview_table_name != table_name):
            return                       # 这会儿已经翻到别处了

        列们 = [名 for 名, _ in 结果.columns]
        列类型 = [号 for _, 号 in 结果.columns]
        self._preview_columns = 列们
        self._预览类型 = 列类型
        self._preview_pk = _get_table_pk(self.engine, db_name, table_name)

        # 装格子会触发 itemChanged（Qt 的规矩），先摁住信号——不过格子里
        # 的 UserRole 是**在 setItem 之前**就塞好的，`_on_preview_cell_changed`
        # 那道"没变就返回"的闸本来也拦得住，这是参照项目就有的两道保险。
        self._data_table.blockSignals(True)
        try:
            self._data_table.setColumnCount(len(列们))
            self._data_table.setHorizontalHeaderLabels(列们)
            self._data_table.setRowCount(len(结果.rows))
            for r, 行 in enumerate(结果.rows):
                for c, (t, v) in enumerate(行):
                    self._data_table.setItem(r, c, self._做格子(t, v))
        finally:
            self._data_table.blockSignals(False)

    def _on_preview_cell_changed(self, item: QTableWidgetItem) -> None:
        """Inline edit on preview cell -- auto-save via UPDATE.

        霸下这边落成一条 `更新 `t` 设 `列` = 值 其中 <主键条件>`，**投活发出去**，
        绿地红地在回话里上色。
        """
        if item is None or self._data_table.signalsBlocked():
            return
        if not self._preview_db_name or not self._preview_table_name:
            return

        row, col = item.row(), item.column()
        col_name = self._data_table.horizontalHeaderItem(col).text() if self._data_table.horizontalHeaderItem(col) else ""
        new_text = item.text().strip()
        old_value = item.data(Qt.UserRole)

        # NULL ↔ None 等价跳过，防止无限 itemChanged 递归
        if (new_text.upper() == "NULL" or new_text == "") and old_value is None:
            return

        # 这一列是什么类型。列号对不上的时候（理论上不会）当"认不出"处理：
        # 显示文本原样比，绝不当成"没变"糊过去。
        类型号 = self._预览类型[col] if col < len(self._预览类型) else None

        # 二进制格子不给编辑（`_做格子` 里已经摘掉了可编辑标记，这里再拦一道
        # ——`设为 NULL` 那种程序性改动也会走到这儿，那种是**要放行**的，
        # 因为空值对二进制列也是个正经的值）。
        if 类型号 == _schema.BLOB and not (new_text.upper() == "NULL" or new_text == ""):
            self._数据表回字(item, 类型号, old_value)
            QMessageBox.information(
                self, "提示",
                "二进制的格子不能这么改：格子里那串文字不是它的字节，按文字"
                "存回去存下来的是另一串字节。要清空它，用右键的「🚫 设为 NULL」。"
            )
            return

        # 防止 IME 组合输入中间态导致中文被清空：
        # 如果新文本为空但旧值不为 NULL，可能是 IME 尚未提交，跳过保存
        if new_text == "" and old_value is not None:
            return

        # 变了没有？**拿真值比**（`'5'` 和 `5` 不是两回事，时间列的显示文本
        # 和它那个毫秒整数也不是两回事）。比的是真值，显示文本只用来给人看。
        if 类型号 is None:
            新值 = None if new_text.upper() == "NULL" else new_text
        else:
            新值 = self._文本取值(new_text, 类型号)
        if 新值 == old_value:
            return

        # Build WHERE from PK
        pk_cols = self._preview_pk or [self._preview_columns[0]] if self._preview_columns else []
        where_parts = []
        for pk in pk_cols:
            if pk in self._preview_columns:
                pk_idx = self._preview_columns.index(pk)
                pk_item = self._data_table.item(row, pk_idx)
                if pk_item:
                    # **读的是 UserRole 里那个老真值**：正在改的这一格要是自己
                    # 就是主键，条件里写新值的话这条 `更新` 一行也匹配不上，
                    # 而界面还会绿给你看——那是最坏的那种"成功"。
                    pk_val = pk_item.data(Qt.UserRole)
                    if pk_val is None:
                        where_parts.append(f"`{pk}` 为空")
                    else:
                        pk_t = self._预览类型[pk_idx] if pk_idx < len(self._预览类型) else _schema.TEXT
                        try:
                            where_parts.append('`%s` = %s' % (pk, 字面量(pk_val, pk_t)))
                        except ValueError:
                            # 给不出字面量（只可能是二进制）——这一行没法按
                            # 主键定位，宁可不动。
                            return
        if not where_parts:
            return

        if 类型号 is None:
            set_clause = '`%s` = 空' % col_name if 新值 is None else "`%s` = '%s'" % (col_name, str(新值).replace("'", "''"))
        else:
            try:
                set_clause = ('`%s` = 空' % col_name if 新值 is None
                              else '`%s` = %s' % (col_name, 字面量(新值, 类型号)))
            except ValueError:
                self._数据表回字(item, 类型号, old_value)
                return
        sql = '更新 `%s` 设 %s 其中 %s' % (
            self._preview_table_name, set_clause, ' 与 '.join(where_parts))

        self.engine.发(sql, lambda 结果, 错: self._收改格(结果, 错, item, 新值))

    def _收改格(self, 结果, 错, item: QTableWidgetItem, 新值) -> None:
        """改一格的回话（**在主线程里被叫**）。

        **绿是"真的写进去了"**：引擎回话说影响 0 行（比如这一行刚被别人删了、
        或者主键被谁改了），那就不是成功——那种情况下绿一下，人以为存进去了，
        盘上其实一个字没动。
        """
        if 错 is not None:
            item.setBackground(QColor("#FEF2F2"))
            return
        if 结果 is None or not 结果.ok or 结果.affected <= 0:
            item.setBackground(QColor("#FEF2F2"))
            return
        # 从这儿起，这一格的真值就是新的了。**回写排在成功之后**：要是提前
        # 回写，那条 `更新` 的 `其中` 就会拿新值去定位老行。
        item.setData(Qt.UserRole, 新值)
        item.setBackground(QColor("#F0FDF4"))

    def _数据表回字(self, item: QTableWidgetItem, 类型号, 真值) -> None:
        """把一格改回它原来的样子（显示文本 + 真值），不惊动 `itemChanged`。"""
        blocked = self._data_table.signalsBlocked()
        self._data_table.blockSignals(True)
        try:
            item.setText(self._显示文本(类型号, 真值) if 类型号 is not None
                         else (str(真值) if 真值 is not None else "NULL"))
        finally:
            self._data_table.blockSignals(blocked)

    def _on_preview_add_row(self) -> None:
        """Add blank row to preview for INSERT.

        **这一条全是本地的**（参照项目就是这样）：只往网格里插一行空白，
        真落盘要等「保存新行」。所以这一段一个字没改。
        """
        if not self._preview_table_name or self._data_table.columnCount() == 0:
            return
        r = self._data_table.rowCount()
        self._data_table.insertRow(r)
        for c in range(self._data_table.columnCount()):
            item = QTableWidgetItem("NULL")
            item.setForeground(QColor("#94A3B8"))
            f = QFont(); f.setItalic(True); item.setFont(f)
            item.setData(Qt.UserRole, None)
            self._data_table.setItem(r, c, item)
        self._preview_new_row = r
        self._preview_save_btn.setVisible(True)
        self._data_table.editItem(self._data_table.item(r, 0))

    def _on_preview_del_row(self) -> None:
        """Delete selected rows.

        一行一条 `删除`，一批投出去（`发多`）。**只有引擎说真删掉了的行才从
        网格里拿掉**；说"影响 0 行"的，那句提示照旧。
        """
        rows = set(i.row() for i in self._data_table.selectedItems())
        if not rows:
            QMessageBox.information(self, "提示", "请先选择要删除的行")
            return
        reply = QMessageBox.question(self, "确认删除",
            f"确定要删除 {len(rows)} 行数据吗？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        pk_cols = self._preview_pk or [self._preview_columns[0]] if self._preview_columns else []
        待发 = []          # [(行号, 语句)]
        定位不了 = []      # 主键是空的那些行：在霸下没法按主键定位
        for row in sorted(rows, reverse=True):
            where_parts = []
            for pk in pk_cols:
                if pk in self._preview_columns:
                    pk_idx = self._preview_columns.index(pk)
                    pk_item = self._data_table.item(row, pk_idx)
                    if pk_item:
                        pk_val = pk_item.data(Qt.UserRole)
                        if pk_val is None:
                            # **这里跟参照项目不一样，是故意的。** 参照项目写的
                            # 是 `WHERE pk IS NULL`——在霸下这会把**所有**主键
                            # 为空的行一起删掉（主键在各版本上都是空的话，那条
                            # 条件匹配的不止你选的这一行）。宁可这一行不动。
                            continue
                        pk_t = self._预览类型[pk_idx] if pk_idx < len(self._预览类型) else _schema.TEXT
                        try:
                            where_parts.append('`%s` = %s' % (pk, 字面量(pk_val, pk_t)))
                        except ValueError:
                            continue
            if where_parts:
                待发.append((row, '删除 从 `%s` 其中 %s'
                             % (self._preview_table_name, ' 与 '.join(where_parts))))
            else:
                定位不了.append(row)
        if not 待发:
            if 定位不了:
                QMessageBox.warning(
                    self, "删除失败",
                    "选中的行里没有主键值，按主键定位不到它——霸下这边删不了。")
            else:
                QMessageBox.warning(self, "删除失败", "未找到要删除的行，数据可能已被修改。")
            return
        self.engine.发多(
            [sql for _, sql in 待发],
            lambda 结果们, 错: self._收删行(结果们, 错, 待发))

    def _收删行(self, 结果们, 错, 待发) -> None:
        """删行的回话（**在主线程里被叫**）。`发多` 回来的结果跟投出去的
        语句**一一对位**。"""
        if 错 is not None:
            QMessageBox.warning(self, "错误", str(错))
            return
        结果们 = 结果们 or []
        要拿掉 = []
        for i, (row, _sql) in enumerate(待发):
            r = 结果们[i] if i < len(结果们) else None
            if r is not None and r.ok and r.affected > 0:
                要拿掉.append(row)
            else:
                # 引擎说没删着——数据可能已经被别人改了。提示照旧。
                QMessageBox.warning(self, "删除失败", "未找到要删除的行，数据可能已被修改。")
        # 从后往前拿：行号不会因为前面的行没了而错位。
        for row in sorted(set(要拿掉), reverse=True):
            self._data_table.removeRow(row)

    def _on_preview_save_row(self) -> None:
        """Save new row via INSERT. 落成一条 `插入 `t` (列) 值 (值)`。

        值按 **UserRole 里的真值**给：空是 `空`，别的按列类型给字面量（整数
        实数裸写，文本时间加单引号）。
        """
        if self._preview_new_row < 0 or self._preview_new_row >= self._data_table.rowCount():
            self._preview_save_btn.setVisible(False)
            return
        # 强制关闭当前编辑器，确保 IME 输入的中文已提交到 model
        idx = self._data_table.currentIndex()
        if idx.isValid():
            editor = self._data_table.indexWidget(idx)
            if editor:
                self._data_table.commitData(editor)
                self._data_table.closePersistentEditor(self._data_table.item(idx.row(), idx.column()))
        cols = self._preview_columns
        if not cols:
            return
        vals = []
        has_data = False
        for c in range(self._data_table.columnCount()):
            item = self._data_table.item(self._preview_new_row, c)
            if not item:
                vals.append("空"); continue
            t = item.text().strip()
            if t.upper() == "NULL" or t == "":
                vals.append("空")
            else:
                has_data = True
                类型号 = self._预览类型[c] if c < len(self._预览类型) else _schema.TEXT
                真值 = self._文本取值(t, 类型号)
                try:
                    # 二进制列：引擎的 `coerce` 收下这串文字就按 UTF-8 编成
                    # 字节。这是**这门语言自己的规矩**，不是界面在替它拿主意。
                    vals.append(字面量(真值, 类型号))
                except ValueError:
                    vals.append("'%s'" % t.replace("'", "''"))
        if not has_data:
            return
        sql = '插入 `%s` (%s) 值 (%s)' % (
            self._preview_table_name,
            ', '.join(f'`{c}`' for c in cols), ', '.join(vals))
        self.engine.发(sql, self._收插行)

    def _收插行(self, 结果, 错) -> None:
        """插一行的回话（**在主线程里被叫**）。成了就重新拉一遍预览。"""
        if 错 is not None:
            QMessageBox.warning(self, "错误", str(错))
            return
        if 结果 is None or not 结果.ok:
            QMessageBox.warning(
                self, "错误",
                str(结果.error) if 结果 is not None else '(没有回结果)')
            return
        self._preview_new_row = -1
        self._preview_save_btn.setVisible(False)
        self._load_preview_data(self._preview_db_name, self._preview_table_name)

    def _on_open_data_viewer(self) -> None:
        """Open the full DataViewer for this table.

        **参照项目那个 `data_viewer` 是另一个界面文件，没搬过来。** 菜单项留着
        （那是界面上的字），点到了只说一句实话——总比弹一个空窗口强。
        """
        if not self._preview_db_name or not self._preview_table_name:
            return
        QMessageBox.information(
            self, "提示",
            "霸下这边还没有「完整数据查看器」那个面板——它是参照项目里的另一个"
            "界面文件，没有搬过来。右边这个预览表本身就能改单元格、加行、删行。"
        )

    def _on_preview_context_menu(self, pos) -> None:
        """数据预览表格的右键菜单。"""
        if not self._preview_db_name or not self._preview_table_name:
            return

        menu = QMenu(self)
        has_selection = bool(self._data_table.selectedItems())

        menu.addAction("➕ 添加新行", self._on_preview_add_row)
        menu.addSeparator()

        if has_selection:
            items = self._data_table.selectedItems()
            rows_selected = len(set(i.row() for i in items))
            cols_selected = len(set(i.column() for i in items))

            if rows_selected == 1 and cols_selected == 1:
                menu.addAction("📋 复制单元格", self._on_preview_copy_cell)
            if rows_selected >= 1:
                menu.addAction("📋 复制选中行", self._on_preview_copy_rows)

            menu.addAction("🚫 设为 NULL", self._on_preview_set_null)
            menu.addSeparator()
            menu.addAction(f"🗑 删除选中行 ({rows_selected}行)", self._on_preview_del_row)
            menu.addSeparator()

        menu.addAction("👁 完整数据查看器...", self._on_open_data_viewer)
        menu.addAction("📥 导出当前页CSV...", self._on_preview_export_csv)
        menu.addSeparator()
        menu.addAction("↻ 刷新数据", lambda: self._load_preview_data(self._preview_db_name, self._preview_table_name))

        menu.exec_(self._data_table.viewport().mapToGlobal(pos))

    def _on_preview_copy_cell(self) -> None:
        """复制单个选中单元格的内容。"""
        from PySide6.QtWidgets import QApplication
        items = self._data_table.selectedItems()
        if items:
            QApplication.clipboard().setText(items[0].text())

    def _on_preview_copy_rows(self) -> None:
        """复制选中行的数据（制表符分隔）。"""
        from PySide6.QtWidgets import QApplication
        rows = sorted(set(i.row() for i in self._data_table.selectedItems()))
        if not rows:
            return
        lines = []
        cols = self._data_table.columnCount()
        for r in rows:
            vals = []
            for c in range(cols):
                item = self._data_table.item(r, c)
                vals.append(item.text() if item else "NULL")
            lines.append("\t".join(vals))
        QApplication.clipboard().setText("\n".join(lines))

    def _on_preview_set_null(self) -> None:
        """将选中的单元格设置为 NULL。"""
        for item in self._data_table.selectedItems():
            item.setText("NULL")
            item.setForeground(QColor("#94A3B8"))
            f = QFont(); f.setItalic(True); item.setFont(f)
            item.setData(Qt.UserRole, None)

    def _on_preview_export_csv(self) -> None:
        """导出当前预览数据为 CSV。"""
        import csv, os
        from PySide6.QtWidgets import QFileDialog
        default_name = f"{self._preview_db_name}_{self._preview_table_name}_preview.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出CSV", os.path.join(os.getcwd(), default_name),
            "CSV 文件 (*.csv);;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            import io
            output = io.StringIO()
            w = csv.writer(output)
            headers = [self._data_table.horizontalHeaderItem(c).text()
                      for c in range(self._data_table.columnCount())]
            w.writerow(headers)
            for r in range(self._data_table.rowCount()):
                row = [self._data_table.item(r, c).text() if self._data_table.item(r, c) else "NULL"
                      for c in range(self._data_table.columnCount())]
                w.writerow(row)
            # 参照项目走的是 `baxia.utils.encoding.safe_write_text`（那个模块在
            # 霸下没有）。这里就是它本来的做法：**UTF-8 带 BOM**，Excel 打开
            # 中文不乱码。
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(output.getvalue())
            QMessageBox.information(self, "完成", f"已导出到:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # --------------- 辅助方法 ---------------

    def _get_selected_database(self) -> str:
        """获取当前选中的数据库名称。"""
        item = self._tree.currentItem()
        if not item:
            return ""

        item_type = item.data(0, Qt.UserRole)
        if item_type == self.DB_ITEM:
            return item.data(0, Qt.UserRole + 1)
        elif item_type == self.TABLE_ITEM:
            return item.data(0, Qt.UserRole + 1)
        elif item_type == self.COLUMN_ITEM:
            parent = item.parent()
            if parent and parent.data(0, Qt.UserRole) == self.TABLE_ITEM:
                return parent.data(0, Qt.UserRole + 1)
        return ""

    def _get_selected_table(self) -> tuple[str, str]:
        """获取当前选中的 (数据库名, 表名)。"""
        item = self._tree.currentItem()
        if not item:
            return ("", "")

        item_type = item.data(0, Qt.UserRole)
        if item_type == self.TABLE_ITEM:
            return (
                item.data(0, Qt.UserRole + 1),
                item.data(0, Qt.UserRole + 2),
            )
        elif item_type == self.COLUMN_ITEM:
            parent = item.parent()
            if parent and parent.data(0, Qt.UserRole) == self.TABLE_ITEM:
                return (
                    parent.data(0, Qt.UserRole + 1),
                    parent.data(0, Qt.UserRole + 2),
                )
        return ("", "")

    def _select_item_by_name(self, db_name: str, table_name: str = "") -> None:
        """在树中定位并选择指定项。"""
        for i in range(self._tree.topLevelItemCount()):
            db_item = self._tree.topLevelItem(i)
            if db_item.text(0).lower() == db_name.lower():
                if table_name:
                    for j in range(db_item.childCount()):
                        tbl_item = db_item.child(j)
                        if tbl_item.text(0).lower() == table_name.lower():
                            self._tree.setCurrentItem(tbl_item)
                            self._show_detail(tbl_item)
                            return
                self._tree.setCurrentItem(db_item)
                self._show_detail(db_item)
                return
