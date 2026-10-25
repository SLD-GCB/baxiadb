#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面_设计表.py — 三个"设计表"的对话框（搬自 `baxiadb-v1.6/config_gui/ui/dialogs/`）

    CreateTableDialog   建表          （原 create_table_dialog.py）
    EditTableDialog     改一张已有表  （原 edit_table_dialog.py）
    TableDesigner       表设计器：列 / 索引 / 选项（原 table_designer.py）

**界面那部分一个字没动**：布局、控件、文案（含 emoji 和全角符号）、颜色、
tooltip、列头文字、按钮顺序、`setMinimumWidth`/`setMinimumHeight`、确认框
措辞，全照原样。改的只有底下那条数据通路，一共三处：

  1. **类型词表**换成霸下的五个：整数 / 实数 / 文本 / 二进制 / 时间。
     这是**唯一**一处该改的文案——它是这门语言的词表，权威在引擎那边
     （`schema.TYPE_NAMES`），原来那 19 个 MySQL 名字在这个库里是外人。
     连着两样派生出来的东西：类型解析走 `_类型号`（认不出就抛 ValueError，
     和参照项目的 `TypeCode.from_name` 一个脾气，`_on_type_changed` 靠它兜底）；
     长度那一列因为五个类型谁也不带长度，就一直空着、灰着。

  2. **落地不再改内存里的模型再 `save()`**。霸下没有"攒着一起存"这回事，
     也没有"改内存模型"这回事——每条语句下去就直存直取。所以三个对话框
     保存时都是**拼语句**交给 `引擎.发` / `引擎.发多`，回话在主线程里被叫，
     回话里成功了再刷新目录、再 `accept()`。中间不阻塞、不 `processEvents`。

  3. **读模型的地方**从参照项目那套换成霸下的：`col.sql_type` 在霸下是类型
     **名字**（'整数' 这种），不是带 `.length` 的类型对象；`col.default_value`
     是方法，要带上括号。

霸下没有的东西——**控件和文案都留着，只是不参与语句**：

  * 长度 / 精度：五个类型谁也不带长度，长度那一格一直空着（这是真话）。
  * 自增（AUTO_INCREMENT）：霸下没有自增。整数列上那个勾选框照旧能勾
    （控件没动），但保存时不拼进语句——见 `_列定义`。
  * 唯一（UNIQUE）：霸下没有唯一约束。引擎那边连 `建表 唯一` 都不认，
    它会直接报"本库没有唯一约束。要唯一就把它并进主键"。所以那个勾选框
    只是个摆着的格子。
  * 字符集 / 排序规则 / 行格式 / 自增起始值 / 存储引擎：同上，都是摆着的。
  * 注释（表注释、列注释）：霸下的表定义里没有注释这一格，存不住。
  * **改已有列的类型 / 可空 / 默认值 / 主键**：霸下的 `改表` 只有
    加列 / 删列 / 改名 / 换维度列（引擎的报错原文就是这么写的，见
    `syntax.py` 的 parse_alter）。所以这几样**只能如实拒绝**——
    人看着"保存修改"成功、盘上却没变，那是最坏的错答案。
  * **改表名**：霸下也没有这条语句（表名就是它那条目录项），同样如实拒绝。

`改表` 落地时是**一串语句**（删列 → 改名 → 加列，这个次序是挑过的：先删
可以让"改名撞上一个正要走的名字"成立，先改名可以让"新加的列用回刚腾出来
的名字"成立）。**批里没有回滚**——前面成了的就是成了，所以中间失败时
会把目录刷新一遍，让界面显示的是盘上真实的样子，再报错。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QComboBox, QTableWidget, QTableWidgetItem, QPushButton,
    QDialogButtonBox, QLabel, QCheckBox, QMessageBox, QHeaderView,
    QWidget, QTabWidget, QSpinBox,
)
from PySide6.QtCore import Qt

import schema as _schema
import 界面零件 as _零件


# ══ 一、方言：霸下的五个类型 ═══════════════════════════════════════════
#
# 只有一处权威：`schema.TYPE_NAMES`。这里把名字原样拿出来，不自己抄一份。

SQL_TYPES = [
    ("整数", _schema.INT),
    ("实数", _schema.FLOAT),
    ("文本", _schema.TEXT),
    ("二进制", _schema.BLOB),
    ("时间", _schema.TIME),
]

# 霸下没有"长度/精度"这回事：类型只有五个，谁也不带长度。
# 留着一个空集合是为了 `_on_type_changed` 那段逻辑原样能跑。
TYPES_NEEDING_LENGTH = set()

# 自增只对整数类型"有资格"——勾选框的启用/禁用照这个走（照旧是参照项目
# 那套）。但**保存时一律不拼**：霸下没有自增，见 `_列定义`。
TYPES_ALLOWING_AUTO_INCREMENT = {_schema.INT}


def _类型号(名):
    """类型名 -> 霸下的类型号。认不出就抛 ValueError。

    和参照项目的 `TypeCode.from_name` 一个脾气——`_on_type_changed` 那边
    是靠 `except ValueError` 兜底的。
    """
    号 = _schema.TYPE_ALIASES.get(名)
    if 号 is None:
        raise ValueError('认不出的类型：%s' % 名)
    return 号


def _类型字(号):
    """类型号 -> 霸下认的类型名（拼语句用）。"""
    return _schema.TYPE_NAMES.get(号, '文本')


def 字面量(值, 类型号):
    """把值拼成霸下的字面量。

    **文本和时间加单引号（里面的单引号写成两个），整数实数裸写。**
    （`sql.py` 的词法里没有反斜杠转义，转义只有"两个单引号"这一种。）

    `值` 可以是模型里的真值（`结果.rows` 里那个），也可以是界面上敲出来
    的文本，两种都认。

    **空值不走这里**：空要写成一个 `空`，而且不能用 `=` 比（引擎会拒），
    判空一律写 `为空`。
    """
    if 类型号 in (_schema.INT, _schema.FLOAT):
        try:
            float(str(值))
        except ValueError:
            pass                     # 不是个数：还是加引号，让引擎按列类型报
        else:
            return str(值)
    if 类型号 == _schema.BLOB and isinstance(值, (bytes, bytearray)):
        # 二进制值的字面量给不出来：渲染出来是 `b'\x00…'` 那种样子，写成
        # 文本字面量存回去的就是另一串字节了。宁可现在说，不要静默存错。
        raise ValueError('二进制的值给不出字面量（渲染出来的是另一串字节）')
    return "'%s'" % str(值).replace("'", "''")


def _默认文本(列字):
    """比对默认值用的样子。字典里没有 `default` 这个键（表设计器那张网格
    就没有默认值一列）时，当成"没有默认值"。"""
    值 = 列字.get('default')
    return '' if 值 is None else str(值)


def _列定义(列名, 类型号, 主键=False, 非空=False, 默认=None):
    """一段建表用的列定义：

        `列名` 类型 [主键] [非空] [默认 字面量]

    **长度、自增、唯一、注释都不拼**——霸下没有这几样，拼进去引擎也不认
    （自增/唯一那两个勾选框只是留着的地方）。默认值不拼 `None`：那表示
    这一列没有默认值。
    """
    段 = '`%s` %s' % (列名, _类型字(类型号))
    if 主键:
        段 += ' 主键'
    if 非空:
        段 += ' 非空'
    if 默认 is not None:
        段 += ' 默认 ' + 字面量(默认, 类型号)
    return 段


def _建表语句(表名, 列们):
    """`建表 表 (列定义, …)`。

    **表必须有主键**，引擎会拒绝没有主键的表（它的点查是按键直取的）。
    这里不自己拦——没勾主键就让它落到引擎那边，人看到的是引擎那句中文
    报错，比界面自己编一句更接近事实。
    """
    段们 = [_列定义(c['name'], c['type_code'], c['pk'], c['nn'],
                   c.get('default')) for c in 列们]
    return '建表 `%s` (%s)' % (表名, ', '.join(段们))


def _结构差(表名, 旧列们, 新列们):
    """把"旧结构 -> 新结构"这一步改动翻成霸下能说的 `改表` 语句。

    `旧列们` / `新列们` 一列一个字典，只认这几个键：
    `name` / `type_code` / `nn` / `pk` / `default`（长度、注释、自增、唯一
    这几样霸下本来就存不住，不参与比对）。

    返回 `(语句们, 做不到的理由)`，两条里总有一条是空的。做不到的时候
    **如实说做不到**——把改不动的那部分默默吞掉，比报错坏得多：人以为
    保存成功了，盘上的结构其实没动。

    能说的只有三样，正好是引擎 `改表` 支持的那三样：删列（连带删索引）、
    改名、加列。
    """
    旧名 = [c['name'] for c in 旧列们]
    新名 = [c['name'] for c in 新列们]
    旧按名 = {c['name']: c for c in 旧列们}

    # ① 认"改列名"。判据：同一个位置上换了名字，而那个老名字在整张新表里
    #    再没出现过、新名字在老表里也没有——那就是改了个名，不是"删一列
    #    再加一列"（后者会把那一列的数据白白丢掉，而引擎本来是支持改名的）。
    改名 = {}
    for i in range(min(len(旧名), len(新名))):
        if 旧名[i] != 新名[i] and 旧名[i] not in 新名 and 新名[i] not in 旧名:
            改名[旧名[i]] = 新名[i]
    新到老 = {新: 老 for 老, 新 in 改名.items()}

    def 规范(名):
        """新名字 -> 它在老表里的名字（没改过名的就是它自己）。"""
        return 新到老.get(名, 名)

    旧序 = [n for n in 旧名 if n in set(新名) or n in 改名]
    新序 = [规范(c['name']) for c in 新列们
            if c['name'] in 旧按名 or c['name'] in 新到老]
    if 旧序 != 新序:
        return [], ('列的顺序变了。霸下的 `改表` 没有"换列序"这条语句——'
                    '列的次序在 `建表` 那一刻就定下了。要换只能新建一张表，'
                    '再把数据搬过去。')

    新按规范 = {规范(c['name']): c for c in 新列们}

    # ② 留下来的那几列：定义动没动？动过就说做不到。
    动过 = []
    for n in 旧序:
        旧 = 旧按名[n]
        新 = 新按规范[n]
        if (旧['type_code'] != 新['type_code'] or 旧['nn'] != 新['nn']
                or 旧['pk'] != 新['pk']
                or _默认文本(旧) != _默认文本(新)):
            动过.append(n)
    if 动过:
        return [], ('列 %s 的定义改了。霸下的 `改表` 只能加列 / 删列 / 改列名 / '
                    '换维度列——改不了已有列的类型、可空、默认值和主键（引擎'
                    '自己的报错里就是这么写的）。要改这几样，只能新建一张表'
                    '再把数据搬过去。' % '、'.join(动过))

    # ③ 真能说的三样。次序是有讲究的：先删、再改名、最后加列。
    语句们 = []
    for n in 旧名:
        if n not in set(新名) and n not in 改名:
            # `连带删索引`：删的这一列上要是有索引，引擎会拒绝（索引条目的键
            # 是按那一列编的，列没了它就切不动自己的键）。人到这一步已经
            # 在确认框里点过"确定要删除列…此操作不可撤销"了，连带把它名下
            # 的索引带走是那句话的本意——所以带上这个逃生门。
            语句们.append('改表 `%s` 删列 `%s` 连带删索引' % (表名, n))
    for 老, 新 in 改名.items():
        # **改名不带 `连带删索引`**：引擎那边会拒（改名不用逃生门，索引表
        # 跟着一起改名，条目一条不丢）。
        语句们.append('改表 `%s` 改名 `%s` 到 `%s`' % (表名, 老, 新))
    for c in 新列们:
        if c['name'] in 旧按名 or c['name'] in 新到老:
            continue
        if c['pk']:
            # 主键编在每一行的行键里，所以 `改表 … 加列 x 整数 主键` 后面
            # 那个 `主键` 引擎是**不看的**（加进去也不进主键）。与其默默
            # 按没有主键存下来，不如现在说清楚。
            return [], ('新加的列 %s 不能是主键：主键编在每一行的行键里，'
                        '往里加一列等于把整张表的行键换掉，`改表 … 加列` '
                        '后面那个 `主键` 引擎本来看不看的。要这样的表只能'
                        '新建一张。' % c['name'])
        语句们.append('改表 `%s` 加列 %s'
                      % (表名, _列定义(c['name'], c['type_code'], c['pk'],
                                      c['nn'], c.get('default'))))
    return 语句们, None


# ══ 二、建表 ═══════════════════════════════════════════════════════════


class CreateTableDialog(QDialog):
    """创建表的对话框 -- 包含列编辑器。"""

    COL_NAME = 0
    COL_TYPE = 1
    COL_LENGTH = 2
    COL_PK = 3
    COL_NN = 4
    COL_UQ = 5
    COL_AI = 6
    COL_DEFAULT = 7
    COL_COMMENT = 8

    COLUMN_HEADERS = [
        "列名", "类型", "长度/精度", "主键", "非空", "唯一", "自增", "默认值", "注释"
    ]

    def __init__(self, engine, database_name: str = "", parent=None):
        super().__init__(parent)
        self.engine = engine
        self.database_name = database_name
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("创建表")
        self.setMinimumWidth(900)
        self.setMinimumHeight(550)
        self.setModal(True)

        layout = QVBoxLayout(self)

        # 标题
        title = QLabel(f"新建表 -- 数据库: {self.database_name or '(未选择)'}")
        title.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px 0;")
        layout.addWidget(title)

        # 表名
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("表名:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("请输入表名称 (例如: users)")
        self._name_edit.setMinimumWidth(200)
        name_layout.addWidget(self._name_edit)
        name_layout.addWidget(QLabel("注释:"))
        self._comment_edit = QLineEdit()
        self._comment_edit.setPlaceholderText("可选: 表注释")
        name_layout.addWidget(self._comment_edit)
        name_layout.addStretch()
        layout.addLayout(name_layout)

        # 列编辑器表格
        col_label = QLabel("列定义:")
        col_label.setStyleSheet("font-weight: bold; padding-top: 12px;")
        layout.addWidget(col_label)

        self._col_table = QTableWidget()
        self._col_table.setColumnCount(len(self.COLUMN_HEADERS))
        self._col_table.setHorizontalHeaderLabels(self.COLUMN_HEADERS)
        self._col_table.horizontalHeader().setSectionResizeMode(
            self.COL_NAME, QHeaderView.Stretch
        )
        self._col_table.horizontalHeader().setSectionResizeMode(
            self.COL_TYPE, QHeaderView.ResizeToContents
        )
        self._col_table.horizontalHeader().setSectionResizeMode(
            self.COL_LENGTH, QHeaderView.ResizeToContents
        )
        self._col_table.horizontalHeader().setSectionResizeMode(
            self.COL_COMMENT, QHeaderView.Stretch
        )
        self._col_table.setMinimumHeight(250)
        layout.addWidget(self._col_table)

        # 添加默认第一行
        self._add_row()

        # 列操作按钮
        col_btn_layout = QHBoxLayout()
        add_btn = QPushButton("+ 添加列")
        add_btn.clicked.connect(self._add_row)
        col_btn_layout.addWidget(add_btn)

        del_btn = QPushButton("- 删除选中列")
        del_btn.clicked.connect(self._remove_row)
        col_btn_layout.addWidget(del_btn)

        up_btn = QPushButton(" 上移")
        up_btn.clicked.connect(self._move_up)
        col_btn_layout.addWidget(up_btn)

        down_btn = QPushButton(" 下移")
        down_btn.clicked.connect(self._move_down)
        col_btn_layout.addWidget(down_btn)

        col_btn_layout.addStretch()
        layout.addLayout(col_btn_layout)

        # 按钮
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Ok).setText("创建表")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        layout.addWidget(buttons)

    def _add_row(self) -> None:
        row = self._col_table.rowCount()
        self._col_table.insertRow(row)

        # 列名
        self._col_table.setItem(row, self.COL_NAME, QTableWidgetItem(""))

        # 类型下拉
        type_combo = QComboBox()
        for type_name, _ in SQL_TYPES:
            type_combo.addItem(type_name)
        type_combo.setCurrentText("整数")
        type_combo.currentTextChanged.connect(
            lambda text, r=row: self._on_type_changed(r, text)
        )
        self._col_table.setCellWidget(row, self.COL_TYPE, type_combo)

        # 长度（自定义输入，填多少是多少）
        # 霸下没有长度这回事，这一格永远是灰的、空的——引擎的类型只有五个。
        length_edit = QLineEdit()
        length_edit.setPlaceholderText("默认")
        length_edit.setEnabled(False)
        length_edit.setMinimumWidth(60)
        self._col_table.setCellWidget(row, self.COL_LENGTH, length_edit)

        # 主键 checkbox
        pk_widget = self._make_centered_checkbox()
        self._col_table.setCellWidget(row, self.COL_PK, pk_widget)

        # 非空 checkbox
        nn_widget = self._make_centered_checkbox()
        self._col_table.setCellWidget(row, self.COL_NN, nn_widget)

        # 唯一 checkbox（霸下没有唯一约束，保存时不拼，见文件头）
        uq_widget = self._make_centered_checkbox()
        self._col_table.setCellWidget(row, self.COL_UQ, uq_widget)

        # 自增 checkbox（霸下没有自增，保存时不拼，见文件头）
        ai_widget = self._make_centered_checkbox()
        self._col_table.setCellWidget(row, self.COL_AI, ai_widget)

        # 默认值
        self._col_table.setItem(row, self.COL_DEFAULT, QTableWidgetItem(""))

        # 注释
        self._col_table.setItem(row, self.COL_COMMENT, QTableWidgetItem(""))

    def _make_centered_checkbox(self) -> QWidget:
        """创建一个居中的 checkbox 容器。"""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)
        cb = QCheckBox()
        layout.addWidget(cb)
        return widget

    def _get_checkbox(self, row: int, col: int) -> QCheckBox:
        widget = self._col_table.cellWidget(row, col)
        if widget:
            return widget.findChild(QCheckBox)
        return None

    def _on_type_changed(self, row: int, type_name: str) -> None:
        """当类型改变时，启用/禁用长度字段并填入建议默认值。"""
        if row < 0 or row >= self._col_table.rowCount():
            return
        try:
            # Strip "(中文)" suffix for type resolution
            clean_name = type_name.split(" (")[0] if " (" in type_name else type_name
            code = _类型号(clean_name)
            length_widget = self._col_table.cellWidget(row, self.COL_LENGTH)
            if length_widget and isinstance(length_widget, QLineEdit):
                # 霸下没有长度：`TYPES_NEEDING_LENGTH` 是空集，所以这一格
                # 一直是灰的、一直是空的。分支留着，是为了哪天引擎真加了
                # 长度的说法，这儿不用重写。
                if code in TYPES_NEEDING_LENGTH:
                    length_widget.setEnabled(True)
                    if code == _schema.TEXT:
                        length_widget.setText("255")
                    else:
                        length_widget.setText("")
                else:
                    length_widget.setEnabled(False)
                    length_widget.setText("")

            # 自增仅对整数类型有效
            ai_cb = self._get_checkbox(row, self.COL_AI)
            if ai_cb:
                if code not in TYPES_ALLOWING_AUTO_INCREMENT:
                    ai_cb.setChecked(False)
                    ai_cb.setEnabled(False)
                else:
                    ai_cb.setEnabled(True)
        except ValueError:
            pass

    def _remove_row(self) -> None:
        current = self._col_table.currentRow()
        if current >= 0 and self._col_table.rowCount() > 1:
            self._col_table.removeRow(current)

    def _move_up(self) -> None:
        current = self._col_table.currentRow()
        if current > 0:
            self._swap_rows(current, current - 1)
            self._col_table.selectRow(current - 1)

    def _move_down(self) -> None:
        current = self._col_table.currentRow()
        if current >= 0 and current < self._col_table.rowCount() - 1:
            self._swap_rows(current, current + 1)
            self._col_table.selectRow(current + 1)

    def _swap_rows(self, row1: int, row2: int) -> None:
        """交换两行的所有数据 (try/finally 保证控件不丢失)。"""
        cols = self._col_table.columnCount()
        row1_data = {}
        row2_data = {}
        # 持有强引用，防止 removeCellWidget 期间被 GC 回收
        all_widgets = []

        for col in range(cols):
            widget1 = self._col_table.cellWidget(row1, col)
            widget2 = self._col_table.cellWidget(row2, col)
            item1 = self._col_table.item(row1, col)
            item2 = self._col_table.item(row2, col)

            row1_data[col] = (widget1, item1)
            row2_data[col] = (widget2, item2)
            if widget1:
                all_widgets.append(widget1)
            if widget2:
                all_widgets.append(widget2)

        try:
            # 移除所有控件
            for col in range(cols):
                self._col_table.removeCellWidget(row1, col)
                self._col_table.removeCellWidget(row2, col)

            # 重新设置
            for col in range(cols):
                w2, i2 = row2_data[col]
                w1, i1 = row1_data[col]

                if w2 is not None:
                    self._col_table.setCellWidget(row1, col, w2)
                if i2 is not None:
                    self._col_table.setItem(row1, col, QTableWidgetItem(i2))

                if w1 is not None:
                    self._col_table.setCellWidget(row2, col, w1)
                if i1 is not None:
                    self._col_table.setItem(row2, col, QTableWidgetItem(i1))
        finally:
            # 确保强引用在操作完成前一直存在
            all_widgets.clear()

    def _collect_columns(self) -> list[dict]:
        """从表格中收集所有列定义。"""
        columns = []
        for row in range(self._col_table.rowCount()):
            name_item = self._col_table.item(row, self.COL_NAME)
            name = name_item.text().strip() if name_item else ""
            if not name:
                continue  # 跳过空行

            type_combo = self._col_table.cellWidget(row, self.COL_TYPE)
            type_name = type_combo.currentText() if type_combo else "整数"
            # Strip Chinese label suffix, e.g. "NVARCHAR (中文)" → "NVARCHAR"
            clean_name = type_name.split(" (")[0] if " (" in type_name else type_name
            type_code = _类型号(clean_name)

            length_edit = self._col_table.cellWidget(row, self.COL_LENGTH)
            length = 0
            if length_edit and isinstance(length_edit, QLineEdit):
                text = length_edit.text().strip()
                if text:
                    try:
                        length = int(text)
                    except ValueError:
                        pass  # 保留 0，后续校验时会提示

            pk_cb = self._get_checkbox(row, self.COL_PK)
            pk = pk_cb.isChecked() if pk_cb else False

            nn_cb = self._get_checkbox(row, self.COL_NN)
            nn = nn_cb.isChecked() if nn_cb else False

            uq_cb = self._get_checkbox(row, self.COL_UQ)
            uq = uq_cb.isChecked() if uq_cb else False

            ai_cb = self._get_checkbox(row, self.COL_AI)
            ai = ai_cb.isChecked() if ai_cb else False

            default_item = self._col_table.item(row, self.COL_DEFAULT)
            default = default_item.text().strip() if default_item else None
            if default == "":
                default = None

            comment_item = self._col_table.item(row, self.COL_COMMENT)
            comment = comment_item.text().strip() if comment_item else ""

            columns.append({
                "name": name,
                "type_code": type_code,
                "length": length,
                "pk": pk,
                "nn": nn,
                "uq": uq,
                "ai": ai,
                "default": default,
                "comment": comment,
            })
        return columns

    def _on_accept(self) -> None:
        table_name = self._name_edit.text().strip()
        if not table_name:
            QMessageBox.warning(self, "输入错误", "请输入表名称。")
            return

        import re
        # Allow Unicode letters (Chinese, etc.), digits, underscores
        if not re.match(r'^(?:[_]|[^\W\d_])\w*$', table_name):
            QMessageBox.warning(
                self, "名称无效",
                "表名称必须以字母(含中文)或下划线开头，只能包含字母、数字、中文和下划线。"
            )
            return

        # 检查数据库
        db_name = self.database_name or self.engine.current_database
        if not db_name:
            QMessageBox.warning(self, "错误", "未选择数据库，请先在左侧选择一个数据库。")
            return

        # 检查表是否存在
        # （霸下的 `目录.has_table` 是 `(库名, 表名)` 这个次序，和参照项目
        #   反着——这里照霸下的来。）
        if self.engine.catalog.has_table(db_name, table_name):
            QMessageBox.warning(self, "已存在", f"表 '{table_name}' 已存在于数据库 '{db_name}' 中。")
            return

        columns_data = self._collect_columns()
        if not columns_data:
            QMessageBox.warning(self, "输入错误", "请至少定义一个列。")
            return

        # 检查列名重复
        col_names = [c["name"].lower() for c in columns_data]
        if len(col_names) != len(set(col_names)):
            QMessageBox.warning(self, "列名重复", "存在重复的列名，请确保每个列名唯一。")
            return

        # 验证：VARCHAR/CHAR/DECIMAL 必须输入有效的长度
        # （霸下没有长度这回事，`TYPES_NEEDING_LENGTH` 是空集，所以这一段
        #   现在走不到。留着是为了那段提示文案字字都在。）
        for col_data in columns_data:
            tc = col_data["type_code"]
            if tc in TYPES_NEEDING_LENGTH:
                if col_data["length"] <= 0:
                    QMessageBox.warning(
                        self, "类型错误",
                        f"列 '{col_data['name']}' 类型为 {_类型字(tc)}，请输入有效的长度值（正整数）。"
                    )
                    return
            if col_data["ai"] and tc not in TYPES_ALLOWING_AUTO_INCREMENT:
                QMessageBox.warning(
                    self, "类型错误",
                    f"列 '{col_data['name']}': 只有整数类型支持自增。"
                )
                return

        # 落地：霸下没有"改内存模型再 save"这回事，拼一条 `建表` 发出去。
        # 长度、自增、唯一、注释都不进语句（霸下没有这几样）。
        try:
            语句 = _建表语句(table_name, columns_data)
        except ValueError as e:
            QMessageBox.critical(self, "错误", f"创建表失败: {e}")
            return

        self.engine.发(语句, self._收保存)

    # ---- 回话（在主线程里被叫） --------------------------------------

    def _收保存(self, 结果, 错) -> None:
        """`建表` 的回话。**语句写错不算 `错`**——那时候 `结果.ok` 是
        False、`结果.error` 里是引擎那句中文。"""
        if 错 is not None:
            QMessageBox.critical(self, "错误", f"创建表失败: {错}")
            return
        if 结果 is None or not 结果.ok:
            QMessageBox.critical(
                self, "错误",
                f"创建表失败: {结果.error if 结果 is not None else '没有回结果'}")
            return
        # 投出去之后不能立刻接着读目录——要在回话里读。先把目录拉新的，
        # 拉回来了再关对话框，好让面板那边的树一刷新就能看见这张新表。
        self.engine.catalog.刷新(self._收刷新)

    def _收刷新(self, 错) -> None:
        if 错 is not None:
            QMessageBox.critical(self, "错误", f"创建表失败: {错}")
            return
        self.accept()

    def get_table_name(self) -> str:
        return self._name_edit.text().strip()


# ══ 三、编辑表结构 ═════════════════════════════════════════════════════


class EditTableDialog(QDialog):
    """编辑表结构的对话框。"""

    COL_NAME = 0
    COL_TYPE = 1
    COL_LENGTH = 2
    COL_PK = 3
    COL_NN = 4
    COL_UQ = 5
    COL_AI = 6
    COL_DEFAULT = 7
    COL_COMMENT = 8

    COLUMN_HEADERS = [
        "列名", "类型", "长度/精度", "主键", "非空", "唯一", "自增", "默认值", "注释"
    ]

    def __init__(self, engine, database_name: str, table_name: str, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.database_name = database_name
        self.table_name = table_name
        self._original_columns = []
        self._setup_ui()
        self._load_table()

    def _setup_ui(self) -> None:
        self.setWindowTitle(f"编辑表 -- {self.database_name}.{self.table_name}")
        self.setMinimumWidth(900)
        self.setMinimumHeight(550)
        self.setModal(True)

        layout = QVBoxLayout(self)

        # 标题
        title = QLabel(f"编辑表结构: {self.database_name}.{self.table_name}")
        title.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px 0;")
        layout.addWidget(title)

        # 表名
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("表名:"))
        self._name_edit = QLineEdit(self.table_name)
        self._name_edit.setMinimumWidth(200)
        name_layout.addWidget(self._name_edit)
        name_layout.addWidget(QLabel("注释:"))
        self._comment_edit = QLineEdit()
        name_layout.addWidget(self._comment_edit)
        name_layout.addStretch()
        layout.addLayout(name_layout)

        # 列编辑器
        col_label = QLabel("列定义:")
        col_label.setStyleSheet("font-weight: bold; padding-top: 12px;")
        layout.addWidget(col_label)

        self._col_table = QTableWidget()
        self._col_table.setColumnCount(len(self.COLUMN_HEADERS))
        self._col_table.setHorizontalHeaderLabels(self.COLUMN_HEADERS)
        self._col_table.horizontalHeader().setSectionResizeMode(
            self.COL_NAME, QHeaderView.Stretch)
        self._col_table.horizontalHeader().setSectionResizeMode(
            self.COL_COMMENT, QHeaderView.Stretch)
        self._col_table.setMinimumHeight(250)
        layout.addWidget(self._col_table)

        # 列操作按钮
        col_btn_layout = QHBoxLayout()
        add_btn = QPushButton("+ 添加列")
        add_btn.clicked.connect(self._add_row)
        col_btn_layout.addWidget(add_btn)
        del_btn = QPushButton("- 删除选中列")
        del_btn.clicked.connect(self._remove_row)
        col_btn_layout.addWidget(del_btn)
        up_btn = QPushButton(" 上移")
        up_btn.clicked.connect(self._move_up)
        col_btn_layout.addWidget(up_btn)
        down_btn = QPushButton(" 下移")
        down_btn.clicked.connect(self._move_down)
        col_btn_layout.addWidget(down_btn)
        col_btn_layout.addStretch()
        layout.addLayout(col_btn_layout)

        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Ok).setText("保存修改")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        layout.addWidget(buttons)

    def _load_table(self) -> None:
        """从引擎加载现有表定义并填充到编辑器。

        **读的是目录缓存**（同步读，不卡），刷新由 `目录.刷新` 在别处投活做。
        """
        try:
            db = self.engine.catalog.get_database(self.database_name)
            table = db.table(self.table_name)

            self._comment_edit.setText(table.comment)

            # 留一份"原来的样子"，保存时拿它跟网格里的比——霸下的 `改表`
            # 只能加列 / 删列 / 改名，比出来是把已有列改掉了就得如实拒绝。
            self._original_columns = [
                {'name': col.name, 'type_code': col.type_code,
                 'nn': not col.nullable, 'pk': col.primary_key,
                 'default': col.default_value()}
                for col in table.columns
            ]

            for col in table.columns:
                row = self._col_table.rowCount()
                self._col_table.insertRow(row)

                # 列名
                self._col_table.setItem(row, self.COL_NAME,
                                        QTableWidgetItem(col.name))

                # 类型
                type_combo = QComboBox()
                for type_name, _ in SQL_TYPES:
                    type_combo.addItem(type_name)
                # 霸下的模型上，类型是**名字**（'整数' 这种）和类型号两样，
                # 没有参照项目那个带 `.name` 的类型对象。
                type_name = _类型字(col.type_code)
                if type_combo.findText(type_name) >= 0:
                    type_combo.setCurrentText(type_name)
                type_combo.currentTextChanged.connect(
                    lambda text, r=row: self._on_type_changed(r, text))
                self._col_table.setCellWidget(row, self.COL_TYPE, type_combo)

                # 长度（自定义输入）
                # 霸下没有长度：类型只有五个，谁也不带长度，所以一直是空的。
                length_edit = QLineEdit()
                length_edit.setPlaceholderText("默认")
                length_edit.setEnabled(col.type_code in TYPES_NEEDING_LENGTH)
                length_edit.setMinimumWidth(60)
                self._col_table.setCellWidget(row, self.COL_LENGTH, length_edit)

                # PK
                pk_widget = self._make_checkbox_widget(col.primary_key)
                self._col_table.setCellWidget(row, self.COL_PK, pk_widget)

                # NN
                nn_widget = self._make_checkbox_widget(not col.nullable)
                self._col_table.setCellWidget(row, self.COL_NN, nn_widget)

                # UQ（霸下没有唯一约束：模型上恒为 False，所以这一格永远是
                # 不勾的。这是真话，不是加载坏了）
                uq_widget = self._make_checkbox_widget(col.unique)
                self._col_table.setCellWidget(row, self.COL_UQ, uq_widget)

                # AI（同上：霸下没有自增）
                ai_widget = self._make_checkbox_widget(col.auto_increment)
                self._col_table.setCellWidget(row, self.COL_AI, ai_widget)

                # 默认值（模型上是方法，要带括号）
                default_value = col.default_value()
                default_str = str(default_value) if default_value is not None else ""
                self._col_table.setItem(row, self.COL_DEFAULT,
                                        QTableWidgetItem(default_str))

                # 注释（霸下的表定义里没有注释这一格，恒为空串）
                self._col_table.setItem(row, self.COL_COMMENT,
                                        QTableWidgetItem(col.comment))

        except Exception as e:
            QMessageBox.critical(self, "错误", f"加载表定义失败: {e}")
            self.reject()

    def _make_checkbox_widget(self, checked: bool = False) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)
        cb = QCheckBox()
        cb.setChecked(checked)
        layout.addWidget(cb)
        return widget

    def _get_checkbox(self, row: int, col: int) -> QCheckBox:
        widget = self._col_table.cellWidget(row, col)
        if widget:
            return widget.findChild(QCheckBox)
        return None

    def _add_row(self) -> None:
        row = self._col_table.rowCount()
        self._col_table.insertRow(row)
        self._col_table.setItem(row, self.COL_NAME, QTableWidgetItem(""))

        type_combo = QComboBox()
        for type_name, _ in SQL_TYPES:
            type_combo.addItem(type_name)
        type_combo.setCurrentText("整数")
        type_combo.currentTextChanged.connect(
            lambda text, r=row: self._on_type_changed(r, text))
        self._col_table.setCellWidget(row, self.COL_TYPE, type_combo)

        length_edit = QLineEdit()
        length_edit.setPlaceholderText("默认")
        length_edit.setEnabled(False)
        length_edit.setMinimumWidth(60)
        self._col_table.setCellWidget(row, self.COL_LENGTH, length_edit)

        for col_idx in [self.COL_PK, self.COL_NN, self.COL_UQ, self.COL_AI]:
            self._col_table.setCellWidget(row, col_idx,
                                          self._make_checkbox_widget(False))
        self._col_table.setItem(row, self.COL_DEFAULT, QTableWidgetItem(""))
        self._col_table.setItem(row, self.COL_COMMENT, QTableWidgetItem(""))

    def _on_type_changed(self, row: int, type_name: str) -> None:
        # Guard: row may be stale after row removals
        if row < 0 or row >= self._col_table.rowCount():
            return
        try:
            clean_name = type_name.split(" (")[0] if " (" in type_name else type_name
            code = _类型号(clean_name)
            length_widget = self._col_table.cellWidget(row, self.COL_LENGTH)
            if length_widget and isinstance(length_widget, QLineEdit):
                if code in TYPES_NEEDING_LENGTH:
                    length_widget.setEnabled(True)
                    if code == _schema.TEXT:
                        length_widget.setText("255")
                else:
                    length_widget.setEnabled(False)
                    length_widget.setText("")

            ai_cb = self._get_checkbox(row, self.COL_AI)
            if ai_cb:
                if code not in TYPES_ALLOWING_AUTO_INCREMENT:
                    ai_cb.setChecked(False)
                    ai_cb.setEnabled(False)
                else:
                    ai_cb.setEnabled(True)
        except ValueError:
            pass

    def _remove_row(self) -> None:
        current = self._col_table.currentRow()
        if current >= 0 and self._col_table.rowCount() > 0:
            # 确认删除有内容的行
            name_item = self._col_table.item(current, self.COL_NAME)
            col_name = name_item.text().strip() if name_item else ""
            if col_name:
                reply = QMessageBox.question(
                    self, "确认删除",
                    f"确定要删除列 '{col_name}' 吗？\n此操作不可撤销！",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                )
                if reply != QMessageBox.Yes:
                    return
            self._col_table.removeRow(current)

    def _move_up(self) -> None:
        current = self._col_table.currentRow()
        if current > 0:
            self._swap_rows(current, current - 1)
            self._col_table.selectRow(current - 1)

    def _move_down(self) -> None:
        current = self._col_table.currentRow()
        if current >= 0 and current < self._col_table.rowCount() - 1:
            self._swap_rows(current, current + 1)
            self._col_table.selectRow(current + 1)

    def _swap_rows(self, row1: int, row2: int) -> None:
        """交换两行数据 (try/finally 保证控件不会丢失)。"""
        cols = self._col_table.columnCount()
        row1_data, row2_data = {}, {}
        all_widgets = []
        for col in range(cols):
            w1 = self._col_table.cellWidget(row1, col)
            w2 = self._col_table.cellWidget(row2, col)
            i1 = self._col_table.item(row1, col)
            i2 = self._col_table.item(row2, col)
            row1_data[col] = (w1, i1)
            row2_data[col] = (w2, i2)
            if w1: all_widgets.append(w1)
            if w2: all_widgets.append(w2)
        try:
            for col in range(cols):
                self._col_table.removeCellWidget(row1, col)
                self._col_table.removeCellWidget(row2, col)
            for col in range(cols):
                w2, i2 = row2_data[col]
                w1, i1 = row1_data[col]
                if w2 is not None:
                    self._col_table.setCellWidget(row1, col, w2)
                if i2 is not None:
                    self._col_table.setItem(row1, col, QTableWidgetItem(i2))
                if w1 is not None:
                    self._col_table.setCellWidget(row2, col, w1)
                if i1 is not None:
                    self._col_table.setItem(row2, col, QTableWidgetItem(i1))
        finally:
            all_widgets.clear()

    def _collect_columns(self) -> list[dict]:
        columns = []
        for row in range(self._col_table.rowCount()):
            name_item = self._col_table.item(row, self.COL_NAME)
            name = name_item.text().strip() if name_item else ""
            if not name:
                continue

            type_combo = self._col_table.cellWidget(row, self.COL_TYPE)
            type_name = type_combo.currentText() if type_combo else "整数"
            # Strip "(中文)" label suffix for type resolution
            type_name_clean = type_name.split(" (")[0] if " (" in type_name else type_name
            type_code = _类型号(type_name_clean)

            length_edit = self._col_table.cellWidget(row, self.COL_LENGTH)
            length = 0
            if length_edit and isinstance(length_edit, QLineEdit):
                text = length_edit.text().strip()
                if text:
                    try:
                        length = int(text)
                    except ValueError:
                        pass

            pk_cb = self._get_checkbox(row, self.COL_PK)
            nn_cb = self._get_checkbox(row, self.COL_NN)
            uq_cb = self._get_checkbox(row, self.COL_UQ)
            ai_cb = self._get_checkbox(row, self.COL_AI)

            default_item = self._col_table.item(row, self.COL_DEFAULT)
            default = default_item.text().strip() if default_item else None
            if default == "":
                default = None
            comment_item = self._col_table.item(row, self.COL_COMMENT)
            comment = comment_item.text().strip() if comment_item else ""

            columns.append({
                "name": name,
                "type_code": type_code,
                "length": length,
                "pk": pk_cb.isChecked() if pk_cb else False,
                "nn": nn_cb.isChecked() if nn_cb else False,
                "uq": uq_cb.isChecked() if uq_cb else False,
                "ai": ai_cb.isChecked() if ai_cb else False,
                "default": default,
                "comment": comment,
            })
        return columns

    def _on_accept(self) -> None:
        new_name = self._name_edit.text().strip()
        if not new_name:
            QMessageBox.warning(self, "输入错误", "请输入表名称。")
            return

        import re
        if not re.match(r'^(?:[_]|[^\W\d_])\w*$', new_name):
            QMessageBox.warning(
                self, "名称无效",
                "表名称必须以字母(含中文)或下划线开头，只能包含字母、数字、中文和下划线。"
            )
            return

        columns_data = self._collect_columns()
        if not columns_data:
            QMessageBox.warning(self, "输入错误", "请至少定义一个列。")
            return

        # 检查列名重复
        col_names = [c["name"].lower() for c in columns_data]
        if len(col_names) != len(set(col_names)):
            QMessageBox.warning(self, "列名重复", "存在重复的列名。")
            return

        # 验证
        for col_data in columns_data:
            tc = col_data["type_code"]
            if tc in TYPES_NEEDING_LENGTH and col_data["length"] <= 0:
                QMessageBox.warning(
                    self, "类型错误",
                    f"列 '{col_data['name']}' 类型需要指定长度。")
                return
            if col_data["ai"] and tc not in TYPES_ALLOWING_AUTO_INCREMENT:
                QMessageBox.warning(
                    self, "类型错误",
                    f"列 '{col_data['name']}': 自增仅对整数类型有效。")
                return

        # 落地：霸下没有"改内存模型再 save"。改表名先拦下来——霸下没有
        # 这条语句，只能如实说。
        if new_name != self.table_name:
            QMessageBox.warning(
                self, "不支持",
                "霸下没有「改表名」这条语句（表名就是它那条目录项，改名要"
                "把整张表的行键都换掉）。要换名字请新建一张表，再把数据"
                "搬过去。")
            return

        新列们 = [{'name': c['name'], 'type_code': c['type_code'],
                   'nn': c['nn'], 'pk': c['pk'], 'default': c['default']}
                  for c in columns_data]
        语句们, 做不到 = _结构差(self.table_name, self._original_columns, 新列们)
        if 做不到 is not None:
            QMessageBox.warning(self, "做不到", 做不到)
            return
        if not 语句们:
            # 什么都没改：直接关掉，不用去打搅引擎。
            self.accept()
            return

        # 批里没有回滚（前面成了的就是成了），所以这一串是**一条语句一个
        # 落盘**，按 删列 -> 改名 -> 加列 的次序发。
        self.engine.发多(语句们, self._收保存)

    # ---- 回话（在主线程里被叫） --------------------------------------

    def _收保存(self, 结果们, 错) -> None:
        if 错 is not None:
            QMessageBox.critical(self, "错误", f"保存表结构失败: {错}")
            return
        坏 = [r.error for r in (结果们 or []) if not r.ok]
        if 坏:
            # **批里没有回滚**：前面几条成了的就是成了。先把目录刷新一遍，
            # 让界面拿到盘上真实的样子，再报错——不能让人对着一个假的
            # 结构接着改第二遍。
            self.engine.catalog.刷新()
            QMessageBox.critical(self, "错误", f"保存表结构失败: {坏[0]}")
            return
        self.engine.catalog.刷新(self._收刷新)

    def _收刷新(self, 错) -> None:
        if 错 is not None:
            QMessageBox.critical(self, "错误", f"保存表结构失败: {错}")
            return
        self.accept()


# ══ 四、表设计器 ═══════════════════════════════════════════════════════


class TableDesigner(QDialog):
    """完整表设计器 -- 列 + 索引 + 外键 + 选项。"""

    def __init__(self, engine, database_name: str, table_name: str = "", parent=None):
        super().__init__(parent)
        self.engine = engine
        self.database_name = database_name
        self.table_name = table_name
        self._is_new = not table_name
        # 原来那张表的列定义（保存时跟网格里的比）。新建的表用不上。
        self._original_columns = []
        self.setWindowTitle(
            f"设计表 -- {database_name}.{table_name}" if table_name else f"新建表 -- {database_name}"
        )
        self.setMinimumSize(950, 600)
        self.setModal(True)
        self._setup_ui()
        if not self._is_new:
            self._load_table()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 表名 + 注释
        top = QHBoxLayout()
        top.addWidget(QLabel("表名:"))
        self._name_edit = QLineEdit(self.table_name)
        self._name_edit.setPlaceholderText("输入表名...")
        top.addWidget(self._name_edit, 1)
        top.addWidget(QLabel("注释:"))
        self._comment_edit = QLineEdit()
        top.addWidget(self._comment_edit, 2)
        layout.addLayout(top)

        # 标签页
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_columns_tab(), "列")
        self._tabs.addTab(self._build_indexes_tab(), "索引")
        self._tabs.addTab(self._build_options_tab(), "选项")
        layout.addWidget(self._tabs, 1)

        # 按钮
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        btns.button(QDialogButtonBox.Ok).setText("保存" if not self._is_new else "创建")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        layout.addWidget(btns)

    def _build_columns_tab(self) -> QWidget:
        w = QWidget()
        lo = QVBoxLayout(w)

        self._col_table = QTableWidget(0, 8)
        self._col_table.setHorizontalHeaderLabels(
            ["列名", "类型", "长度", "主键", "非空", "唯一", "自增", "注释"]
        )
        hdr = self._col_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(7, QHeaderView.Stretch)
        self._col_table.setMinimumHeight(200)
        lo.addWidget(self._col_table)

        bl = QHBoxLayout()
        for label, slot in [
            ("+ 添加列", self._add_col_row),
            ("- 删除", self._del_col_row),
            ("上移", self._col_up),
            ("下移", self._col_down),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            bl.addWidget(btn)
        bl.addStretch()
        lo.addLayout(bl)
        if self._is_new:
            self._add_col_row()
        return w

    def _build_indexes_tab(self) -> QWidget:
        w = QWidget()
        lo = QVBoxLayout(w)

        self._idx_table = QTableWidget(0, 4)
        self._idx_table.setHorizontalHeaderLabels(["索引名", "列", "类型", "方法"])
        hdr = self._idx_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        lo.addWidget(self._idx_table)

        bl = QHBoxLayout()
        self._idx_add_btn = QPushButton("+ 添加索引")
        self._idx_add_btn.clicked.connect(self._add_idx_row)
        bl.addWidget(self._idx_add_btn)
        self._idx_del_btn = QPushButton("- 删除")
        self._idx_del_btn.clicked.connect(self._del_idx_row)
        bl.addWidget(self._idx_del_btn)
        bl.addStretch()

        info = QLabel(
            "提示: PRIMARY KEY 在 '列' 标签页中设置。\n"
            "唯一索引请在类型中选择 UNIQUE。"
        )
        info.setStyleSheet("color: #64748B; font-size: 11px;")
        lo.addWidget(info)
        lo.addLayout(bl)
        return w

    def _build_options_tab(self) -> QWidget:
        w = QWidget()
        lo = QFormLayout(w)

        # 下面这几格霸下一样都没有（存储引擎只有一种、没有字符集/排序规则/
        # 行格式/自增）。控件和文案全留着，只是**保存时一个字都不进语句**。
        self._engine_combo = QComboBox()
        self._engine_combo.addItems(["baxia", "InnoDB"])
        lo.addRow("存储引擎:", self._engine_combo)

        self._charset_combo = QComboBox()
        self._charset_combo.addItems(["utf8mb4", "utf8mb3", "latin1", "gbk", "gb2312"])
        lo.addRow("字符集:", self._charset_combo)

        self._collation_combo = QComboBox()
        self._collation_combo.addItems([
            "utf8mb4_general_ci", "utf8mb4_unicode_ci", "utf8mb4_bin",
            "utf8mb3_general_ci", "latin1_swedish_ci",
        ])
        lo.addRow("排序规则:", self._collation_combo)

        self._row_format_combo = QComboBox()
        self._row_format_combo.addItems(["Dynamic", "Compact", "Redundant"])
        lo.addRow("行格式:", self._row_format_combo)

        # 注意：这一格用的是 QSpinBox，参照项目那份文件**没有把 QSpinBox
        # 引进 import 里**，所以那张"选项"页一构造就 NameError。这里补上了
        # ——不然这个对话框根本打不开。
        #
        # ⛔ **上限 9999999999 装不进 `QSpinBox`**（它里面是 C 的 int，
        # 4 字节，顶到 2147483647）。原来那一句 `setRange(1, 9999999999)`
        # 会当场抛 `OverflowError`，**这张"选项"页一构造就崩**，报错指的
        # 还是 `setRange` 那一行。`自增起始值` 是 BIGINT 的起点，天生就
        # 该比 int32 大，所以不能夹小 —— 走 `界面零件.整数框`，它自己
        # 挑控件（装得下 `QSpinBox`，装不下文本框）。
        self._auto_inc_spin, 读自增, 写自增 = _零件.整数框(1, 9999999999)
        self._自增起始 = 读自增
        写自增(1)
        lo.addRow("自增起始值:", self._auto_inc_spin)

        lo.addRow(QLabel(""))  # spacer
        return w

    def _load_table(self) -> None:
        try:
            db = self.engine.catalog.get_database(self.database_name)
            tbl = db.table(self.table_name)
            self._comment_edit.setText(tbl.comment)

            # 留一份"原来的样子"，保存时拿它跟网格里的比（霸下的 `改表`
            # 只能加列 / 删列 / 改名，比出来是把已有列改掉了就得如实拒绝）。
            self._original_columns = [
                {'name': col.name, 'type_code': col.type_code,
                 'nn': not col.nullable, 'pk': col.primary_key,
                 'default': col.default_value()}
                for col in tbl.columns
            ]

            for col in tbl.columns:
                self._add_col_row()
                r = self._col_table.rowCount() - 1
                self._col_table.setItem(r, 0, QTableWidgetItem(col.name))
                combo = QComboBox()
                combo.addItems([t[0] for t in SQL_TYPES])
                tn = _类型字(col.type_code)
                if combo.findText(tn) >= 0:
                    combo.setCurrentText(tn)
                self._col_table.setCellWidget(r, 1, combo)

                # 霸下没有长度：这一格一直空着（占位符是它本来的样子）。
                le = QLineEdit()
                le.setPlaceholderText("默认")
                le.setMinimumWidth(60)
                self._col_table.setCellWidget(r, 2, le)

                for c, v in [(3, col.primary_key), (4, not col.nullable),
                              (5, col.unique), (6, col.auto_increment)]:
                    self._col_table.setCellWidget(r, c, self._make_check(v))
                self._col_table.setItem(r, 7, QTableWidgetItem(col.comment or ""))
        except Exception as e:
            QMessageBox.critical(self, "错误", f"加载表失败: {e}")

    def _add_col_row(self) -> None:
        r = self._col_table.rowCount()
        self._col_table.insertRow(r)
        self._col_table.setItem(r, 0, QTableWidgetItem(""))
        combo = QComboBox()
        combo.addItems([t[0] for t in SQL_TYPES])
        combo.setCurrentText("整数")
        self._col_table.setCellWidget(r, 1, combo)
        le = QLineEdit(); le.setPlaceholderText("默认"); le.setMinimumWidth(60)
        self._col_table.setCellWidget(r, 2, le)
        for c in range(3, 7):
            self._col_table.setCellWidget(r, c, self._make_check(False))
        self._col_table.setItem(r, 7, QTableWidgetItem(""))

    def _del_col_row(self) -> None:
        r = self._col_table.currentRow()
        if r >= 0 and self._col_table.rowCount() > 0:
            self._col_table.removeRow(r)

    def _col_up(self) -> None:
        r = self._col_table.currentRow()
        if r > 0:
            self._swap_rows(r, r - 1)
            self._col_table.selectRow(r - 1)

    def _col_down(self) -> None:
        r = self._col_table.currentRow()
        if r >= 0 and r < self._col_table.rowCount() - 1:
            self._swap_rows(r, r + 1)
            self._col_table.selectRow(r + 1)

    def _make_check(self, checked: bool) -> QWidget:
        w = QWidget()
        lo = QHBoxLayout(w); lo.setContentsMargins(0,0,0,0)
        lo.setAlignment(Qt.AlignCenter)
        cb = QCheckBox(); cb.setChecked(checked)
        lo.addWidget(cb)
        return w

    def _get_check(self, row: int, col: int) -> QCheckBox:
        w = self._col_table.cellWidget(row, col)
        return w.findChild(QCheckBox) if w else None

    def _swap_rows(self, r1, r2):
        cols = self._col_table.columnCount()
        d1, d2, wrefs = {}, {}, []
        for c in range(cols):
            d1[c] = (self._col_table.cellWidget(r1, c), self._col_table.item(r1, c))
            d2[c] = (self._col_table.cellWidget(r2, c), self._col_table.item(r2, c))
            if d1[c][0]: wrefs.append(d1[c][0])
            if d2[c][0]: wrefs.append(d2[c][0])
        try:
            for c in range(cols):
                self._col_table.removeCellWidget(r1, c)
                self._col_table.removeCellWidget(r2, c)
            for c in range(cols):
                w2, i2 = d2[c]; w1, i1 = d1[c]
                if w2: self._col_table.setCellWidget(r1, c, w2)
                if i2: self._col_table.setItem(r1, c, QTableWidgetItem(i2))
                if w1: self._col_table.setCellWidget(r2, c, w1)
                if i1: self._col_table.setItem(r2, c, QTableWidgetItem(i1))
        finally:
            wrefs.clear()

    def _add_idx_row(self) -> None:
        r = self._idx_table.rowCount()
        self._idx_table.insertRow(r)
        self._idx_table.setItem(r, 0, QTableWidgetItem(""))
        self._idx_table.setItem(r, 1, QTableWidgetItem(""))
        type_combo = QComboBox()
        type_combo.addItems(["INDEX", "UNIQUE", "FULLTEXT"])
        self._idx_table.setCellWidget(r, 2, type_combo)
        method_combo = QComboBox()
        method_combo.addItems(["BTREE", "HASH"])
        self._idx_table.setCellWidget(r, 3, method_combo)

    def _del_idx_row(self) -> None:
        r = self._idx_table.currentRow()
        if r >= 0:
            self._idx_table.removeRow(r)

    def _on_accept(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            return QMessageBox.warning(self, "错误", "请输入表名")
        import re
        if not re.match(r'^(?:[_]|[^\W\d_])\w*$', name):
            return QMessageBox.warning(self, "错误", "表名必须以字母(含中文)或下划线开头")

        # Collect columns
        cols_data = []
        for r in range(self._col_table.rowCount()):
            cn = self._col_table.item(r, 0)
            if not cn or not cn.text().strip():
                continue
            combo = self._col_table.cellWidget(r, 1)
            tn = combo.currentText() if combo else "整数"
            # Strip "(中文)" label suffix for type resolution
            tn_clean = tn.split(" (")[0] if " (" in tn else tn
            tc = _类型号(tn_clean)
            le = self._col_table.cellWidget(r, 2)
            length = 0
            if le and isinstance(le, QLineEdit):
                text = le.text().strip()
                if text:
                    try:
                        length = int(text)
                    except ValueError:
                        pass
            pk_cb = self._get_check(r, 3)
            nn_cb = self._get_check(r, 4)
            uq_cb = self._get_check(r, 5)
            ai_cb = self._get_check(r, 6)
            comment_item = self._col_table.item(r, 7)

            # Validate
            # （霸下没有长度，`TYPES_NEEDING_LENGTH` 是空集，这一段走不到；
            #   文案照旧。）
            if tc in TYPES_NEEDING_LENGTH and length <= 0:
                return QMessageBox.warning(self, "错误", f"列 '{cn.text()}' 需要指定长度")
            if (ai_cb and ai_cb.isChecked()) and tc not in TYPES_ALLOWING_AUTO_INCREMENT:
                return QMessageBox.warning(self, "错误", f"列 '{cn.text()}' 类型不支持自增")

            cols_data.append({
                "name": cn.text().strip(),
                "type_code": tc, "length": length,
                "pk": pk_cb.isChecked() if pk_cb else False,
                "nn": nn_cb.isChecked() if nn_cb else False,
                "uq": uq_cb.isChecked() if uq_cb else False,
                "ai": ai_cb.isChecked() if ai_cb else False,
                "comment": comment_item.text().strip() if comment_item else "",
            })

        if not cols_data:
            return QMessageBox.warning(self, "错误", "请至少定义一个列")

        # Check unique col names
        col_names = [c["name"].lower() for c in cols_data]
        if len(col_names) != len(set(col_names)):
            return QMessageBox.warning(self, "错误", "列名重复")

        # ── 落地：拼语句发出去（霸下没有"改内存模型再 save"）────────────
        if self._is_new:
            if self.engine.catalog.has_table(self.database_name, name):
                return QMessageBox.warning(self, "错误", f"表 '{name}' 已存在")
            try:
                语句 = _建表语句(name, cols_data)
            except ValueError as e:
                return QMessageBox.critical(self, "错误", f"保存失败: {e}")
            self.engine.发(语句, self._收保存)
            return

        if name != self.table_name:
            return QMessageBox.warning(
                self, "不支持",
                "霸下没有「改表名」这条语句（表名就是它那条目录项）。"
                "要换名字请新建一张表，再把数据搬过去。")

        # 这张网格里没有"默认值"这一列——没地方改，所以原来是什么就是什么。
        旧默认 = {c['name']: c.get('default') for c in self._original_columns}
        新列们 = [{'name': c['name'], 'type_code': c['type_code'],
                   'nn': c['nn'], 'pk': c['pk'],
                   'default': 旧默认.get(c['name'])} for c in cols_data]
        语句们, 做不到 = _结构差(self.table_name, self._original_columns, 新列们)
        if 做不到 is not None:
            return QMessageBox.warning(self, "做不到", 做不到)
        if not 语句们:
            self.accept()
            return
        self.engine.发多(语句们, self._收保存)

    # ---- 回话（在主线程里被叫） --------------------------------------

    def _收保存(self, 结果或们, 错) -> None:
        if 错 is not None:
            QMessageBox.critical(self, "错误", f"保存失败: {错}")
            return
        if isinstance(结果或们, list):
            坏 = [r.error for r in 结果或们 if not r.ok]
            if 坏:
                self.engine.catalog.刷新()
                QMessageBox.critical(self, "错误", f"保存失败: {坏[0]}")
                return
        elif 结果或们 is None or not 结果或们.ok:
            QMessageBox.critical(
                self, "错误",
                f"保存失败: {结果或们.error if 结果或们 is not None else '没有回结果'}")
            return
        self.engine.catalog.刷新(self._收刷新)

    def _收刷新(self, 错) -> None:
        if 错 is not None:
            QMessageBox.critical(self, "错误", f"保存失败: {错}")
            return
        self.accept()
