#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面_查询.py — 「SQL 查询」面板，搬自 `baxiadb-v1.6/config_gui/ui/query_editor.py`

**界面那一层一个字没动。** 布局、控件、文案、emoji、全角符号、颜色、
tooltip、快捷键、按钮顺序、样式表字符串、语法高亮的配色，全部逐字照抄。
改的只有**底下那条数据通路**，外加**语言的词表**（下面第一节）：

    参照项目                                霸下
    ────────────────────────────────────    ──────────────────────────────
    engine.catalog.database_names           → 同名（`目录` 上的缓存，同步读不卡）
    db.table_names / db.table(名)           → 同名（`库` 上都有）
    tbl.column_names / .primary_key         → 同名（`表` 上都有）
    tbl.columns[i].sql_type.to_sql()        → `列.sql_type` **本身就是类型名串**
    engine.current_database 先存后改再改回   → 接驳层的 setter 收得下，当摆设
    engine.save_all() / catalog.save()      → 空壳，霸下每条语句写完就落盘了
    engine.direct_query(...)                → 删掉，换成 `引擎.取` / `引擎.发`
    Planner / Optimizer / Executor          → 删掉
    parse_sql(...) / SQLError               → 删掉，霸下自己解，错在 `结果.error` 里
    Engine SQL 管道（同步、返回 result）     → `引擎.取/发(语句, 回话)`（异步、非阻塞）
    `--direct-query:` 那条私路               → 删掉，索引面板改发真语句

取数一律是**异步**的：`回话(结果, 错)` 在**主线程**里被叫。语句写错不算
`错`——那时候 `结果.ok` 是 False、`结果.error` 里是中文错误文本，面板照原样
红字显示，**一个字不加工**。


一、语言：英文 SQL -> 中文 SQL
=============================

霸下说的是中文 SQL，所以**凡是会进到语句里去的东西**都换成了霸下那套词：

    参照（英文）                            霸下
    SELECT / FROM / WHERE / LIMIT …        查询 / 从 / 其中 / 限制 …
    `库`.`表`（两截名）                      `表`（**一个内核就是一个库**，没有库名这一截）
    LIKE / NOT LIKE                        像 / 不像
    IS NULL / IS NOT NULL                  为空 / 不为空
    IN (…)                                 属于 (…)
    AND / OR                               与 / 或
    ASC / DESC                             升序 / 降序
    ORDER BY / GROUP BY / HAVING           排序 按 / 分组 按 /（`其中`，霸下没有独立的 HAVING 词）
    VALUES / SET / INTO                    值 / 设 /（`插入` 不收 INTO）
    INSERT / UPDATE / DELETE               插入 / 更新 / 删除
    CREATE TABLE / DROP TABLE / TRUNCATE   建表 / 删表 / 删除 从（霸下没有截断表）
    INNER JOIN / LEFT JOIN / RIGHT JOIN    内连接 / 左连接 / 右连接
    INT / TEXT / FLOAT / …                 整数 / 文本 / 实数 / 二进制 / 时间（**只有这五个**）
    NOT NULL / PRIMARY KEY / AUTO_INCREMENT 非空 / 主键 /（自增，霸下没有）

**只是标签、不进语句的那些英文子句名**（`WHERE` / `GROUP BY` / `HAVING` /
`SET` / `VALUES` / `FROM` / `JOIN` / `ON` / `SELECT` 这些框标题）按铁律原样
留着——它们是界面上的字，不是语言里的词。反过来，**下拉框里那些会被拼进
语句的值**（操作符、与/或、类型、约束、DDL 操作）换成了霸下的词：那些值
本来就是"语言的词表"，跟语法高亮的关键字表是一回事。

高亮器的关键字表**不抄一份，直接引 `sql.KEYWORDS`**（只挑带中文的那些）：
词表只有一处权威，引擎加了词这儿自动跟上，不会出现"看着认识、其实不认"。


二、这里没有执行计划
====================

参照项目这个文件里**没有** EXPLAIN / 执行计划的按钮和标签页，所以没有
可保留的控件，也没有编一份假的。霸下没有执行计划器——语句怎么走由引擎
内部定，界面上给不出来。


三、摆着但其实是空的地方（如实记在这儿，别当它是真的）
======================================================

* **多库下拉**：霸下只有一个库，下拉里就一项（`-- 选择数据库 --` 加上那
  唯一的名字）。控件保留、可选——**禁掉它这张面板就没法用了**（各面板都
  要先选出库才往下走）。**不暗示能切**。
* **远程那条路**（`_execute_remote` / `_display_remote_result`）：`client`
  形参按参照项目的签名保留，但霸下**永远传 None**，这条路走不到。留着是
  为了主窗那边 `panel_cls(client=backend)` 那个位置参数不用改。
* **索引面板那几句假话已经清了**。原先照搬参照项目的 `B+树二分 O(log n)` /
  `不经过 SQL 解析`，在这个库里**一个字都不成立**：霸下不用 B+ 树（自研的
  区+榫卯），而且**闸口只有 `取` / `发` 两个口，根本没有"绕过 SQL 解析"的
  第三条路**——那个面板拼的既然是正常的 `查询` 语句，走的就是手写框那条路。
  改成实话的几处见 `IndexPanel` 的头注释与 `_on_field_changed`。
* **自增**：霸下不做自增。`AUTO_INCREMENT` 那个勾保留但**禁用**（它是
  `自增`），永远不会被勾上，也永远不进语句。
* **查询历史**：参照项目这个文件里没有历史，也没有配置文件。霸下没有
  配置层——真要加历史，**只能放内存里**（关窗口就没了），别写盘。
  现在有了：`RawSqlPanel` 自己留一份**内存里**的列表（上限 50 条），
  手写框旁边一个"历史"按钮弹菜单回填。**不落盘**，关了窗口就没了。
* **导出**：从 `结果` 对象导，不从表格控件里抄（控件里二进制已经显示成
  `<12 字节>` 了）。二进制写 `0x` 加十六进制，空值写 `NULL`。

界面上**没有**事务按钮（BEGIN/COMMIT/ROLLBACK）、没有存储过程、没有变量、
没有多结果集、没有"统计行数"按钮——参照项目这个文件里都没有，所以没有
可保留的控件，也没有禁用谁。


四、引擎上限（面板上该让人知道）
================================

    一次结果最多 20 万行（`syntax` 的 `max_result_rows`）
    一趟扫描最多 200 万行（`max_scan_rows`）
    一批最多 4096 条（`io_layer.MAX_BATCH_STATEMENTS`）

超了引擎会拒，错误文本回在 `结果.error` 里。这儿的限值下拉框管的是语句
里的 `限制`，**是另一回事**。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QLabel, QSplitter, QMessageBox,
    QHeaderView, QComboBox, QCheckBox, QLineEdit, QSpinBox,
    QScrollArea, QFrame, QGroupBox, QGridLayout, QToolButton,
    QStackedWidget, QButtonGroup,
    QPlainTextEdit, QMenu,          # 手写语句框，以及它的历史下拉
)
from PySide6.QtGui import (QFont, QSyntaxHighlighter, QTextCharFormat, QColor,
                           QKeySequence, QShortcut)
from PySide6.QtCore import Qt, Signal
import re

import sql as _sql

from 界面零件 import ResultTable
# 「改表」那一页整个把活交给表结构编辑器：它已经把"旧结构 -> 新结构"翻
# 成 `改表 …` 那一套写完了（`_结构差`），能说清楚的正好是引擎支持的三样。
# 那边只 import PySide6 + `schema`，**不会循环**。
from 界面_设计表 import EditTableDialog


# ══ 语句面：谁走"取"、谁走"发" ═══════════════════════════════════════
#
# 闸口那两条（`fetch` / `send`）底下是同一次调用，分的是**记账**：带结果集的
# 算"出"。霸下按空格断词，第一个词就是语句头，照它分就够了。

_取数开头 = frozenset([
    '查询', '选择', '查', 'SELECT',
    '显示', '列出', 'SHOW',
    '检查索引', 'CHECKINDEX',
])


def _是取数语句(语句: str) -> bool:
    """这条语句是取数的，还是改动的。

    **认不出来的一律按改动走**：改动那条路真去写盘，写错了会报错；把一条
    写语句按取数发出去，最坏是账记错了边，谁也看不出来。
    """
    头 = 语句.strip().split(None, 1)
    return bool(头) and 头[0].upper() in _取数开头


def _批动向(语句: str):
    """这句是"进批"还是"出批"？不是批语句就返回 None。

    只认**头一个词**——`批量 开始` 的头一个词是 `批量`，`开始` 是简写。
    这两个写法在引擎里**是同一条语句**（`sql.py` 里都返回 `BatchBegin`，
    字段也一样），所以这儿也不用分。

    **只认头一个词是这条路的局限**：一整段文本写成
    `插入 …; 批量 提交`（批语句不在最前）时认不出来，横幅会停在旧状态。
    要认得准就得在界面按分号重切语句，而那是静默出错的源头——宁可漏报，
    不可切歪。按钮点出来的和手写框里单敲的那一条都认得到，够用。
    """
    头 = (语句 or "").strip().split(None, 1)
    if not 头:
        return None
    词 = 头[0].strip(';,（(【[').upper()
    if 词 == '批量' and len(头) > 1:
        词 = 头[1].split(None, 1)[0].strip(';,）)】]').upper()
    if 词 in ('开始', 'BEGIN', 'BEGINWORK'):
        return True
    if 词 in ('提交', 'COMMIT'):
        return False
    return None


def _有中文(s: str) -> bool:
    return any('一' <= c <= '鿿' for c in s)


# ===================================================================
# SQL 语法高亮器
# ===================================================================

class SQLHighlighter(QSyntaxHighlighter):
    """SQL 语法高亮器

    **词表不抄一份，直接引 `sql.KEYWORDS`**：那是引擎认的词，高亮跟着它走
    才不会出现"看着认识、其实不认"。只挑带中文的那些——英文规范名是引擎
    内部用的，界面上照参照项目那样不高亮。

    颜色、字体、规则（`_build_rules`）**一个字没动**。
    """

    KEYWORDS = [w for w in _sql.KEYWORDS if _有中文(w)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keyword_fmt = QTextCharFormat()
        self._keyword_fmt.setForeground(QColor("#2563EB"))
        self._keyword_fmt.setFontWeight(700)
        self._string_fmt = QTextCharFormat()
        self._string_fmt.setForeground(QColor("#16A34A"))
        self._number_fmt = QTextCharFormat()
        self._number_fmt.setForeground(QColor("#F59E0B"))
        self._comment_fmt = QTextCharFormat()
        self._comment_fmt.setForeground(QColor("#94A3B8"))
        self._comment_fmt.setFontItalic(True)
        self._rules = self._build_rules()

    def _build_rules(self):
        rules = []
        for kw in self.KEYWORDS:
            rules.append((re.compile(rf"\b{kw}\b", re.IGNORECASE), self._keyword_fmt))
        rules.append((re.compile(r"'[^']*'"), self._string_fmt))
        rules.append((re.compile(r'"[^"]*"'), self._string_fmt))
        rules.append((re.compile(r"\b\d+\.?\d*\b"), self._number_fmt))
        rules.append((re.compile(r"--[^\n]*"), self._comment_fmt))
        return rules

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self._rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)


# ===================================================================
# 公共组件
# ===================================================================

# 操作符。**显示的就是拼进语句的**——霸下这边操作符本来就是中文词，
# 所以不像参照项目那样要再翻一道（那边 `IN (...)` 是个例外，这儿
# `属于 (...)` 一样是个例外）。
_OPERATORS = [
    ("=", "="), ("!=", "!="), (">", ">"), (">=", ">="),
    ("<", "<"), ("<=", "<="), ("像", "像"), ("不像", "不像"),
    ("为空", "为空"), ("不为空", "不为空"),
    ("属于 (...)", "属于"),
]

_AGG_FUNCS = ["(无)", "计数", "求和", "平均", "最大", "最小"]


def _quote_val(val: str) -> str:
    """智能加引号：数字不加，其他加单引号

    （霸下的字面量规矩就是这样：整数实数裸写，文本和时间加单引号。
    **空值不走这儿**——判空用 `为空`，赋值用 `空`，都不能拿 `=` 跟它比。）
    """
    v = val.strip()
    if not v:
        return "''"
    try:
        float(v)
        return v
    except ValueError:
        return f"'{v}'"


def _escape_sql(s: str) -> str:
    """霸下的字面量转义：**单引号写成两个**。

    参照项目那边是 MySQL 那套（反斜杠 + `\\'`），霸下内部不认反斜杠，
    照它那样转出来的串会多出两个反斜杠。
    """
    return s.replace("'", "''")


class WhereConditionRow(QFrame):
    """单条 WHERE 条件行"""

    removed = Signal(object)

    def __init__(self, columns: list[str], index: int, parent=None):
        super().__init__(parent)
        self._index = index
        self._columns = columns
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)

        if self._index > 0:
            self._and_or_combo = QComboBox()
            # **与 / 或**：这个框里的字是要拼进 `其中` 的，不是标签。
            self._and_or_combo.addItems(["与", "或"])
            self._and_or_combo.setFixedWidth(55)
            layout.addWidget(self._and_or_combo)
        else:
            lbl = QLabel("WHERE")
            lbl.setStyleSheet("font-weight: bold; color: #2563EB;")
            layout.addWidget(lbl)

        self._col_combo = QComboBox()
        self._col_combo.addItems(self._columns)
        self._col_combo.setMinimumWidth(90)
        self._col_combo.currentTextChanged.connect(self._on_op_changed)
        layout.addWidget(self._col_combo)

        self._op_combo = QComboBox()
        for label, _ in _OPERATORS:
            self._op_combo.addItem(label)
        self._op_combo.setFixedWidth(110)
        self._op_combo.currentTextChanged.connect(self._on_op_changed)
        layout.addWidget(self._op_combo)

        self._value_edit = QLineEdit()
        self._value_edit.setPlaceholderText("值...")
        self._value_edit.setMinimumWidth(100)
        layout.addWidget(self._value_edit, 1)

        del_btn = QToolButton()
        del_btn.setText("✕")
        del_btn.setToolTip("删除此条件")
        del_btn.setFixedSize(22, 22)
        del_btn.clicked.connect(lambda: self.removed.emit(self))
        layout.addWidget(del_btn)

    def _on_op_changed(self):
        op = self._op_combo.currentText()
        no_val = op in ("为空", "不为空")
        self._value_edit.setEnabled(not no_val)
        self._value_edit.setPlaceholderText("(无需输入)" if no_val else "值...")

    def get_sql(self) -> str:
        col = self._col_combo.currentText()
        op_label = self._op_combo.currentText()
        # **判空不能用 `=` 比**：`为空` / `不为空` 是谓词，后面不带值。
        if op_label in ("为空", "不为空"):
            return f"`{col}` {op_label}"
        val = self._value_edit.text().strip()
        if not val:
            return ""
        if op_label == "属于 (...)":
            parts = [_quote_val(v.strip()) for v in val.split(",")]
            return f"`{col}` 属于 ({', '.join(parts)})" if parts else ""
        return f"`{col}` {op_label} {_quote_val(val)}"

    def get_connector(self) -> str:
        return "" if self._index == 0 else self._and_or_combo.currentText()


# ===================================================================
# 查询模式面板基类
# ===================================================================

class BaseQueryPanel(QWidget):
    """所有查询模式面板的基类"""

    sql_generated = Signal(str)
    #: 面板**自己**动了库（不是发语句，是弹了个对话框、由对话框自己去发）
    #: 时喊一声。现在只有 `AlterPanel` 用：它把活交给 `EditTableDialog`，
    #: 那条路不经过 `sql_generated`，所以得有一条自己的回话线，好让编辑器
    #: 重拉一遍目录（不然新列不会进别的页的下拉框）。
    数据变了 = Signal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._columns: list[str] = []
        self._where_rows: list[WhereConditionRow] = []
        self._built = False

    def _ensure_built(self):
        """确保 UI 已构建（延迟初始化）"""
        if not self._built:
            self._build_ui()
            self._built = True

    def _build_ui(self):
        """子类实现：构建 UI"""
        raise NotImplementedError

    def build_sql(self) -> str:
        """子类实现：生成 SQL"""
        raise NotImplementedError

    def _emit_sql(self):
        """拼出来就**发出去跑**。

        **原来这方法在五个子类里各抄了一份，五份一字不差**，现在收到基类
        一份。差别只有一个：原先签名是 `_emit_sql(self, execute=False)`，
        而那个参数**从来没起过作用**——函数体一眼看到底，`execute` 既没拿
        来做判断也没往下传；`sql_generated` 接的那头是
        `QueryEditor._on_execute_request`，**就是执行**。所以面板上那两颗
        按钮「生成 SQL」和「生成并执行」按下去发出的东西分毫不差。
        我实测过一次：在「表结构」页选 `删表`、点「生成 SQL」，表真的没了。

        两颗并成一颗（只留「生成并执行」，名字说得准的那颗）之后，这个
        参数连存在的理由都没了，一并去掉。
        """
        sql = self.build_sql()
        if sql: self.sql_generated.emit(sql)

    def _get_db_name(self) -> str:
        return ""

    def _get_table_name(self) -> str:
        return ""

    def refresh_columns(self):
        """子类可重写"""
        pass

    # ---- 公共 WHERE 条件构建器 ----

    def _setup_where_section(self, parent_layout, columns_getter):
        """添加 WHERE 条件区域"""
        where_group = QGroupBox("筛选条件 (WHERE)")
        wl = QVBoxLayout(where_group)
        wl.setSpacing(4)

        self._where_container = QVBoxLayout()
        self._where_container.setSpacing(2)
        wl.addLayout(self._where_container)

        add_btn = QPushButton("＋ 添加条件")
        add_btn.setFixedWidth(90)
        add_btn.clicked.connect(lambda: self._add_where_row(columns_getter()))
        wl.addWidget(add_btn)

        parent_layout.addWidget(where_group)

    def _add_where_row(self, columns):
        if not columns:
            return
        idx = len(self._where_rows)
        w = WhereConditionRow(columns, idx)
        w.removed.connect(self._remove_where_row)
        self._where_rows.append(w)
        self._where_container.addWidget(w)

    def _remove_where_row(self, widget: WhereConditionRow):
        if widget not in self._where_rows:
            return
        self._where_rows.remove(widget)
        # 保存数据后重建
        saved = []
        for w in self._where_rows:
            saved.append({
                "col": w._col_combo.currentText(),
                "op": w._op_combo.currentText(),
                "val": w._value_edit.text(),
            })
        while self._where_container.count():
            item = self._where_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._where_rows.clear()
        cols = self._columns
        for i, d in enumerate(saved):
            w = WhereConditionRow(cols, i)
            w.removed.connect(self._remove_where_row)
            for cb_name, key in [(w._col_combo, "col"), (w._op_combo, "op")]:
                idx = cb_name.findText(d[key])
                if idx >= 0:
                    cb_name.setCurrentIndex(idx)
            w._value_edit.setText(d["val"])
            self._where_rows.append(w)
            self._where_container.addWidget(w)

    def _clear_where_rows(self):
        while self._where_container.count():
            item = self._where_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._where_rows.clear()

    def _build_where_clause(self) -> str:
        parts = []
        for w in self._where_rows:
            cond = w.get_sql()
            if not cond:
                continue
            conn = w.get_connector()
            parts.append(f"  {conn} {cond}" if conn else f"  {cond}")
        if not parts:
            return ""
        first = parts[0]
        # **`其中`，不是 `WHERE`。** 这一行是要拼进语句里去的。
        parts[0] = "其中 " + first.strip()
        return "\n" + "\n".join(parts)


# ===================================================================
# 1. SELECT 查询面板
# ===================================================================
#
# 注：参照项目里这个面板**没有挂到任何标签页上**（`_QUERY_MODES` 里没有
# 它），是这个文件里的一件存货。按铁律原样搬过来，`build_sql` 照霸下的
# 语句面翻。它调用的 `_ensure_built` / `_refresh_db_list` 都还在。

class SelectPanel(BaseQueryPanel):
    """SELECT 查询 — 默认极简模式：下拉选库→选表→勾字段→一键查询。

    高级模式折叠在下方，点击展开：WHERE 构建器、GROUP BY、HAVING。
    """

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        # ═══════════════════════════════════════════
        # 第1行：数据库 + 表（大号下拉框）
        # ═══════════════════════════════════════════
        row1 = QHBoxLayout(); row1.setSpacing(10)
        row1.addWidget(QLabel("数据库"))
        self._db_combo = QComboBox(); self._db_combo.setMinimumWidth(140)
        self._db_combo.setStyleSheet("QComboBox { font-size: 13px; padding: 4px; }")
        self._db_combo.currentTextChanged.connect(self._on_db_changed)
        row1.addWidget(self._db_combo)

        row1.addWidget(QLabel("表"))
        self._table_combo = QComboBox(); self._table_combo.setMinimumWidth(160)
        self._table_combo.setStyleSheet("QComboBox { font-size: 13px; padding: 4px; }")
        self._table_combo.currentTextChanged.connect(self._on_table_changed)
        row1.addWidget(self._table_combo)

        row1.addStretch()
        layout.addLayout(row1)

        # ═══════════════════════════════════════════
        # 第2行：字段勾选
        # ═══════════════════════════════════════════
        col_group = QGroupBox("查询字段")
        cgl = QVBoxLayout(col_group); cgl.setSpacing(2)
        btn_row = QHBoxLayout()
        self._select_all_cb = QCheckBox("全选")
        self._select_all_cb.setChecked(True)
        self._select_all_cb.toggled.connect(self._on_select_all)
        btn_row.addWidget(self._select_all_cb)
        inv_btn = QPushButton("反选"); inv_btn.setFixedWidth(40)
        inv_btn.clicked.connect(self._on_invert)
        btn_row.addWidget(inv_btn)
        btn_row.addStretch()
        cgl.addLayout(btn_row)

        self._col_scroll = QScrollArea(); self._col_scroll.setMaximumHeight(120)
        self._col_scroll.setWidgetResizable(True)
        self._col_scroll.setFrameShape(QFrame.NoFrame)
        self._col_container = QWidget()
        self._col_grid = QGridLayout(self._col_container)
        self._col_grid.setContentsMargins(0, 0, 0, 0); self._col_grid.setSpacing(2)
        self._col_scroll.setWidget(self._col_container)
        cgl.addWidget(self._col_scroll)
        layout.addWidget(col_group)

        # ═══════════════════════════════════════════
        # 第3行：快速筛选 + 排序 + 行数 + 查询按钮
        # ═══════════════════════════════════════════
        filter_row = QHBoxLayout(); filter_row.setSpacing(8)

        filter_row.addWidget(QLabel("筛选"))
        self._quick_filter = QLineEdit()
        self._quick_filter.setPlaceholderText("如: age>18  或  name:张三  或  status=active")
        self._quick_filter.setMinimumWidth(220)
        # 右边那半是**拼出来的霸下语句**，所以是 `其中` / `像`。
        self._quick_filter.setToolTip(
            "支持的格式:\n"
            "  age>18        → 其中 age > 18\n"
            "  name:张三     → 其中 name 像 '%张三%'\n"
            "  status=active → 其中 status = 'active'\n"
            "  id:1,2,3      → 其中 id 属于 (1,2,3)\n"
            "多个条件用逗号分隔: age>18, status=active"
        )
        self._quick_filter.returnPressed.connect(self._on_query)
        filter_row.addWidget(self._quick_filter, 1)

        filter_row.addWidget(QLabel("排序"))
        self._order_combo = QComboBox(); self._order_combo.setMinimumWidth(90)
        self._order_combo.addItem("(不排序)")
        filter_row.addWidget(self._order_combo)
        self._order_asc_cb = QCheckBox("↑"); self._order_asc_cb.setChecked(True)
        self._order_asc_cb.setToolTip("升序/降序")
        filter_row.addWidget(self._order_asc_cb)

        filter_row.addWidget(QLabel("行数"))
        self._limit_spin = QSpinBox(); self._limit_spin.setRange(0, 100000)
        self._limit_spin.setValue(200); self._limit_spin.setFixedWidth(65)
        # 引擎的上限，如实告诉人：一次结果最多 20 万行。
        self._limit_spin.setToolTip(
            "语句里的 `限制`。霸下引擎另有硬上限：一次结果最多 20 万行，"
            "一趟扫描最多 200 万行")
        filter_row.addWidget(self._limit_spin)

        # 大大的查询按钮
        query_btn = QPushButton("🔍 查 询")
        query_btn.setFixedSize(100, 36)
        query_btn.setToolTip("生成SQL并立即执行 (Enter)")
        query_btn.clicked.connect(self._on_query)
        query_btn.setStyleSheet(
            "QPushButton { font-size: 14px; font-weight: bold; color: #FFF; "
            "background-color: #2563EB; border-radius: 6px; }"
            "QPushButton:hover { background-color: #1D4ED8; }"
            "QPushButton:pressed { background-color: #1E40AF; }"
        )
        filter_row.addWidget(query_btn)
        layout.addLayout(filter_row)

        # ═══════════════════════════════════════════
        # 高级模式（默认折叠）
        # ═══════════════════════════════════════════
        self._advanced_toggle = QPushButton("▸ 高级条件（点击展开）")
        self._advanced_toggle.setStyleSheet(
            "QPushButton { text-align: left; border: none; color: #64748B; "
            "font-size: 11px; padding: 2px 0; }"
            "QPushButton:hover { color: #2563EB; }")
        self._advanced_toggle.clicked.connect(self._on_toggle_advanced)
        layout.addWidget(self._advanced_toggle)

        # 高级条件容器
        self._advanced_widget = QWidget()
        self._advanced_widget.setVisible(False)
        adv_layout = QVBoxLayout(self._advanced_widget)
        adv_layout.setContentsMargins(0, 0, 0, 0)
        adv_layout.setSpacing(6)

        # WHERE 条件构建器
        self._setup_where_section(adv_layout, lambda: self._columns)

        # GROUP BY + HAVING
        grp_row = QHBoxLayout(); grp_row.setSpacing(8)
        grp_row.addWidget(QLabel("分组(GROUP BY):"))
        self._group_combo = QComboBox(); self._group_combo.setMinimumWidth(100)
        self._group_combo.addItem("(不分组)")
        grp_row.addWidget(self._group_combo)
        grp_row.addWidget(QLabel("HAVING:"))
        self._having_edit = QLineEdit()
        # 霸下没有独立的 HAVING 词：它写成 `分组 按 x 其中 <条件>`，
        # 所以这儿举的例子也得是中文的。
        self._having_edit.setPlaceholderText("如: 计数(*) > 1")
        self._having_edit.setMinimumWidth(140)
        grp_row.addWidget(self._having_edit, 1)
        adv_layout.addLayout(grp_row)

        layout.addWidget(self._advanced_widget)

        self._refresh_db_list()

    # ── 快速查询：生成SQL + 立即执行 ──────────────────────

    def _on_query(self):
        """一键查询：生成SQL并立即执行"""
        sql = self.build_sql()
        if sql:
            self.sql_generated.emit(sql)

    # ── 高级模式切换 ──────────────────────────────────────

    def _on_toggle_advanced(self):
        visible = not self._advanced_widget.isVisible()
        self._advanced_widget.setVisible(visible)
        if visible:
            self._advanced_toggle.setText("▾ 高级条件（点击折叠）")
        else:
            self._advanced_toggle.setText("▸ 高级条件（点击展开）")

    # ── 数据库/表联动 ────────────────────────────────────

    def _refresh_db_list(self):
        c = self._db_combo.currentText()
        self._db_combo.blockSignals(True)
        self._db_combo.clear(); self._db_combo.addItem("-- 选择数据库 --")
        try:
            # 读的是 `目录` 那份内存缓存，**同步读不卡**。
            for n in self.engine.catalog.database_names:
                self._db_combo.addItem(n)
        except Exception: pass
        finally: self._db_combo.blockSignals(False)
        if c and self._db_combo.findText(c) >= 0:
            self._db_combo.setCurrentText(c)

    def _on_db_changed(self, db):
        if not db or db.startswith("--"):
            self._table_combo.blockSignals(True)
            self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
            self._table_combo.blockSignals(False); self._clear_columns(); return
        self._table_combo.blockSignals(True)
        self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
        try:
            for t in self.engine.catalog.get_database(db).table_names:
                self._table_combo.addItem(t)
        except Exception: pass
        finally: self._table_combo.blockSignals(False)

    def _on_table_changed(self, t):
        if not t or t.startswith("--"): self._clear_columns(); return
        self._load_columns()

    def _load_columns(self):
        db = self._db_combo.currentText(); tbl = self._table_combo.currentText()
        if not db or not tbl: return
        try:
            cols = self.engine.catalog.get_database(db).table(tbl).column_names
        except Exception:
            self._clear_columns(); return
        # **先清再赋值。** 这两句的次序反了就没救：`_clear_columns` 会把
        # `self._columns` 也清成空，先赋值的话紧接着就被自己抹掉，下面的
        # 循环拿到的是空列表——列一个都填不上，而 `_get_selected_cols`
        # 返回空、`build_sql` 默默退回 `查询 *`，**不报任何错**。
        self._clear_columns()
        self._columns = cols
        for i, cn in enumerate(cols):
            cb = QCheckBox(cn); cb.setChecked(True)
            self._col_grid.addWidget(cb, i // 4, i % 4)
        self._select_all_cb.setChecked(True)
        self._update_combos()

    def _clear_columns(self):
        """把字段勾选格子清空。

        **必须 `takeAt` 摘掉，不能只 `deleteLater`。** `deleteLater` 只是
        排了个队，控件在这一轮事件循环里还挂在布局上：紧接着 `count()` 和
        `itemAt()` 照样数得到它们，于是重建之后新旧勾选框**叠着**都在，
        `_get_selected_cols` 把同一列报两遍（老面板从没挂到页面上过，
        这条一直没露出来）。
        """
        while self._col_grid.count():
            w = self._col_grid.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._columns = []

    def _get_selected_cols(self):
        return [self._col_grid.itemAt(i).widget().text()
                for i in range(self._col_grid.count())
                if isinstance(self._col_grid.itemAt(i).widget(), QCheckBox)
                and self._col_grid.itemAt(i).widget().isChecked()]

    def _update_combos(self):
        for combo in (self._order_combo, self._group_combo):
            cur = combo.currentText()
            combo.blockSignals(True); combo.clear()
            combo.addItem("(不排序)" if combo is self._order_combo else "(不分组)")
            for c in self._columns: combo.addItem(c)
            if cur and combo.findText(cur) >= 0: combo.setCurrentText(cur)
            combo.blockSignals(False)

    def _on_select_all(self, checked):
        for i in range(self._col_grid.count()):
            w = self._col_grid.itemAt(i).widget()
            if isinstance(w, QCheckBox): w.setChecked(checked)

    def _on_invert(self):
        for i in range(self._col_grid.count()):
            w = self._col_grid.itemAt(i).widget()
            if isinstance(w, QCheckBox): w.setChecked(not w.isChecked())

    def _get_db_name(self): return self._db_combo.currentText()
    def _get_table_name(self): return self._table_combo.currentText()

    # ── SQL 生成 ─────────────────────────────────────────

    def _parse_quick_filter(self) -> str:
        """解析快速筛选输入框，转为 WHERE 子句"""
        text = self._quick_filter.text().strip()
        if not text:
            return ""

        conditions = []
        # 按逗号拆分多个条件
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue

            # 模式1: col:value → 其中 col 像 '%value%'
            if ":" in part and ">" not in part and "<" not in part and "=" not in part.split(":")[0]:
                col, val = part.split(":", 1)
                col = col.strip(); val = val.strip()
                if "," in val:
                    # col:1,2,3 → 其中 col 属于 (1,2,3)
                    # **数字不加引号**，跟语句面那份规矩一条心。
                    items = [_quote_val(v.strip()) for v in val.split(",")]
                    conditions.append(f"`{col}` 属于 ({', '.join(items)})")
                else:
                    conditions.append(f"`{col}` 像 '%{_escape_sql(val)}%'")
                continue

            # 模式2: col>=value, col<=value, col!=value
            for op in [">=", "<=", "!=", ">", "<", "="]:
                if op in part:
                    col, val = part.split(op, 1)
                    col = col.strip(); val = val.strip()
                    # 数字不加引号
                    try:
                        float(val)
                        conditions.append(f"`{col}` {op} {val}")
                    except ValueError:
                        conditions.append(f"`{col}` {op} '{_escape_sql(val)}'")
                    break

        return conditions

    def build_sql(self) -> str:
        db = self._db_combo.currentText(); tbl = self._table_combo.currentText()
        if not db or db.startswith("--") or not tbl or tbl.startswith("--"): return ""

        cols = self._get_selected_cols()
        cols_str = ", ".join(f"`{c}`" for c in cols) if cols else "*"
        # **不写库名**：霸下只有一个库，`库` . `表` 那种两截名它不认。
        sql = f"查询 {cols_str} 从 `{tbl}`"

        # 快速筛选 或 高级 WHERE（快速筛选优先）
        quick_conds = self._parse_quick_filter()
        if quick_conds:
            sql += "\n其中 " + "\n  与 ".join(quick_conds)
        else:
            sql += self._build_where_clause()

        # GROUP BY + HAVING（仅高级模式）
        # 霸下没有独立的 HAVING 词：`分组 按 x` 后面跟 `其中` 就是 having。
        grp = self._group_combo.currentText()
        if grp and grp != "(不分组)":
            sql += f"\n分组 按 `{grp}`"
            hav = self._having_edit.text().strip()
            if hav: sql += f"\n其中 {hav}"

        # ORDER BY
        order = self._order_combo.currentText()
        if order and order != "(不排序)":
            d = "升序" if self._order_asc_cb.isChecked() else "降序"
            sql += f"\n排序 按 `{order}` {d}"

        # LIMIT
        lim = self._limit_spin.value()
        if lim > 0: sql += f"\n限制 {lim}"

        return sql + ";"


# ===================================================================
# 2. INSERT 插入面板
# ===================================================================

class InsertPanel(BaseQueryPanel):
    """INSERT 插入：选表、填值、支持批量"""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4); layout.setSpacing(8)

        row1 = QHBoxLayout(); row1.setSpacing(8)
        row1.addWidget(QLabel("数据库:"))
        self._db_combo = QComboBox(); self._db_combo.setMinimumWidth(120)
        self._db_combo.currentTextChanged.connect(self._on_db_changed)
        row1.addWidget(self._db_combo)
        row1.addWidget(QLabel("表:"))
        self._table_combo = QComboBox(); self._table_combo.setMinimumWidth(140)
        self._table_combo.currentTextChanged.connect(self._on_table_changed)
        row1.addWidget(self._table_combo)
        row1.addStretch()
        layout.addLayout(row1)

        # 批量行数
        row2 = QHBoxLayout(); row2.setSpacing(8)
        row2.addWidget(QLabel("批量插入行数:"))
        self._batch_spin = QSpinBox(); self._batch_spin.setRange(1, 100)
        self._batch_spin.setValue(1); self._batch_spin.setFixedWidth(60)
        # 这些行是拼成**一条** `插入` 的多个值组，不拆成多条语句。
        # 引擎那边还有一道帧的硬上限：一批最多 4096 条。
        self._batch_spin.setToolTip(
            "一次拼成一条 `插入` 的多个值组（不是多条语句）。"
            "霸下引擎一批最多 4096 条")
        self._batch_spin.valueChanged.connect(self._on_batch_changed)
        row2.addWidget(self._batch_spin)

        # 插入 / 替换：**同一个面板加一个开关，不另起一页。** 理由在引擎里
        # ——`sql.py` 里 `插入` 和 `替换` 走的是**同一个解析函数**
        # （`parse_insert(overwrite=True)`），语法完全同形，值网格一模一样；
        # 差别只有"主键撞上时报错"还是"整行覆盖"。为换开头一个词再抄一份
        # 120 行的值网格，以后每改一次网格要改两处。
        self._替换_cb = QCheckBox("替换")
        self._替换_cb.setToolTip(
            "勾上拼 `替换`，不勾拼 `插入`。\n"
            "`插入` 撞上已存在的主键会报错；`替换` 把那一行整行覆盖掉。\n"
            "（引擎的报错原文就是「要覆盖请把 插入 换成 替换」，"
            "这两条是同一个形状。）")
        row2.addWidget(self._替换_cb)
        row2.addStretch()
        layout.addLayout(row2)

        # 值输入区域
        self._values_group = QGroupBox("字段值 (VALUES)")
        self._values_layout = QVBoxLayout(self._values_group)
        self._values_scroll = QScrollArea(); self._values_scroll.setWidgetResizable(True)
        self._values_scroll.setMaximumHeight(300); self._values_scroll.setFrameShape(QFrame.NoFrame)
        self._values_container = QWidget()
        self._values_grid = QGridLayout(self._values_container)
        self._values_grid.setContentsMargins(0, 0, 0, 0); self._values_grid.setSpacing(4)
        self._values_scroll.setWidget(self._values_container)
        self._values_layout.addWidget(self._values_scroll)
        layout.addWidget(self._values_group)

        # 按钮
        row3 = QHBoxLayout(); row3.addStretch()
        # 原先这儿还有一颗「生成 SQL」，跟这颗干的是同一件事，删了。
        # 理由写在 `BaseQueryPanel._emit_sql` 上。
        gen_exec_btn = QPushButton("生成并执行"); gen_exec_btn.setFixedWidth(90)
        gen_exec_btn.clicked.connect(self._emit_sql)
        gen_exec_btn.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #16A34A; border-radius: 4px; padding: 4px 8px; }")
        row3.addWidget(gen_exec_btn)
        layout.addLayout(row3)
        self._refresh_db_list()

    def _refresh_db_list(self):
        c = self._db_combo.currentText()
        self._db_combo.blockSignals(True)
        self._db_combo.clear(); self._db_combo.addItem("-- 选择数据库 --")
        try:
            for n in self.engine.catalog.database_names:
                self._db_combo.addItem(n)
        except Exception: pass
        finally: self._db_combo.blockSignals(False)
        if c and self._db_combo.findText(c) >= 0: self._db_combo.setCurrentText(c)

    def _on_db_changed(self, db):
        if not db or db.startswith("--"):
            self._table_combo.blockSignals(True)
            self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
            self._table_combo.blockSignals(False); self._columns = []; return
        self._table_combo.blockSignals(True)
        self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
        try:
            for t in self.engine.catalog.get_database(db).table_names:
                self._table_combo.addItem(t)
        except Exception: pass
        finally: self._table_combo.blockSignals(False)

    def _on_table_changed(self, t):
        if not t or t.startswith("--"): self._columns = []; return
        db = self._db_combo.currentText()
        try:
            tbl = self.engine.catalog.get_database(db).table(t)
            # 接驳层的 `列.sql_type` **本身就是类型名串**（'文本' / '整数' …），
            # 参照项目那边是个带 `to_sql()` 的对象——这是两边的模型不一样。
            # `auto_increment` 霸下恒为 False（不做自增）。
            self._columns = [(c.name, c.sql_type, c.nullable, c.auto_increment)
                           for c in tbl.columns]
        except Exception: self._columns = []; return
        self._rebuild_values_grid()

    def _on_batch_changed(self, n):
        if self._columns: self._rebuild_values_grid()

    def _rebuild_values_grid(self):
        # **`takeAt` 先摘掉，不能只 `deleteLater`**：`deleteLater` 只是排队，
        # 旧格子在**这一轮**事件循环里还挂在布局上，而下面 `build_sql` 是按
        # 坐标 `itemAtPosition(行, 列)` 取值的——取到的是旧控件，人刚敲进去
        # 的字被当成没敲。改"批量行数"时最明显。
        while self._values_grid.count():
            w = self._values_grid.takeAt(0).widget()
            if w is not None:
                w.setParent(None); w.deleteLater()
        if not self._columns: return

        batch = self._batch_spin.value()
        # Header
        self._values_grid.addWidget(QLabel("列名"), 0, 0)
        self._values_grid.addWidget(QLabel("类型"), 0, 1)
        for b in range(batch):
            self._values_grid.addWidget(QLabel(f"第{b+1}行"), 0, b + 2)

        for r, (name, stype, nullable, auto_inc) in enumerate(self._columns):
            self._values_grid.addWidget(QLabel(name), r + 1, 0)
            self._values_grid.addWidget(QLabel(stype), r + 1, 1)
            type_label = QLabel(stype)
            type_label.setStyleSheet("color: #64748B; font-size: 10px;")
            self._values_grid.addWidget(type_label, r + 1, 1)
            for b in range(batch):
                edit = QLineEdit()
                if auto_inc:
                    edit.setPlaceholderText("自增")
                    edit.setEnabled(False)
                    edit.setStyleSheet("background: #F1F5F9;")
                elif not nullable:
                    edit.setPlaceholderText("必填")
                else:
                    # 霸下的空字面量是 `空`（写 NULL 它也认，同义词）。
                    edit.setPlaceholderText("空")
                self._values_grid.addWidget(edit, r + 1, b + 2)

    def _get_db_name(self): return self._db_combo.currentText()
    def _get_table_name(self): return self._table_combo.currentText()

    def build_sql(self) -> str:
        db = self._db_combo.currentText(); tbl = self._table_combo.currentText()
        if not db or db.startswith("--") or not tbl or tbl.startswith("--"): return ""
        if not self._columns: return ""

        batch = self._batch_spin.value()
        col_names = [c[0] for c in self._columns]
        non_auto_idx = [i for i, c in enumerate(self._columns) if not c[3]]

        value_rows = []
        for b in range(batch):
            vals = []
            for i, (name, stype, nullable, auto_inc) in enumerate(self._columns):
                if auto_inc:
                    continue
                edit = self._values_grid.itemAtPosition(i + 1, b + 2)
                if edit is None or not edit.widget():
                    vals.append("空")
                    continue
                text = edit.widget().text().strip()
                # `NULL` 也认（同义词），但拼出去的一律是中文那个。
                if text == "" or text.upper() == "NULL" or text == "空":
                    vals.append("空")
                else:
                    vals.append(_quote_val(text))
            if any(v != "空" for v in vals):
                value_rows.append(f"({', '.join(vals)})")

        if not value_rows:
            return ""

        insert_cols = [c for c in col_names if c not in
                       [self._columns[i][0] for i in range(len(self._columns)) if self._columns[i][3]]]
        # 实际只用非自增列（霸下不做自增，所以这一条其实是全列）
        used_cols = [self._columns[i][0] for i in non_auto_idx]

        cols_str = ", ".join(f"`{c}`" for c in used_cols)
        vals_str = ",\n       ".join(value_rows)
        # 霸下的 `插入` 不收 `INTO`，也不写库名；多个值组是一条语句。
        # `替换` 跟它是**同一个形状**，只换开头的词、外加"主键撞了整行覆盖"。
        词 = "替换" if self._替换_cb.isChecked() else "插入"
        return f"{词} `{tbl}` ({cols_str})\n值\n       {vals_str};"


# ===================================================================
# 3. UPDATE 更新面板
# ===================================================================

class UpdatePanel(BaseQueryPanel):
    """UPDATE 更新：选表、SET 列=值、WHERE"""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4); layout.setSpacing(8)

        row1 = QHBoxLayout(); row1.setSpacing(8)
        row1.addWidget(QLabel("数据库:"))
        self._db_combo = QComboBox(); self._db_combo.setMinimumWidth(120)
        self._db_combo.currentTextChanged.connect(self._on_db_changed)
        row1.addWidget(self._db_combo)
        row1.addWidget(QLabel("表:"))
        self._table_combo = QComboBox(); self._table_combo.setMinimumWidth(140)
        self._table_combo.currentTextChanged.connect(self._on_table_changed)
        row1.addWidget(self._table_combo)
        row1.addStretch()
        layout.addLayout(row1)

        # SET 区域
        set_group = QGroupBox("更新字段 (SET)")
        self._set_layout = QVBoxLayout(set_group)
        self._set_scroll = QScrollArea(); self._set_scroll.setWidgetResizable(True)
        self._set_scroll.setMaximumHeight(250); self._set_scroll.setFrameShape(QFrame.NoFrame)
        self._set_container = QWidget()
        self._set_grid = QGridLayout(self._set_container)
        self._set_grid.setContentsMargins(0, 0, 0, 0); self._set_grid.setSpacing(4)
        self._set_scroll.setWidget(self._set_container)
        self._set_layout.addWidget(self._set_scroll)
        layout.addWidget(set_group)

        # WHERE
        self._setup_where_section(layout, lambda: self._columns)

        # 按钮
        row3 = QHBoxLayout(); row3.addStretch()
        # 原先这儿还有一颗「生成 SQL」，跟这颗干的是同一件事，删了。
        # 理由写在 `BaseQueryPanel._emit_sql` 上。
        gen_exec_btn = QPushButton("生成并执行"); gen_exec_btn.setFixedWidth(90)
        gen_exec_btn.clicked.connect(self._emit_sql)
        gen_exec_btn.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #DC2626; border-radius: 4px; padding: 4px 8px; }")
        row3.addWidget(gen_exec_btn)
        layout.addLayout(row3)
        self._refresh_db_list()

    def _refresh_db_list(self):
        c = self._db_combo.currentText()
        self._db_combo.blockSignals(True)
        self._db_combo.clear(); self._db_combo.addItem("-- 选择数据库 --")
        try:
            for n in self.engine.catalog.database_names:
                self._db_combo.addItem(n)
        except Exception: pass
        finally: self._db_combo.blockSignals(False)
        if c and self._db_combo.findText(c) >= 0: self._db_combo.setCurrentText(c)

    def _on_db_changed(self, db):
        if not db or db.startswith("--"):
            self._table_combo.blockSignals(True)
            self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
            self._table_combo.blockSignals(False); self._columns = []; return
        self._table_combo.blockSignals(True)
        self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
        try:
            for t in self.engine.catalog.get_database(db).table_names:
                self._table_combo.addItem(t)
        except Exception: pass
        finally: self._table_combo.blockSignals(False)

    def _on_table_changed(self, t):
        if not t or t.startswith("--"): self._columns = []; return
        db = self._db_combo.currentText()
        try:
            self._columns = self.engine.catalog.get_database(db).table(t).column_names
        except Exception: self._columns = []; return
        self._rebuild_set_grid()

    def _rebuild_set_grid(self):
        # 理由同 `InsertPanel._rebuild_values_grid`：`deleteLater` 排的队，
        # 旧输入框这一轮还在格子里，按坐标取值会取到旧的。
        while self._set_grid.count():
            w = self._set_grid.takeAt(0).widget()
            if w is not None:
                w.setParent(None); w.deleteLater()
        for r, cn in enumerate(self._columns):
            self._set_grid.addWidget(QLabel(cn), r, 0)
            self._set_grid.addWidget(QLabel("="), r, 1)
            edit = QLineEdit(); edit.setPlaceholderText("新值（留空=不改）")
            edit.setMinimumWidth(150)
            self._set_grid.addWidget(edit, r, 2)

    def _get_db_name(self): return self._db_combo.currentText()
    def _get_table_name(self): return self._table_combo.currentText()

    def build_sql(self) -> str:
        db = self._db_combo.currentText(); tbl = self._table_combo.currentText()
        if not db or db.startswith("--") or not tbl or tbl.startswith("--"): return ""
        if not self._columns: return ""

        set_parts = []
        for r, cn in enumerate(self._columns):
            edit = self._set_grid.itemAtPosition(r, 2)
            if edit is None or not edit.widget():
                continue
            val = edit.widget().text().strip()
            if not val:
                continue
            # 赋值用 `空`（`NULL` 也认）。**注意这条不是"比较"**——
            # 判空要用 `为空`，那是另一回事。
            if val.upper() == "NULL" or val == "空":
                set_parts.append(f"`{cn}` = 空")
            else:
                set_parts.append(f"`{cn}` = {_quote_val(val)}")

        if not set_parts:
            return ""

        # 霸下的 `更新` 不写库名。
        sql = f"更新 `{tbl}`\n设 " + ",\n    ".join(set_parts)
        sql += self._build_where_clause()
        if not self._where_rows:
            sql += "\n-- ⚠ 没有 WHERE 条件，将更新全部行！"
        return sql + ";"


# ===================================================================
# 4. DELETE 删除面板
# ===================================================================

class DeletePanel(BaseQueryPanel):
    """DELETE 删除：选表、WHERE"""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4); layout.setSpacing(8)

        row1 = QHBoxLayout(); row1.setSpacing(8)
        row1.addWidget(QLabel("数据库:"))
        self._db_combo = QComboBox(); self._db_combo.setMinimumWidth(120)
        self._db_combo.currentTextChanged.connect(self._on_db_changed)
        row1.addWidget(self._db_combo)
        row1.addWidget(QLabel("表:"))
        self._table_combo = QComboBox(); self._table_combo.setMinimumWidth(140)
        self._table_combo.currentTextChanged.connect(self._on_table_changed)
        row1.addWidget(self._table_combo)
        row1.addStretch()
        layout.addLayout(row1)

        # WHERE
        self._setup_where_section(layout, lambda: self._columns)

        # 警告。**霸下的 `删除` 可以把 `其中` 整个省掉，省了就是清全表**——
        # 这句话在这儿是真话，一个字没动。
        warn = QLabel("⚠ 没有 WHERE 条件时将删除全部行！")
        warn.setStyleSheet("color: #DC2626; font-weight: bold; font-size: 12px;")
        layout.addWidget(warn)

        # 按钮
        row3 = QHBoxLayout(); row3.addStretch()
        # 原先这儿还有一颗「生成 SQL」，跟这颗干的是同一件事，删了。
        # 理由写在 `BaseQueryPanel._emit_sql` 上。
        gen_exec_btn = QPushButton("生成并执行"); gen_exec_btn.setFixedWidth(90)
        gen_exec_btn.clicked.connect(self._emit_sql)
        gen_exec_btn.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #DC2626; border-radius: 4px; padding: 4px 8px; }")
        row3.addWidget(gen_exec_btn)
        layout.addLayout(row3)
        self._refresh_db_list()

    def _refresh_db_list(self):
        c = self._db_combo.currentText()
        self._db_combo.blockSignals(True)
        self._db_combo.clear(); self._db_combo.addItem("-- 选择数据库 --")
        try:
            for n in self.engine.catalog.database_names:
                self._db_combo.addItem(n)
        except Exception: pass
        finally: self._db_combo.blockSignals(False)
        if c and self._db_combo.findText(c) >= 0: self._db_combo.setCurrentText(c)

    def _on_db_changed(self, db):
        if not db or db.startswith("--"):
            self._table_combo.blockSignals(True)
            self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
            self._table_combo.blockSignals(False); self._columns = []; return
        self._table_combo.blockSignals(True)
        self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
        try:
            for t in self.engine.catalog.get_database(db).table_names:
                self._table_combo.addItem(t)
        except Exception: pass
        finally: self._table_combo.blockSignals(False)

    def _on_table_changed(self, t):
        if not t or t.startswith("--"): self._columns = []; return
        db = self._db_combo.currentText()
        try:
            self._columns = self.engine.catalog.get_database(db).table(t).column_names
        except Exception: self._columns = []; return

    def _get_db_name(self): return self._db_combo.currentText()
    def _get_table_name(self): return self._table_combo.currentText()

    def build_sql(self) -> str:
        db = self._db_combo.currentText(); tbl = self._table_combo.currentText()
        if not db or db.startswith("--") or not tbl or tbl.startswith("--"): return ""
        # **`其中` 可以省，省了就是清全表。** 霸下的 `删除` 不写库名。
        sql = f"删除 从 `{tbl}`"
        sql += self._build_where_clause()
        if not self._where_rows:
            sql += "\n-- ⚠ 没有 WHERE 条件，将删除全部行！"
        return sql + ";"


# ===================================================================
# 5. JOIN 联表查询面板
# ===================================================================
#
# 注：参照项目里这个面板也没有挂到任何标签页上（`_QUERY_MODES` 里没有），
# 同样是存货。霸下的 `连接 / 内连接 / 左连接 / 右连接 + 在` 是有的，
# 所以这个面板的路是通的。

class JoinPanel(BaseQueryPanel):
    """JOIN 联表查询：多表、JOIN条件、选字段、WHERE"""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4); layout.setSpacing(8)

        # 数据库
        row_db = QHBoxLayout(); row_db.setSpacing(8)
        row_db.addWidget(QLabel("数据库:"))
        self._db_combo = QComboBox(); self._db_combo.setMinimumWidth(120)
        self._db_combo.currentTextChanged.connect(self._on_db_changed)
        row_db.addWidget(self._db_combo)
        row_db.addStretch()
        layout.addLayout(row_db)

        # 主表
        t1_group = QGroupBox("主表 (FROM)")
        t1l = QHBoxLayout(t1_group)
        t1l.addWidget(QLabel("表:"))
        self._t1_combo = QComboBox(); self._t1_combo.setMinimumWidth(140)
        self._t1_combo.currentTextChanged.connect(lambda t: self._load_all_columns())
        t1l.addWidget(self._t1_combo)
        t1l.addWidget(QLabel("别名:"))
        self._t1_alias = QLineEdit(); self._t1_alias.setPlaceholderText("如: u")
        self._t1_alias.setMaximumWidth(60)
        # **别名一改，字段清单就得重铺一遍。** `_load_all_columns` 里那句
        # `alias = alias_edit.text().strip() or t` 本来就打算用别名，可这格
        # 的 `textChanged` 原先**没接**——敲进去的别名等于白敲：字段列照旧写
        # 表名，只有 `在` 子句用它。两张不同的表还看不出来（都能跑），
        # **自连接**就成了 `查询 甲.a, 甲.b, 甲.a, 甲.b`——同一列报两遍、
        # 而且两边分不开，问 o 的 a 拿到的是 u 的 a，**一声不吭地给错行**。
        self._t1_alias.textChanged.connect(lambda _t: self._load_all_columns())
        t1l.addWidget(self._t1_alias)
        t1l.addStretch()
        layout.addWidget(t1_group)

        # JOIN 表
        j_group = QGroupBox("联接表 (JOIN)")
        jl = QVBoxLayout(j_group)
        jrow1 = QHBoxLayout(); jrow1.setSpacing(8)
        jrow1.addWidget(QLabel("JOIN类型:"))
        self._join_type = QComboBox()
        # 霸下这三个词：`内连接` / `左连接` / `右连接`（`连接` 也是内连接）。
        self._join_type.addItems(["内连接", "左连接", "右连接"])
        self._join_type.setFixedWidth(120)
        jrow1.addWidget(self._join_type)
        jrow1.addWidget(QLabel("表:"))
        self._t2_combo = QComboBox(); self._t2_combo.setMinimumWidth(140)
        self._t2_combo.currentTextChanged.connect(lambda t: self._load_all_columns())
        jrow1.addWidget(self._t2_combo)
        jrow1.addWidget(QLabel("别名:"))
        self._t2_alias = QLineEdit(); self._t2_alias.setPlaceholderText("如: o")
        self._t2_alias.setMaximumWidth(60)
        self._t2_alias.textChanged.connect(lambda _t: self._load_all_columns())
        jrow1.addWidget(self._t2_alias)
        jrow1.addStretch()
        jl.addLayout(jrow1)

        jrow2 = QHBoxLayout(); jrow2.setSpacing(8)
        jrow2.addWidget(QLabel("ON 条件:"))
        jrow2.addWidget(QLabel("主表."))
        self._on_col1 = QComboBox(); self._on_col1.setMinimumWidth(100)
        jrow2.addWidget(self._on_col1)
        jrow2.addWidget(QLabel("="))
        jrow2.addWidget(QLabel("联接表."))
        self._on_col2 = QComboBox(); self._on_col2.setMinimumWidth(100)
        jrow2.addWidget(self._on_col2)
        jrow2.addStretch()
        jl.addLayout(jrow2)
        layout.addWidget(j_group)

        # 字段选择
        col_group = QGroupBox("查询字段 (SELECT)")
        cgl = QVBoxLayout(col_group); cgl.setSpacing(2)
        btn_row = QHBoxLayout()
        self._select_all_cb = QCheckBox("全选")
        self._select_all_cb.toggled.connect(self._on_select_all)
        btn_row.addWidget(self._select_all_cb)
        inv_btn = QPushButton("反选"); inv_btn.setFixedWidth(45)
        inv_btn.clicked.connect(self._on_invert)
        btn_row.addWidget(inv_btn)
        btn_row.addStretch()
        cgl.addLayout(btn_row)
        self._col_scroll = QScrollArea(); self._col_scroll.setMaximumHeight(130)
        self._col_scroll.setWidgetResizable(True)
        self._col_scroll.setFrameShape(QFrame.NoFrame)
        self._col_container = QWidget()
        self._col_grid = QGridLayout(self._col_container)
        self._col_grid.setContentsMargins(0, 0, 0, 0); self._col_grid.setSpacing(2)
        self._col_scroll.setWidget(self._col_container)
        cgl.addWidget(self._col_scroll)
        layout.addWidget(col_group)

        # WHERE + ORDER + LIMIT
        self._setup_where_section(layout, lambda: self._all_columns)

        row5 = QHBoxLayout(); row5.setSpacing(8)
        row5.addWidget(QLabel("排序:"))
        self._order_combo = QComboBox(); self._order_combo.setMinimumWidth(100)
        self._order_combo.addItem("(不排序)")
        row5.addWidget(self._order_combo)
        self._order_asc_cb = QCheckBox("升序"); self._order_asc_cb.setChecked(True)
        row5.addWidget(self._order_asc_cb)
        row5.addWidget(QLabel("限制:"))
        self._limit_spin = QSpinBox(); self._limit_spin.setRange(0, 100000)
        self._limit_spin.setValue(100); self._limit_spin.setFixedWidth(70)
        row5.addWidget(self._limit_spin)
        row5.addWidget(QLabel("行"))
        row5.addStretch()
        # 原先这儿还有一颗「生成 SQL」，跟这颗干的是同一件事，删了。
        # 理由写在 `BaseQueryPanel._emit_sql` 上。
        gen_exec_btn = QPushButton("生成并执行"); gen_exec_btn.setFixedWidth(90)
        gen_exec_btn.clicked.connect(self._emit_sql)
        gen_exec_btn.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #16A34A; border-radius: 4px; padding: 4px 8px; }")
        row5.addWidget(gen_exec_btn)
        layout.addLayout(row5)

        self._all_columns: list[str] = []
        self._refresh_db_list()

    def _refresh_db_list(self):
        c = self._db_combo.currentText()
        self._db_combo.blockSignals(True)
        self._db_combo.clear(); self._db_combo.addItem("-- 选择数据库 --")
        try:
            for n in self.engine.catalog.database_names:
                self._db_combo.addItem(n)
        except Exception: pass
        finally: self._db_combo.blockSignals(False)
        if c and self._db_combo.findText(c) >= 0: self._db_combo.setCurrentText(c)

    def _on_db_changed(self, db):
        if not db or db.startswith("--"):
            for cb in (self._t1_combo, self._t2_combo):
                cb.blockSignals(True); cb.clear(); cb.addItem("-- 选择表 --")
                cb.blockSignals(False)
            return
        for cb in (self._t1_combo, self._t2_combo):
            cb.blockSignals(True); cb.clear(); cb.addItem("-- 选择表 --")
            try:
                for t in self.engine.catalog.get_database(db).table_names:
                    cb.addItem(t)
            except Exception: pass
            finally: cb.blockSignals(False)

    def _load_all_columns(self):
        db = self._db_combo.currentText()
        all_cols = []
        for t_combo, alias_edit, on_combo in [
            (self._t1_combo, self._t1_alias, self._on_col1),
            (self._t2_combo, self._t2_alias, self._on_col2),
        ]:
            t = t_combo.currentText()
            if not t or t.startswith("--"): continue
            alias = alias_edit.text().strip() or t
            try:
                cols = self.engine.catalog.get_database(db).table(t).column_names
            except Exception: cols = []
            for cn in cols:
                all_cols.append(f"{alias}.{cn}")
            on_combo.blockSignals(True); on_combo.clear()
            for cn in cols: on_combo.addItem(cn)
            on_combo.blockSignals(False)
        self._all_columns = all_cols
        self._rebuild_col_checkboxes()
        self._update_order_combo()

    def _rebuild_col_checkboxes(self):
        """按 `_all_columns` 重铺一遍勾选框。

        **先把旧的摘干净。** `deleteLater` 只是排队，控件这一轮还挂在布局
        上，`itemAt` 照样数得到——不清的话每换一次表就往上叠一层，两表联查
        时会拼出 `查询 u.a, u.b, u.a, u.b, o.a, o.c` 这种同一列报两遍的语句
        （老面板从没挂到页面上过，这条一直没露出来）。
        """
        while self._col_grid.count():
            w = self._col_grid.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for i, cn in enumerate(self._all_columns):
            cb = QCheckBox(cn); cb.setChecked(True)
            self._col_grid.addWidget(cb, i // 4, i % 4)
        self._select_all_cb.setChecked(True)

    def _update_order_combo(self):
        cur = self._order_combo.currentText()
        self._order_combo.blockSignals(True); self._order_combo.clear()
        self._order_combo.addItem("(不排序)")
        for c in self._all_columns: self._order_combo.addItem(c)
        if cur and self._order_combo.findText(cur) >= 0: self._order_combo.setCurrentText(cur)
        self._order_combo.blockSignals(False)

    def _on_select_all(self, checked):
        for i in range(self._col_grid.count()):
            w = self._col_grid.itemAt(i).widget()
            if isinstance(w, QCheckBox): w.setChecked(checked)

    def _on_invert(self):
        for i in range(self._col_grid.count()):
            w = self._col_grid.itemAt(i).widget()
            if isinstance(w, QCheckBox): w.setChecked(not w.isChecked())

    def _get_selected_cols(self):
        return [self._col_grid.itemAt(i).widget().text()
                for i in range(self._col_grid.count())
                if isinstance(self._col_grid.itemAt(i).widget(), QCheckBox)
                and self._col_grid.itemAt(i).widget().isChecked()]

    def _get_db_name(self): return self._db_combo.currentText()

    def build_sql(self) -> str:
        db = self._db_combo.currentText()
        if not db or db.startswith("--"): return ""
        t1 = self._t1_combo.currentText(); t2 = self._t2_combo.currentText()
        if not t1 or t1.startswith("--") or not t2 or t2.startswith("--"): return ""

        a1 = self._t1_alias.text().strip() or t1
        a2 = self._t2_alias.text().strip() or t2

        cols = self._get_selected_cols()
        cols_str = ", ".join(cols) if cols else f"{a1}.*, {a2}.*"

        jt = self._join_type.currentText()
        on1 = self._on_col1.currentText(); on2 = self._on_col2.currentText()

        # 别名用 `作为`（`AS` 在霸下是同义词，写中文的更稳）；ON 那个词是 `在`。
        sql = f"查询 {cols_str}\n从 `{t1}` 作为 {a1}"
        sql += f"\n{jt} `{t2}` 作为 {a2} 在 {a1}.`{on1}` = {a2}.`{on2}`"

        sql += self._build_where_clause()

        order = self._order_combo.currentText()
        if order and order != "(不排序)":
            d = "升序" if self._order_asc_cb.isChecked() else "降序"
            sql += f"\n排序 按 {order} {d}"

        lim = self._limit_spin.value()
        if lim > 0: sql += f"\n限制 {lim}"

        return sql + ";"


# ===================================================================
# 6. DDL 建表面板
# ===================================================================

class DDLPanel(BaseQueryPanel):
    """DDL：CREATE TABLE / DROP TABLE / ALTER TABLE"""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4); layout.setSpacing(8)

        # 操作类型
        row0 = QHBoxLayout(); row0.setSpacing(8)
        row0.addWidget(QLabel("DDL 操作:"))
        self._ddl_type = QComboBox()
        # 三项对着霸下的三条：`建表` / `删表` / `删除 从 …`。
        # **霸下没有截断表**：清空一张表就是不带 `其中` 的删除（全表）。
        self._ddl_type.addItems(["建表", "删表", "清空表"])
        self._ddl_type.currentTextChanged.connect(self._on_ddl_type_changed)
        self._ddl_type.setFixedWidth(160)
        row0.addWidget(self._ddl_type)
        row0.addStretch()
        layout.addLayout(row0)

        # 数据库 + 表名
        row1 = QHBoxLayout(); row1.setSpacing(8)
        row1.addWidget(QLabel("数据库:"))
        self._db_combo = QComboBox(); self._db_combo.setMinimumWidth(120)
        self._db_combo.currentTextChanged.connect(self._on_db_changed)
        row1.addWidget(self._db_combo)
        row1.addWidget(QLabel("表名:"))
        self._table_edit = QLineEdit()
        self._table_edit.setPlaceholderText("输入表名...")
        self._table_edit.setMinimumWidth(160)
        row1.addWidget(self._table_edit, 1)
        layout.addLayout(row1)

        # CREATE TABLE 专属：列定义
        self._create_group = QGroupBox("列定义 (CREATE TABLE)")
        cl = QVBoxLayout(self._create_group); cl.setSpacing(4)

        self._cols_container = QVBoxLayout()
        self._cols_container.setSpacing(2)
        cl.addLayout(self._cols_container)

        add_col_btn = QPushButton("＋ 添加列")
        add_col_btn.setFixedWidth(80)
        add_col_btn.clicked.connect(self._on_add_column)
        cl.addWidget(add_col_btn)
        layout.addWidget(self._create_group)

        # 拼不出来时的回话处。**照 `IndexPanel._result_hint` 的老样子**：
        # 说不出口的话由这里说，`build_sql` 一律回空串。
        self._提示 = QLabel("")
        self._提示.setStyleSheet("color: #F59E0B; font-size: 11px;")
        layout.addWidget(self._提示)

        # 按钮
        row3 = QHBoxLayout(); row3.addStretch()
        # 原先这儿还有一颗「生成 SQL」，跟这颗干的是同一件事，删了。
        # 理由写在 `BaseQueryPanel._emit_sql` 上。
        gen_exec_btn = QPushButton("生成并执行"); gen_exec_btn.setFixedWidth(90)
        gen_exec_btn.clicked.connect(self._emit_sql)
        gen_exec_btn.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #2563EB; border-radius: 4px; padding: 4px 8px; }")
        row3.addWidget(gen_exec_btn)
        layout.addLayout(row3)
        self._refresh_db_list()

    def _refresh_db_list(self):
        c = self._db_combo.currentText()
        self._db_combo.blockSignals(True)
        self._db_combo.clear(); self._db_combo.addItem("-- 选择数据库 --")
        try:
            for n in self.engine.catalog.database_names:
                self._db_combo.addItem(n)
        except Exception: pass
        finally: self._db_combo.blockSignals(False)
        if c and self._db_combo.findText(c) >= 0: self._db_combo.setCurrentText(c)

    def _on_db_changed(self, db): pass

    def _on_ddl_type_changed(self, ddl_type):
        self._create_group.setVisible(ddl_type == "建表")

    def _on_add_column(self):
        row = QHBoxLayout(); row.setSpacing(4)
        name_edit = QLineEdit(); name_edit.setPlaceholderText("列名")
        name_edit.setMaximumWidth(120)
        row.addWidget(name_edit)
        type_combo = QComboBox()
        # **霸下只有这五个类型名**（`schema.TYPE_NAMES`）。
        # 写别的当场被拒，而拒出来的错会指到这一格。
        type_combo.addItems(["整数", "实数", "文本", "二进制", "时间"])
        type_combo.setMaximumWidth(130)
        row.addWidget(type_combo)
        not_null_cb = QCheckBox("非空")
        row.addWidget(not_null_cb)
        pk_cb = QCheckBox("主键")
        row.addWidget(pk_cb)
        # **霸下不做自增**：勾留着，但点不动，也永远不进语句。
        # 主键的值得自己给。
        auto_inc_cb = QCheckBox("自增")
        auto_inc_cb.setEnabled(False)
        auto_inc_cb.setToolTip("霸下不做自增——主键得自己给值")
        row.addWidget(auto_inc_cb)
        del_btn = QToolButton(); del_btn.setText("✕")
        del_btn.setFixedSize(22, 22)
        del_btn.clicked.connect(lambda: (self._cols_container.removeItem(row),
                                         self._delete_layout(row)))
        row.addWidget(del_btn)
        row.addStretch()
        self._cols_container.addLayout(row)

    @staticmethod
    def _delete_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            elif item.layout(): DDLPanel._delete_layout(item.layout())

    def _get_db_name(self): return self._db_combo.currentText()

    def build_sql(self) -> str:
        # 提示是**上一次**的，先清掉；下面哪条路真拼不出来，哪条路自己写。
        self._提示.setText("")
        db = self._db_combo.currentText()
        if not db or db.startswith("--"): return ""
        tbl = self._table_edit.text().strip()
        if not tbl: return ""

        ddl_type = self._ddl_type.currentText()

        if ddl_type == "删表":
            # 霸下没有 `IF EXISTS`，也没有库名那一截（参照项目那边是
            # `DROP TABLE IF EXISTS ...`，多出来的部分霸下不认）。
            return f"删表 `{tbl}`;"
        if ddl_type == "清空表":
            # **霸下没有截断表**：清空就是不带 `其中` 的删除，全表。
            return f"删除 从 `{tbl}`;"

        # CREATE TABLE
        col_defs = []
        for i in range(self._cols_container.count()):
            item = self._cols_container.itemAt(i)
            if not item.layout(): continue
            row = item.layout()
            widgets = []
            for j in range(row.count()):
                w = row.itemAt(j).widget()
                if w: widgets.append(w)
            if len(widgets) < 2: continue
            name = widgets[0].text().strip() if isinstance(widgets[0], QLineEdit) else ""
            stype = widgets[1].currentText() if isinstance(widgets[1], QComboBox) else "整数"
            if not name: continue
            # 反引号把标识符包起来——霸下认这个，不加的话列名叫 `时间`
            # 这种（跟类型名同字）就说不清了。
            defn = f"  `{name}` {stype}"
            # Check the checkboxes（勾上的字就是霸下那套约束词：
            # `主键` / `非空`；`自增` 是禁用的，勾不上）
            for w in widgets[2:]:
                if isinstance(w, QCheckBox) and w.isChecked():
                    defn += f" {w.text().replace(' ', '_').upper()}"
            col_defs.append(defn)

        if not col_defs:
            # **一列都没有时，回空串，不回一句假 SQL。**
            # 原先这儿拼的是 `-- 请添加至少一列定义\n-- 建表 \`甲\` ();` ——
            # `--` 在霸下是**行注释**（`sql.py:335`），这一整串被引擎
            # `parse_all` 认成 **0 条语句**。它长得像 SQL、按钮也不灰，
            # 点下去只会从闸口那头换来一句「✗ 没有语句」；万一哪天注释给
            # 挪掉一半，剩下的 ``建表 `甲` ()`` 就是一张没有列的表。
            # 别的面板拼不出来时一律回空串（`IndexPanel` 就是这么办的），
            # 说不出口的话由面板自己那行提示说。
            self._提示.setText("请先「＋ 添加列」——霸下建表至少要一列")
            return ""
        self._提示.setText("")
        return f"建表 `{tbl}` (\n" + ",\n".join(col_defs) + "\n);"


# ===================================================================
# 7. 🔎 快速查询面板
# ===================================================================
#
# **底下这条通路整个换过了。** 参照项目那边这个面板不走 SQL，它拼一个
# `--direct-query:...` 的私活串给 `engine.direct_query(...)` —— 霸下**没有
# 这条路**（闸口只有 `取` / `发` 两个口，没有"绕过解析"的第三口）。
# 现在这个面板拼的是**正常的 `查询` 语句**，走 `引擎.取`。
#
# 界面上原先那几句 `B+ 树` / `不经过 SQL 解析` 是照搬参照项目来的，在霸下
# **每一个字都不成立**，已经清了：
#   * 霸下不用 B+ 树（自研的区 + 榫卯），索引条目按列值编码；
#   * 闸口是**按前缀扫索引表取候选集**（`syntax` 里那段 `_pick_index`），
#     不是二分 seek；
#   * 这个面板拼的既然是正常 `查询` 语句，也就**没有"绕过 SQL 解析"这回事**
#     ——闸口只有 `取` / `发` 两个口。

class IndexPanel(BaseQueryPanel):
    """快速查询：选库→选表→选字段→输入值→拼一条正常的 `查询` 语句。

    **这儿没有捷径。** 拼出来的是普通的 `查询 … 其中 …`，走 `引擎.取`，
    跟手写框那条路是同一条。索引只是引擎自己可能用上的一样东西：
      - 主键字段 → `其中 主键 = 值` 被认成"主键全等"，闸口**直取那一行**
        （`syntax` 里那句注释原文：主键全等就直取，`arena.get` 一次定位）
      - 非主键字段 → 用不用索引由引擎自己定；用了也是**按前缀扫索引表取
        候选集**，不是二分 seek；没用上就是全表扫 + 逐行筛
    """

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        hint = QLabel("🔎 快速查询 — 选字段 → 输入值 → 拼一条正常的 `查询` 语句"
                      "（走引擎，跟手写框那条路是同一条）")
        hint.setStyleSheet("font-size: 13px; font-weight: bold; color: #7C3AED; padding: 4px 0;")
        layout.addWidget(hint)

        # 行1: 数据库 + 表
        row1 = QHBoxLayout(); row1.setSpacing(10)
        row1.addWidget(QLabel("数据库"))
        self._db_combo = QComboBox(); self._db_combo.setMinimumWidth(140)
        self._db_combo.setStyleSheet("QComboBox { font-size: 13px; padding: 4px; }")
        self._db_combo.currentTextChanged.connect(self._on_db_changed)
        row1.addWidget(self._db_combo)

        row1.addWidget(QLabel("表"))
        self._table_combo = QComboBox(); self._table_combo.setMinimumWidth(160)
        self._table_combo.setStyleSheet("QComboBox { font-size: 13px; padding: 4px; }")
        self._table_combo.currentTextChanged.connect(self._on_table_changed)
        row1.addWidget(self._table_combo)
        row1.addStretch()
        layout.addLayout(row1)

        # 行2: 查询字段（下拉框选任意列）
        row2 = QHBoxLayout(); row2.setSpacing(10)
        row2.addWidget(QLabel("查询字段"))
        self._field_combo = QComboBox()
        self._field_combo.setMinimumWidth(160)
        self._field_combo.setToolTip("选择要用作查询条件的字段（下拉框选择，无需手写）")
        self._field_combo.currentTextChanged.connect(self._on_field_changed)
        row2.addWidget(self._field_combo)

        row2.addWidget(QLabel("查询方式"))
        self._query_mode = QComboBox()
        self._query_mode.addItems(["精确 =", "范围 BETWEEN", "模糊 LIKE", "全部扫描"])
        self._query_mode.setFixedWidth(140)
        self._query_mode.currentTextChanged.connect(self._on_mode_changed)
        row2.addWidget(self._query_mode)
        row2.addStretch()
        layout.addLayout(row2)

        # 行3: 值输入
        row3 = QHBoxLayout(); row3.setSpacing(8)
        self._key_label = QLabel("值")
        row3.addWidget(self._key_label)

        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText("输入查询值...")
        self._key_edit.setMinimumWidth(180)
        self._key_edit.returnPressed.connect(self._on_query)
        row3.addWidget(self._key_edit)

        self._range_label = QLabel("到")
        self._range_label.setVisible(False)
        row3.addWidget(self._range_label)

        self._key_end_edit = QLineEdit()
        self._key_end_edit.setPlaceholderText("结束值")
        self._key_end_edit.setVisible(False)
        self._key_end_edit.setMinimumWidth(100)
        self._key_end_edit.returnPressed.connect(self._on_query)
        row3.addWidget(self._key_end_edit)
        row3.addStretch()
        layout.addLayout(row3)

        # 行4: 字段信息 + 行数 + 按钮
        row4 = QHBoxLayout(); row4.setSpacing(10)
        self._field_info = QLabel("")
        self._field_info.setStyleSheet("color: #64748B; font-size: 11px;")
        row4.addWidget(self._field_info, 1)

        row4.addWidget(QLabel("行数"))
        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(1, 10000)
        self._limit_spin.setValue(200); self._limit_spin.setFixedWidth(70)
        # 引擎的上限，如实告诉人。
        self._limit_spin.setToolTip(
            "语句里的 `限制`。霸下引擎另有硬上限：一次结果最多 20 万行，"
            "一趟扫描最多 200 万行")
        row4.addWidget(self._limit_spin)

        query_btn = QPushButton("⚡ 查询")
        query_btn.setFixedSize(100, 36)
        query_btn.setToolTip(
            "拼一条 `查询 … 其中 …` 发出去走。用不用索引、怎么用，由引擎自己定")
        query_btn.clicked.connect(self._on_query)
        query_btn.setStyleSheet(
            "QPushButton { font-size: 14px; font-weight: bold; color: #FFF; "
            "background-color: #7C3AED; border-radius: 8px; }"
            "QPushButton:hover { background-color: #6D28D9; }")
        row4.addWidget(query_btn)
        layout.addLayout(row4)

        self._result_hint = QLabel("")
        self._result_hint.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._result_hint)

        layout.addStretch()
        self._refresh_db_list()

    def _on_query(self):
        sql = self.build_sql()
        if sql:
            self.sql_generated.emit(sql)

    def _on_mode_changed(self, mode: str):
        is_range = mode == "范围 BETWEEN"
        is_full = mode == "全部扫描"
        self._range_label.setVisible(is_range)
        self._key_end_edit.setVisible(is_range)
        self._key_edit.setVisible(not is_full)
        self._key_label.setText("值" if not is_full else "")
        if is_range:
            self._key_edit.setPlaceholderText("起始值")
        elif is_full:
            self._key_edit.setPlaceholderText("(无需输入)")
        elif mode == "模糊 LIKE":
            self._key_edit.setPlaceholderText("模糊匹配，如: 张")
        else:
            self._key_edit.setPlaceholderText("查询值，如: 1")

    # ── 数据库/表/字段 ─────────────────────────────────

    def _refresh_db_list(self):
        c = self._db_combo.currentText()
        self._db_combo.blockSignals(True)
        self._db_combo.clear(); self._db_combo.addItem("-- 选择数据库 --")
        try:
            for n in self.engine.catalog.database_names:
                self._db_combo.addItem(n)
        except Exception: pass
        finally: self._db_combo.blockSignals(False)
        if c and self._db_combo.findText(c) >= 0:
            self._db_combo.setCurrentText(c)

    def _on_db_changed(self, db):
        if not db or db.startswith("--"):
            self._table_combo.blockSignals(True)
            self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
            self._table_combo.blockSignals(False)
            self._clear_fields(); return
        self._table_combo.blockSignals(True)
        self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
        try:
            for t in self.engine.catalog.get_database(db).table_names:
                self._table_combo.addItem(t)
        except Exception: pass
        finally: self._table_combo.blockSignals(False)

    def _on_table_changed(self, t):
        if not t or t.startswith("--"):
            self._clear_fields(); return
        db = self._db_combo.currentText()
        try:
            tbl = self.engine.catalog.get_database(db).table(t)
            self._table_columns = tbl.column_names
            self._table_pk = tbl.primary_key
            # 接驳层的 `列.sql_type` 本身就是类型名串（参照项目那边是对象）。
            self._table_col_types = {c.name: c.sql_type for c in tbl.columns}

            # 填充字段下拉框
            self._field_combo.blockSignals(True)
            self._field_combo.clear()
            for cn in tbl.column_names:
                label = cn
                if cn in (tbl.primary_key or []):
                    label = f"🔑 {cn} (主键)"
                col_type = self._table_col_types.get(cn, "")
                self._field_combo.addItem(f"{label}  [{col_type}]")
            self._field_combo.blockSignals(False)
            self._on_field_changed()
        except Exception as e:
            self._clear_fields()
            self._field_info.setText(f"加载失败: {e}")

    def _clear_fields(self):
        self._field_combo.blockSignals(True)
        self._field_combo.clear(); self._field_combo.addItem("(选择表后加载)")
        self._field_combo.blockSignals(False)
        self._table_columns = []; self._table_pk = []
        self._table_col_types = {}
        self._field_info.setText("")

    def _on_field_changed(self, field_text: str = ""):
        """选中字段时显示字段信息"""
        if not self._table_columns:
            return
        idx = self._field_combo.currentIndex()
        if idx < 0 or idx >= len(self._table_columns):
            return
        col_name = self._table_columns[idx]
        col_type = self._table_col_types.get(col_name, "?")
        is_pk = col_name in (self._table_pk or [])
        # **主键那格说的是"引擎会直取"，不是"索引二分"。** 索引条目按列值
        # 编码，闸口按前缀扫它取候选，没有 seek。非主键那格不替引擎打包票
        # ——用不用索引由它自己定。
        method = ("🔑 主键全等直取（一次定位）" if is_pk
                  else "引擎自定（用上索引=按前缀扫候选）")
        self._field_info.setText(
            f"`{col_name}` {col_type}  "
            f"{'🔑 主键' if is_pk else '📋 普通列'}  |  查询方式: {method}"
        )

    def _get_selected_field(self) -> str:
        idx = self._field_combo.currentIndex()
        if 0 <= idx < len(self._table_columns):
            return self._table_columns[idx]
        return ""

    def _is_pk_field(self) -> bool:
        field = self._get_selected_field()
        return field in (self._table_pk or [])

    def _get_db_name(self): return self._db_combo.currentText()
    def _get_table_name(self): return self._table_combo.currentText()

    # ── 值怎么拼进语句 ─────────────────────────────────

    def _拼值(self, 文本: str) -> str:
        """把敲进去的一串字拼成霸下认的字面量。

        **按列类型来**：整数/实数裸写，文本/时间加单引号（内部单引号写成
        两个）。裸写的数是不是数，交给引擎去判——它报的错比这儿一眼看得出
        更多东西。**空值不在这儿管**：判空要用 `为空`，不能拿 `=` 比。
        """
        s = 文本.strip()
        类型 = self._table_col_types.get(self._get_selected_field(), "")
        if 类型 in ("整数", "实数"):
            return s
        return "'%s'" % _escape_sql(s)

    def build_sql(self) -> str:
        db = self._db_combo.currentText(); tbl = self._table_combo.currentText()
        if not db or db.startswith("--") or not tbl or tbl.startswith("--"): return ""

        mode = self._query_mode.currentText()
        key_val = self._key_edit.text().strip()
        limit = self._limit_spin.value()
        field = self._get_selected_field()

        if mode == "精确 =":
            if not key_val:
                self._result_hint.setText("请输入查询值")
                self._result_hint.setStyleSheet("color: #F59E0B; font-size: 11px;")
                return ""
            return (f"查询 * 从 `{tbl}` 其中 `{field}` = {self._拼值(key_val)}"
                    f" 限制 {limit};")

        elif mode == "范围 BETWEEN":
            end_val = self._key_end_edit.text().strip()
            if not key_val or not end_val:
                self._result_hint.setText("请输入起始值和结束值")
                self._result_hint.setStyleSheet("color: #F59E0B; font-size: 11px;")
                return ""
            # 霸下的范围是 `介于 A 到 B`。
            return (f"查询 * 从 `{tbl}` 其中 `{field}` 介于 "
                    f"{self._拼值(key_val)} 到 {self._拼值(end_val)}"
                    f" 限制 {limit};")

        elif mode == "模糊 LIKE":
            if not key_val:
                self._result_hint.setText("请输入查询值")
                self._result_hint.setStyleSheet("color: #F59E0B; font-size: 11px;")
                return ""
            # 霸下的模糊是 `像`，通配符还是 `%`。
            return (f"查询 * 从 `{tbl}` 其中 `{field}` 像 "
                    f"'%{_escape_sql(key_val)}%' 限制 {limit};")

        else:  # 全部扫描
            # **全表**：不带 `其中`。这一条代价最大，引擎也拦着 200 万行的顶。
            return f"查询 * 从 `{tbl}` 限制 {limit};"


# ===================================================================
# 查询构建器容器
# ===================================================================

# ===================================================================
# 手写语句 —— 语言里每一条语句的入口
# ===================================================================

class RawSqlPanel(QWidget):
    """直接敲语句。

    **为什么非得有它。** 语法层认 18 条语句形态（`sql.py` 的
    `parse_statement`，801-876 行），而表单只盖得住"能做成表单的那几条"。
    集合运算（`并集`/`交集`/`差集`）、`当…那么…否则…结束`、聚合函数、
    以及任意嵌套的表达式——这几样**做不成下拉框**。硬做出来的界面会让人
    以为"这库就这么几个写法"，而真相是它认得的东西多得多。

    手写框是这几样的唯一入口。这也是它排在模式条第一个的原因。

    **不继承 `BaseQueryPanel`**：那个基类假设面板有"库/表"概念
    （`_get_db_name` / `_get_table_name` 在它那儿是抽象），而敲语句不必先
    选表，硬套只会把 `_ensure_built` 那一套无谓地拉进来。

    信号叫 `sql_generated`，和别的面板同形——`QueryBuilder` 里那句
    `panel.sql_generated.connect(...)` 的写法一行都不用改。
    """

    sql_generated = Signal(str)

    #: 历史最多留多少条。**只在内存里**，不落盘——项目里不留中间产物。
    历史上限 = 50

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._历史: list[str] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        hint = QLabel(
            "直接敲语句。语言里的每一条都在这儿——包括表单做不出来的"
            "集合运算、`当…那么…` 和聚合函数。多条用分号隔开，整段一起跑。")
        hint.setStyleSheet("color: #64748B; font-size: 12px;")
        bar.addWidget(hint, 1)

        self._历史按钮 = QPushButton("历史")
        self._历史按钮.setFixedWidth(60)
        self._历史按钮.setToolTip("回填以前敲过的语句（回填不直接跑，还能改）")
        self._历史按钮.clicked.connect(self._弹历史)
        self._历史按钮.setEnabled(False)
        bar.addWidget(self._历史按钮)
        layout.addLayout(bar)

        self._编辑器 = QPlainTextEdit()
        self._编辑器.setFont(QFont("Consolas", 11))
        self._编辑器.setPlaceholderText(
            "例如：\n"
            "    查询 `标题`, 计数(*) 从 `网页` 分组 按 `标题` 排序 按 计数(*) 降序\n"
            "    建索引 `网页` 按 (`标题`)\n"
            "    压实\n"
        )
        self._编辑器.setMinimumHeight(110)
        # `SQLHighlighter` 收的是**文档**，不是控件。
        # 它是从参照项目搬过来的，而在那边**定义了从来没人用**——这是它
        # 第一次真派上用场。词表直接引 `sql.KEYWORDS`，引擎认什么就高亮
        # 什么，"看着认识、其实不认"这种事就不会发生。
        self._高亮器 = SQLHighlighter(self._编辑器.document())
        layout.addWidget(self._编辑器, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._执行按钮 = QPushButton("▶ 执行  (Ctrl+Enter)")
        self._执行按钮.setStyleSheet(
            "QPushButton { padding: 6px 18px; font-weight: bold;"
            " background-color: #2563EB; color: white; border: none;"
            " border-radius: 4px; }"
            "QPushButton:hover { background-color: #1D4ED8; }")
        self._执行按钮.clicked.connect(self.执行)
        btn_row.addWidget(self._执行按钮)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # `Ctrl+Enter` 用 `QShortcut`，**不重写 `keyPressEvent`**：
        # `QPlainTextEdit` 那个还得管补全、缩进、撤销栈，重写容易漏掉一条；
        # `QShortcut` 是声明式挂载，碰都不碰编辑行为。
        # 挂两条是因为主键盘回车和小键盘回车在 Qt 里是**两个不同的键码**
        # （`Return` / `Enter`）——只挂一条，用另一个半边的人就按不出来，
        # 而且按不出来**不会有任何报错**，只会以为"这功能坏了"。
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.执行)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.执行)

    def 执行(self) -> None:
        """把编辑框里那**整段文本**原样投出去。

        **不在界面按分号拆。** 字符串字面量里完全可能有分号
        （`… 其中 正文 像 '%;%'`），在界面拆是**静默出错**的源头：拆错了
        不报错，只会把一条语句劈成两半各自跑一次。分号归谁管是**词法**的
        事，不是 `str.split` 的事——引擎那边 `parse_all` 本来就认得边界。
        """
        文本 = self._编辑器.toPlainText()
        if not 文本.strip():
            return
        self._记历史(文本)
        self.sql_generated.emit(文本)

    def _记历史(self, 文本: str) -> None:
        """记一条历史，满了从最旧的开始丢。"""
        if self._历史 and self._历史[-1] == 文本:
            return                      # 连着敲两次同一条，不记两遍
        self._历史.append(文本)
        if len(self._历史) > self.历史上限:
            del self._历史[0]
        self._历史按钮.setEnabled(True)

    def _弹历史(self) -> None:
        """点一条回填进编辑框。**回填不直接跑**——回填了还能改改再跑。"""
        if not self._历史:
            return
        菜单 = QMenu(self)
        for 文本 in reversed(self._历史):
            头一行 = 文本.strip().splitlines()
            动作 = 菜单.addAction((头一行[0][:60] if 头一行 else '(空)'))
            动作.setData(文本)
        选 = 菜单.exec(self._历史按钮.mapToGlobal(
            self._历史按钮.rect().bottomLeft()))
        if 选 is not None and 选.data():
            self._编辑器.setPlainText(选.data())
            self._编辑器.setFocus()


# ===================================================================
# 8. 「选库 + 选表」这一类的共用底座
# ===================================================================

class TablePickPanel(BaseQueryPanel):
    """「选库 + 选表」这一类的共用底座。

    老面板（查询 / 插入 / 更新 / 删除 / 联表）各自抄了一份一模一样的三件套
    （刷新库列表 / 选库填表 / 选表填列）。那是搬过来时的原样，**这一批新
    面板不再抄第五遍**；老面板一个字没动。

    子类实现 `_表变了(表名)`——选中的表换了该干什么（填列 / 亮按钮）。
    """

    def _建选择行(self, 布局):
        行 = QHBoxLayout(); 行.setSpacing(8)
        行.addWidget(QLabel("数据库:"))
        self._db_combo = QComboBox(); self._db_combo.setMinimumWidth(120)
        self._db_combo.currentTextChanged.connect(self._on_db_changed)
        行.addWidget(self._db_combo)
        行.addWidget(QLabel("表:"))
        self._table_combo = QComboBox(); self._table_combo.setMinimumWidth(160)
        self._table_combo.currentTextChanged.connect(self._on_table_changed)
        行.addWidget(self._table_combo)
        行.addStretch()
        布局.addLayout(行)

    def _refresh_db_list(self):
        c = self._db_combo.currentText()
        self._db_combo.blockSignals(True)
        self._db_combo.clear(); self._db_combo.addItem("-- 选择数据库 --")
        try:
            for n in self.engine.catalog.database_names:
                self._db_combo.addItem(n)
        except Exception: pass
        finally: self._db_combo.blockSignals(False)
        if c and self._db_combo.findText(c) >= 0:
            self._db_combo.setCurrentText(c)

    def _on_db_changed(self, db):
        self._table_combo.blockSignals(True)
        self._table_combo.clear(); self._table_combo.addItem("-- 选择表 --")
        if db and not db.startswith("--"):
            try:
                for t in self.engine.catalog.get_database(db).table_names:
                    self._table_combo.addItem(t)
            except Exception: pass
        self._table_combo.blockSignals(False)
        # 上面 blockSignals 了，`currentTextChanged` 不会自己响，得手动叫一声。
        self._表变了(self._table_combo.currentText())

    def _on_table_changed(self, t):
        self._表变了(t)

    def _表变了(self, 表名):
        """子类实现：选中的表换了。"""

    def _选中的库(self) -> str:
        n = self._db_combo.currentText()
        return "" if not n or n.startswith("--") else n

    def _选中的表(self) -> str:
        n = self._table_combo.currentText()
        return "" if not n or n.startswith("--") else n


# ===================================================================
# 9. ☰ 显示面板（`显示` 的四个子形态）
# ===================================================================

class ShowPanel(TablePickPanel):
    """`显示 表列表` / `显示 结构` / `显示 索引` / `显示 区域`。

    四条**都是取数的**（`_取数开头` 那份词表里有 `显示`），所以拼出来的
    语句走 `引擎.取`，结果直接铺到下面那张表上——跟手写框那条路一样。
    """

    #: 档名 -> 要不要表名。
    #: `表列表` **不带表名**（它列的就是库里所有表），另外三档都要。
    #: "区域"那一档在解析上表名可以省（`sql.py` 的 `parse_show`），但**执行
    #: 时必填**——引擎的原话是"维度区间是按表记的"。所以这儿也逼人选表，
    #: 不让一句必然报错的语句发出去。
    _档 = [("表列表", False), ("结构", True), ("索引", True), ("区域", True)]

    _说明词 = {
        "表列表": "库里所有你建过的表。**索引表不在里面**——看索引选上面那一档。",
        "结构": "这张表的列：列号 / 名字 / 类型 / 可空 / 默认 / 主键 / 维度。",
        "索引": "这张表上建过哪些索引、各按哪几列。**列是有次序的**，"
                "前缀匹配只认最左边起的一段。",
        "区域": "区域清单，以及本表在这几个区上的维度区间。**必须选表**"
                "——区间是按表记的。",
    }

    def _build_ui(self):
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(8, 4, 8, 4); 布局.setSpacing(8)

        行0 = QHBoxLayout(); 行0.setSpacing(8)
        行0.addWidget(QLabel("显示什么:"))
        self._档_combo = QComboBox()
        self._档_combo.addItems([名 for 名, _ in self._档])
        self._档_combo.setFixedWidth(150)
        self._档_combo.currentTextChanged.connect(self._档变了)
        行0.addWidget(self._档_combo)
        行0.addStretch()
        布局.addLayout(行0)

        self._建选择行(布局)

        self._说明 = QLabel("")
        self._说明.setStyleSheet("color: #64748B; font-size: 11px;")
        self._说明.setWordWrap(True)
        布局.addWidget(self._说明)

        行 = QHBoxLayout()
        跑 = QPushButton("生成并执行"); 跑.setFixedWidth(100)
        跑.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #2563EB; border-radius: 4px; padding: 4px 8px; }")
        跑.clicked.connect(self._发出)
        行.addWidget(跑); 行.addStretch()
        布局.addLayout(行)
        布局.addStretch()

        self._refresh_db_list()
        self._档变了(self._档_combo.currentText())

    def _要表(self) -> bool:
        return dict(self._档)[self._档_combo.currentText()]

    def _档变了(self, 档):
        self._table_combo.setEnabled(self._要表())
        self._说明.setText(self._说明词[档])

    def _发出(self):
        s = self.build_sql()
        if s:
            self.sql_generated.emit(s)
        else:
            # 发不出去时**说清缺哪一样**，别让人对着没反应的按钮猜。
            self._说明.setText("先选数据库" + ("和表" if self._要表() else "")
                               + "——这两样齐了才拼得出语句。")

    def build_sql(self) -> str:
        if not self._选中的库():
            return ""
        档 = self._档_combo.currentText()
        if not self._要表():
            return "显示 表列表;"
        表 = self._选中的表()
        if not 表:
            return ""
        return "显示 %s `%s`;" % (档, 表)


# ===================================================================
# 10. 🗂 索引管理面板（索引四件）
# ===================================================================

class IndexOpsPanel(TablePickPanel):
    """建索引 / 删索引 / 重建索引 / 检查索引。

    **`检查索引` 不按列**——它问的是"这张表的索引对不对"，不是"某一个索引
    对不对"，所以选中它时下面整块列清单收起来，拼出来的是 `检查索引 \\`表\\``。

    另外三条的列清单**是有次序的**：索引的前缀匹配只认最左边起的一段，
    `按 (a, b)` 和 `按 (b, a)` 不是同一张索引。所以这儿**不排序、不去重**，
    取列的次序就是表结构的列序。

    **删索引 / 重建索引要的列清单得跟建的时候一模一样（含次序），拿不准
    先点 `查看现有索引`**——那条按钮发的就是 `显示 索引`，把次序摆在眼前，
    而不是让人凭记忆勾。
    """

    _操作 = ["建索引", "删索引", "重建索引", "检查索引"]

    _说明词 = {
        "建索引": "勾几列就是按几列建。次序按**表结构的列序**——前缀匹配只认"
                  "最左边起的一段。要别的次序请走手写框。",
        "删索引": "列清单要跟建的时候**一模一样（含次序）**。拿不准先点"
                  "「查看现有索引」。",
        "重建索引": "同删索引，列清单要跟建时一致。重建 = 删掉重来，条目按"
                    "当前基表重算。",
        "检查索引": "只读、只报不修。它比的是「基表每行该有的条目键」和"
                    "「盘上有的条目键」，差集就是缺条目 / 多条目。**不按列。**",
    }

    def _build_ui(self):
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(8, 4, 8, 4); 布局.setSpacing(8)

        行0 = QHBoxLayout(); 行0.setSpacing(8)
        行0.addWidget(QLabel("操作:"))
        self._操作_combo = QComboBox()
        self._操作_combo.addItems(self._操作)
        self._操作_combo.setFixedWidth(130)
        self._操作_combo.currentTextChanged.connect(self._操作变了)
        行0.addWidget(self._操作_combo)
        行0.addStretch()
        布局.addLayout(行0)

        self._建选择行(布局)

        # 列清单（检查索引那一档整块收起）
        self._列组 = QGroupBox("索引列（**次序即勾选次序**，按表结构列序排）")
        gl = QVBoxLayout(self._列组); gl.setSpacing(2)
        self._列滚动 = QScrollArea(); self._列滚动.setMaximumHeight(120)
        self._列滚动.setWidgetResizable(True)
        self._列滚动.setFrameShape(QFrame.NoFrame)
        宿主 = QWidget()
        self._列格 = QGridLayout(宿主)
        self._列格.setContentsMargins(0, 0, 0, 0); self._列格.setSpacing(2)
        self._列滚动.setWidget(宿主)
        gl.addWidget(self._列滚动)
        布局.addWidget(self._列组)

        self._说明 = QLabel("")
        self._说明.setStyleSheet("color: #64748B; font-size: 11px;")
        self._说明.setWordWrap(True)
        布局.addWidget(self._说明)

        行 = QHBoxLayout(); 行.setSpacing(8)
        跑 = QPushButton("生成并执行"); 跑.setFixedWidth(100)
        跑.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #2563EB; border-radius: 4px; padding: 4px 8px; }")
        跑.clicked.connect(self._发出)
        行.addWidget(跑)
        看 = QPushButton("查看现有索引"); 看.setFixedWidth(110)
        看.setToolTip("发一条 `显示 索引` 出去，结果落到下面那张表上。\n"
                      "删索引 / 重建索引要的列清单次序从这里读，别凭记忆勾。")
        看.clicked.connect(self._看现有)
        行.addWidget(看); 行.addStretch()
        布局.addLayout(行)
        布局.addStretch()

        self._refresh_db_list()
        self._操作变了(self._操作_combo.currentText())

    def _表变了(self, 表名):
        self._填列(表名)

    def _填列(self, 表名):
        self._列组.setVisible(self._操作_combo.currentText() != "检查索引")
        while self._列格.count():
            w = self._列格.takeAt(0).widget()
            if w is not None:
                w.setParent(None); w.deleteLater()
        if not 表名 or 表名.startswith("--"):
            return
        try:
            列 = self.engine.catalog.get_database(
                self._选中的库()).table(表名).column_names
        except Exception:
            列 = []
        for i, cn in enumerate(列):
            cb = QCheckBox(cn)
            self._列格.addWidget(cb, i // 4, i % 4)

    def _操作变了(self, 操作):
        self._列组.setVisible(操作 != "检查索引")
        self._说明.setText(self._说明词[操作])

    def _勾中的列(self):
        return [self._列格.itemAt(i).widget().text()
                for i in range(self._列格.count())
                if isinstance(self._列格.itemAt(i).widget(), QCheckBox)
                and self._列格.itemAt(i).widget().isChecked()]

    def _看现有(self):
        表 = self._选中的表()
        if 表:
            self.sql_generated.emit("显示 索引 `%s`;" % 表)
        else:
            self._说明.setText("先选数据库和表。")

    def _发出(self):
        s = self.build_sql()
        if s:
            self.sql_generated.emit(s)
        else:
            self._说明.setText("先选数据库和表"
                               + ("，再勾至少一列" if self._操作_combo.currentText()
                                  != "检查索引" else "") + "。")

    def build_sql(self) -> str:
        表 = self._选中的表()
        if not self._选中的库() or not 表:
            return ""
        操作 = self._操作_combo.currentText()
        if 操作 == "检查索引":
            return "检查索引 `%s`;" % 表
        列们 = self._勾中的列()
        if not 列们:
            return ""
        return "%s `%s` 按 (%s);" % (
            操作, 表, ", ".join("`%s`" % c for c in 列们))


# ===================================================================
# 11. 🔧 改表面板（把活交给表结构编辑器）
# ===================================================================

class AlterPanel(TablePickPanel):
    """改表：**这一页不拼语句**，直接把活交给 `界面_设计表.EditTableDialog`。

    为什么不另起炉灶：那个对话框已经写完了"旧结构 -> 新结构"那一整套
    （`_结构差`），能说清楚的正好是引擎支持的三样——加列 / 删列 / 改名；
    改不动的（改类型、改可空、改主键、换列序、加主键列）它会**如实拒绝**
    并说清为什么。再写一份就是第二处将来会忘记同步改的地方。

    **一处它也没有**：`改表 … 换维度列`——那个只能走手写框。
    """

    def _build_ui(self):
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(8, 4, 8, 4); 布局.setSpacing(8)

        说明 = QLabel(
            "改表走的是表结构编辑器（加列 / 删列 / 改列名）。\n"
            "引擎的 `改表` 只有这三样：改不了已有列的类型、可空、默认值、主键，"
            "也换不了列序——那些对话框会如实拒绝并说明。\n"
            "换维度列请走手写框。")
        说明.setStyleSheet("color: #64748B; font-size: 11px;")
        说明.setWordWrap(True)
        布局.addWidget(说明)

        self._建选择行(布局)

        self._开 = QPushButton("打开表结构编辑器…")
        self._开.setFixedWidth(180)
        self._开.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #2563EB; border-radius: 4px; padding: 6px 10px; }")
        self._开.clicked.connect(self._打开)
        行 = QHBoxLayout(); 行.addWidget(self._开); 行.addStretch()
        布局.addLayout(行)
        布局.addStretch()

        self._refresh_db_list()
        self._表变了(self._table_combo.currentText())

    def _表变了(self, 表名):
        self._开.setEnabled(bool(表名 and not 表名.startswith("--")))

    def _打开(self):
        表 = self._选中的表()
        if not self._选中的库() or not 表:
            return
        # 对话框自己去 `引擎.发多` + 拉目录，**不碰闸口**——发还是投给工人
        # 线程。这儿只等它关。
        #
        # 收尾一律重拉：对话框取消时什么都不用刷（重拉一次也就是一趟缓存
        # 读，不亏），而"保存成功了却没刷"会让人在下拉框里看不到新列、
        # 以为没生效。宁可多刷一次，不可漏刷一次。
        EditTableDialog(self.engine, self._选中的库(), 表, self).exec()
        self.数据变了.emit()

    def build_sql(self) -> str:
        return ""      # 这一页不发语句


# ===================================================================
# 12. ⇅ 批量与压实面板
# ===================================================================

class TxnPanel(BaseQueryPanel):
    """批（`批量 开始` / `批量 提交`）与 `压实`。

    **`开始` / `提交` 是 `批量 开始` / `批量 提交` 的简写，落到引擎里是
    同一条语句**——`sql.py` 里两个写法返回的都是同一个 `BatchBegin` /
    `BatchCommit`，字段也一样（这个我核过）。所以这儿只摆**一对**按钮，
    简写写在说明里：摆两对会让人以为它们是两件事，而它们不是。

    **批不是事务，本库没有回滚。** `syntax.py` 的原文是"批里没有回滚。
    前面几条成了就是成了——这不是事务，只是把'N 次落盘'压成'一次落盘'"。
    这条必须说在明处：一边省着这个钱一边假装有那个保证，是最坏的。

    **两样都不是"某张表"的事**：批是全局的（`_in_batch` 在语法层上，跟
    哪张表无关），压实也是整库一趟。所以这一页没有选库选表。
    """

    def _build_ui(self):
        布局 = QVBoxLayout(self)
        布局.setContentsMargins(8, 4, 8, 4); 布局.setSpacing(10)

        self._横幅 = QLabel("当前不在批里")
        self._横幅.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #16A34A; "
            "background: #F0FDF4; padding: 6px; border-radius: 4px;")
        布局.addWidget(self._横幅)

        说明 = QLabel(
            "**批不是事务。本库没有回滚**——前面成了的就是成了，批只是把 N 次\n"
            "落盘压成一次落盘。\n"
            "`开始` / `提交` 是 `批量 开始` / `批量 提交` 的**简写，同一条语句**。\n"
            "批里不能跑 `压实`，也不能关库——收尾只有一条路：`批量 提交`。")
        说明.setStyleSheet("color: #64748B; font-size: 11px;")
        说明.setWordWrap(True)
        布局.addWidget(说明)

        行 = QHBoxLayout(); 行.setSpacing(8)
        开 = QPushButton("批量 开始"); 开.setFixedWidth(120)
        开.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #F59E0B; border-radius: 4px; padding: 6px 10px; }")
        开.clicked.connect(self._开始)
        行.addWidget(开)
        交 = QPushButton("批量 提交"); 交.setFixedWidth(120)
        交.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #16A34A; border-radius: 4px; padding: 6px 10px; }")
        交.clicked.connect(self._提交)
        行.addWidget(交); 行.addStretch()
        布局.addLayout(行)

        布局.addWidget(QFrame(frameShape=QFrame.HLine))

        维护 = QLabel("整库维护")
        维护.setStyleSheet("font-weight: bold; padding-top: 4px;")
        布局.addWidget(维护)
        压说明 = QLabel(
            "`压实`：整库一趟，把被覆盖的旧版本占的区真正从盘上回收掉。\n"
            "它会**重写盘上的区、还要删文件**，耗时长，期间别关窗口。\n"
            "不丢数据（只回收旧版本），但**不能中途打断**。")
        压说明.setStyleSheet("color: #64748B; font-size: 11px;")
        压说明.setWordWrap(True)
        布局.addWidget(压说明)

        self._压实按钮 = QPushButton("压实（整库一趟）")
        self._压实按钮.setFixedWidth(160)
        self._压实按钮.setStyleSheet(
            "QPushButton { font-weight: bold; color: #FFF; "
            "background-color: #DC2626; border-radius: 4px; padding: 6px 10px; }")
        self._压实按钮.clicked.connect(self._压实)
        行2 = QHBoxLayout(); 行2.addWidget(self._压实按钮); 行2.addStretch()
        布局.addLayout(行2)
        布局.addStretch()

    def 批状态(self, 在批里: bool) -> None:
        """由 `QueryEditor._收结果` / `_收多结果` 通知。

        **这个状态是界面自己记的，不是引擎报的**——引擎没有"我是不是在批里"
        这条语句。规则简单，而且成没成都一样：
          * 发出 `批量 开始` -> 在批里（成了是刚进，没成是本来就在）；
          * 发出 `批量 提交` -> 不在批里（成了是刚提交，没成是压根没在批里）。
        """
        if 在批里:
            self._横幅.setText(
                "⚠ 正在批里：这批写还没落盘。必须 `批量 提交` 才能收尾"
                "——批里不能跑 `压实`，也不能关库（引擎会拒）。")
            self._横幅.setStyleSheet(
                "font-size: 13px; font-weight: bold; color: #B45309; "
                "background: #FFFBEB; padding: 6px; border-radius: 4px;")
        else:
            self._横幅.setText("当前不在批里")
            self._横幅.setStyleSheet(
                "font-size: 13px; font-weight: bold; color: #16A34A; "
                "background: #F0FDF4; padding: 6px; border-radius: 4px;")
        self._压实按钮.setEnabled(not 在批里)
        self._压实按钮.setToolTip(
            "批模式的意思是「先别落盘」，而压实要重写盘上的区、还要删文件，"
            "引擎会拒。" if 在批里 else "")

    def _开始(self):
        if QMessageBox.question(
                self, "开始批模式",
                "批模式是**全局**的：这期间所有页发出的写都不会立刻落盘，\n"
                "直到你点 `批量 提交`。\n\n"
                "本库没有回滚——已经成了的就是成了。\n"
                "批里不能跑 `压实`，也不能关库。\n\n开始吗？") == QMessageBox.Yes:
            self.sql_generated.emit("批量 开始;")

    def _提交(self):
        # 出来这个方向不给阻力：不弹确认。
        self.sql_generated.emit("批量 提交;")

    def _压实(self):
        if QMessageBox.question(
                self, "压实",
                "压实会重写盘上的区、删掉旧区文件。\n"
                "不丢数据（只回收被覆盖的旧版本），但**不能中途打断**，\n"
                "耗时按库的大小走。\n\n确定现在压吗？") == QMessageBox.Yes:
            self.sql_generated.emit("压实;")

    def build_sql(self) -> str:
        return ""      # 全靠三个按钮直接 emit


#: 模式条。**key 与 `QueryBuilder._面板类` 必须一一对上**——对不上那里是
#: `KeyError` 当场炸，不会静默少一页。
#:
#: 按"看 / 改 / 结构 / 整库"分组，不是把新加的堆在尾巴上。手写框排第一
#: 且默认选中：它是**唯一 18 条形态全通**的入口，摆在最前面是有意的。
_QUERY_MODES = [
    ("RAW", "✎ 手写语句"),
    ("SELECT", "🔍 查询"),
    ("JOIN", "🔗 联表查询"),
    # 原先叫「⚡ 索引查询」——它发的是普通的 `查询` 语句，用不用索引不归
    # 这个面板管，那名字在暗示一条不存在的捷径。跟着面板里那几处一起改成
    # 实话。
    ("INDEX", "🔎 快速查询"),
    ("SHOW", "☰ 显示"),
    ("IDXOP", "🗂 索引管理"),
    ("ALTER", "🔧 改表"),
    ("INSERT", "✚ 插入数据"),
    ("UPDATE", "✎ 更新数据"),
    ("DELETE", "✕ 删除数据"),
    ("DDL", "⚙ 表结构"),
    ("TXN", "⇅ 批量与压实"),
]


class QueryBuilder(QWidget):
    """多模式可视化查询构建器"""

    #: 表单面板拼出**一条**语句 -> 从这儿出。
    sql_generated = Signal(str)
    #: 手写框那**一整段文本** -> 从这儿出。**必须单独一条信号**：那一段里
    #: 可能有好几条语句，回话是一**串**结果而不是一个。两条信号合起来的话，
    #: `QueryEditor` 就分不清该按单个结果收还是按一串结果收了。
    sql_batch = Signal(str)
    #: 某个面板自己动了库 -> 转给 `QueryEditor` 去重拉目录。
    数据变了 = Signal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._setup_ui()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(2)

        # 模式切换 Tab
        mode_bar = QHBoxLayout()
        mode_bar.setSpacing(2)
        self._mode_group = QButtonGroup(self)
        self._mode_btns = {}
        for i, (key, label) in enumerate(_QUERY_MODES):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.setFlat(True)
            btn.clicked.connect(lambda checked, k=key: self._switch_mode(k))
            btn.setStyleSheet(
                "QPushButton { padding: 6px 14px; border: 1px solid #D1D5DB; "
                "border-radius: 4px; font-weight: bold; font-size: 12px; }"
                "QPushButton:checked { color: #FFF; background-color: #2563EB; "
                "border-color: #2563EB; }"
                "QPushButton:!checked { color: #64748B; background: #F9FAFB; }"
                "QPushButton:!checked:hover { background: #F3F4F6; }")
            self._mode_group.addButton(btn, i)
            self._mode_btns[key] = btn
            mode_bar.addWidget(btn)
        mode_bar.addStretch()

        # 12 个按钮一行摆不下（窗口默认宽 1200）。裹一层横向滚动：
        # **外观一个字不变**——不动 `QButtonGroup`、不动样式表、不动顺序；
        # 只有窗口窄到装不下时才冒出一条滚动条，而不是把按钮切掉。
        # `QPushButton` 的横向 size 策略是 `Minimum`（压不到 sizeHint 以下），
        # 所以宿主的 `minimumSizeHint` 就是按钮总宽，该出滚动条就出。
        宿主 = QWidget()
        宿主.setLayout(mode_bar)
        条 = QScrollArea()
        条.setWidget(宿主); 条.setWidgetResizable(True)
        条.setFrameShape(QFrame.NoFrame)
        条.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        条.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        条.setFixedHeight(46)
        main_layout.addWidget(条)

        # 模式 key -> 面板类。**写成一张表，不写 if/elif 链。** 原先那条链
        # 最后挂着一支 `else: # DDL` 兜底——将来加一个新模式、忘了补分支，
        # 它会**静默**塞给用户一个 `DDLPanel`：按钮上写着"事务"、点开是
        # "表结构"，不报错、也没有任何提示。查不到就当场跳过，绝不兜底。
        _面板类 = {
            "RAW": RawSqlPanel,
            "SELECT": SelectPanel,
            "JOIN": JoinPanel,
            "INDEX": IndexPanel,
            "SHOW": ShowPanel,
            "IDXOP": IndexOpsPanel,
            "ALTER": AlterPanel,
            "INSERT": InsertPanel,
            "UPDATE": UpdatePanel,
            "DELETE": DeletePanel,
            "DDL": DDLPanel,
            "TXN": TxnPanel,
        }

        # Stacked 面板
        self._stack = QStackedWidget()
        self._panels: dict[str, BaseQueryPanel] = {}
        for key, label in _QUERY_MODES:
            类 = _面板类.get(key)
            if 类 is None:
                continue
            panel = 类(self.engine)
            if isinstance(panel, RawSqlPanel):
                panel.sql_generated.connect(self.sql_batch.emit)
            else:
                panel.sql_generated.connect(self.sql_generated.emit)
                panel.数据变了.connect(self.数据变了.emit)
            # `BaseQueryPanel` 那一系的界面是**懒建**的（`_ensure_built`）；
            # 手写框不是那一系，没这一步。
            if hasattr(panel, '_ensure_built'):
                panel._ensure_built()
            self._panels[key] = panel
            self._stack.addWidget(panel)
        main_layout.addWidget(self._stack)

    def _switch_mode(self, key: str):
        if key in self._panels:
            self._stack.setCurrentWidget(self._panels[key])

    def refresh_db_list(self):
        for panel in self._panels.values():
            if hasattr(panel, '_refresh_db_list'):
                panel._refresh_db_list()

    def 批状态(self, 在批里: bool) -> None:
        """把"在不在批里"转给批量面板。**没有那个面板就静默跳过。**

        批是**全局**的：`_in_batch` 挂在语法层上，跟哪张表、哪个页面都无关。
        所以这条状态由编辑器统一转发，不管语句是从手写框敲的还是从批量页
        按钮点出来的。
        """
        p = self._panels.get("TXN")
        if p is not None:
            p.批状态(在批里)


# ===================================================================
# SQL 查询编辑器（主面板）
# ===================================================================

class QueryEditor(QWidget):
    """数据库查询 — 纯表单操作，下拉框选字段即查，无需手写SQL。

    支持两种模式:
      - 本地模式: 使用 Engine 的 SQL 解析/规划/执行管道
      - 远程模式: 使用 MySQLClient 直接发送 SQL 到服务器

    霸下这边只有`本地`这一条腿：`engine` 是 `界面_接驳.引擎`。`client`
    形参按参照项目的签名原样留着（主窗那边 `panel_cls(client=backend)`
    那个位置参数不用改），但**永远是 None**。
    """

    def __init__(self, engine=None, client=None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._client = client
        self._起表 = 0.0            # 投出去的时刻，回来时算一趟花了多久
        self._上次语句 = ""          # 上一次发出去的语句原文
        self._上次结果 = None        # 上一次回来的 `Result`（导出用它）
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        # ====== 模式面板 ======
        self._builder = QueryBuilder(self.engine)
        self._builder.sql_generated.connect(self._on_execute_request)
        # 手写框那条路**单独接一条线**：它发出来的是一整段文本（里面可能
        # 有好几条语句），表单面板发的是拼好的**一条**。合到一条线上的话，
        # 这里就分不清手里这串该不该当成一段文本整体投下去。
        self._builder.sql_batch.connect(self._on_execute_batch)
        # 面板自己动了库（现在只有「改表」那一页：它把活交给表结构编辑器，
        # 不经过上面两条语句线）-> 重拉目录，新列才会进别的页的下拉框。
        self._builder.数据变了.connect(self.refresh)
        layout.addWidget(self._builder, 0)  # 不拉伸

        # ====== 多条语句的逐条清单 ======
        # **只有一次跑了多条时才现身。** 底下那张结果表只放得下**一份**表头，
        # 而 N 条语句的列各不相同——`建表` 没有结果集、`查询` 有。所以表里
        # 放最后那条有结果集的，这张清单把**每一条**的去向逐条写出来。
        # 「这段文本到底跑了几条、哪条坏了」不能靠人数。
        self._多结果清单 = QPlainTextEdit()
        self._多结果清单.setReadOnly(True)
        self._多结果清单.setFont(QFont("Consolas", 10))
        self._多结果清单.setMaximumHeight(92)   # 有上限，别把结果表挤没
        self._多结果清单.setVisible(False)
        layout.addWidget(self._多结果清单, 0)

        # ====== 结果区 ======
        result_frame = QFrame()
        result_frame.setFrameShape(QFrame.StyledPanel)
        rl = QVBoxLayout(result_frame)
        rl.setContentsMargins(4, 4, 4, 4)
        rl.setSpacing(4)

        result_header = QHBoxLayout()
        self._status_label = QLabel("就绪 — 选择表、字段、输入值，点击查询")
        self._status_label.setStyleSheet("color: #64748B; font-size: 12px;")
        result_header.addWidget(self._status_label, 1)

        clear_btn = QPushButton("清空结果")
        clear_btn.setFixedWidth(70)
        clear_btn.clicked.connect(self._on_clear)
        result_header.addWidget(clear_btn)

        export_btn = QPushButton("导出CSV")
        export_btn.setFixedWidth(70)
        export_btn.clicked.connect(self._on_export_csv)
        result_header.addWidget(export_btn)

        rl.addLayout(result_header)

        # 共用控件（`界面零件.ResultTable`）：字典行和按位置排的列表行都收，
        # 霸下的 `结果.as_lists()` 给的是后者。
        self._result_table = ResultTable()
        rl.addWidget(self._result_table, 1)

        layout.addWidget(result_frame, 1)

        # 头一屏得把库表列表填上。参照项目是建 UI 时**同步**读目录，
        # 霸下读的是缓存，而缓存要先去拉——所以这儿投一把，刷完再填。
        # **投完就返回**，不在这儿等。
        self.refresh()

    # ── 执行入口：所有面板操作都汇聚到这里 ──────────────

    def _on_execute_request(self, sql: str):
        """接收面板生成的语句，**投给工人去跑**（非阻塞）。

        取数的走 `引擎.取`，改动的走 `引擎.发`；两条底下是同一次调用，
        分的是记账。回话在主线程里被叫，见 `_收结果`。
        """
        if not sql or not sql.strip():
            return

        # 远程模式：使用 MySQLClient
        # （霸下没有第二条腿，`self._client` 永远是 None，这段走不到。
        #  留着是为了主窗那行 `panel_cls(client=backend)` 不用改。）
        if self._client is not None:
            self._execute_remote(sql)
            return

        import time
        self._起表 = time.perf_counter()
        self._上次语句 = sql

        if _是取数语句(sql):
            self.engine.取(sql, self._收结果)
        else:
            self.engine.发(sql, self._收结果)

    def _on_execute_batch(self, 文本: str):
        """手写框那条路：**整段文本一起投**，拿回**每条语句各一个**结果。

        **为什么不在界面上按 `;` 自己拆**：分号归谁管是词法的事，不是字符串
        `split` 的事。字符串字面量里完全可能有分号（`查询 * 从 t 其中 正文
        像 '%;%'`），在界面拆是**静默出错**——拆错了不报错，只会把一条语句
        劈成两半各跑一次，而两半都是"合法"语句时连错误文本都没有。引擎的
        `parse_all` 本来就认得边界，`execute_batch`（`syntax.py:1080`）对
        一段文本里的**每条语句各产一个 `Result`**，`发多` 原样递回来。

        **走 `发多` 不走 `取多`**：两条底下是同一次调用（`io_layer.py:545`
        和 `638` 都落到 `self._leg.many`），分的是记账；而这段文本里取数和
        改动可以是**混着**的，用 `取多` 会把改动的那几条记到"出"那边去。
        """
        if not 文本 or not 文本.strip():
            return

        # 远程模式那条腿（`self._client` 永远是 None，见 `_on_execute_request`）
        if self._client is not None:
            self._execute_remote(文本)
            return

        import time
        self._起表 = time.perf_counter()
        self._上次语句 = 文本
        self.engine.发多([文本], self._收多结果)

    def _收多结果(self, 结果们, 错):
        """手写框的回话。**在主线程里被叫。**

        `结果们` 与文本里的语句**一一对应、按序排列**。单条时长度为 1，
        表现跟表单那条路一致。
        """
        import time
        耗时 = (time.perf_counter() - self._起表) * 1000

        if 错 is not None:
            self._多结果清单.setVisible(False)
            self._status_label.setStyleSheet("color: #DC2626; font-size: 12px;")
            self._status_label.setText(f"✗ {错}")
            return

        结果们 = list(结果们 or [])
        if not 结果们:
            self._多结果清单.setVisible(False)
            self._status_label.setStyleSheet("color: #DC2626; font-size: 12px;")
            self._status_label.setText("✗ 没有语句")
            return

        # **只有一条时原样交给表单那条路。** 单句是手写框最常见的用法，
        # 不能因为它从哪个框出去就换一套说法——成功那句「✓ 完成 — N 行」、
        # 失败那句「✗ 中文原文」、以及"失败了结果表不动"，全都跟表单那条
        # 路一字不差，省得两处各写一份、日后改一处漏一处。
        if len(结果们) == 1:
            self._多结果清单.setVisible(False)
            self._收结果(结果们[0], None)
            return

        # 结果表只放得下**一份**表头，所以挑**最后一条有结果集**的填进去。
        # 倒着找是有道理的：人敲 `建表…; 插入…; 查询…` 时想看的是最后那条
        # 查询；而中间那些没有结果集的（`插入`/`批量`/`压实`）填进去只会
        # 是一张空表。
        展示 = None
        for r in reversed(结果们):
            if r.ok and r.columns:
                展示 = r
                break
        if 展示 is None:
            展示 = 结果们[-1]
        self._display_exec_result(展示)
        self._上次结果 = 展示

        self._写多结果清单(结果们)
        self._报批状态()

        好了 = sum(1 for r in 结果们 if r.ok)
        坏了 = len(结果们) - 好了
        行数 = sum(len(r.rows) for r in 结果们 if r.ok)
        影响 = sum(r.affected for r in 结果们 if r.ok)

        # **跑了几条摆在眼前。** 一段文本里几条语句、成了几条、坏了几个，
        # 看一眼就知道，不用去数。有一条坏了就不打绿勾——勾是"整段都成"。
        摘要 = "%s 跑了 %d 条" % ("✓" if not 坏了 else "✗", len(结果们))
        if 坏了:
            摘要 += f"，其中 {坏了} 条出错"
        if 行数:
            摘要 += f"，共 {行数} 行"
        if 影响:
            摘要 += f"，影响 {影响} 行"

        self._status_label.setStyleSheet(
            "color: %s; font-size: 12px;" % ("#DC2626" if 坏了 else "#16A34A"))
        self._status_label.setText(f"{摘要} ({耗时:.1f}ms)")

        # **手写这条路上一律重拉目录。** 手敲的文本可以是任何东西（`建表`/
        # `改表`/`删索引` 混在取数语句里都正常），挨条判"是不是取数"要多拿
        # 一份单句原文，而那个原文得在界面重新切一遍语句——切歪了就是静默
        # 出错。与其冒着切歪的风险省一次目录刷新，不如老实刷。
        self.refresh()

        self._notify_data_changed()

    def _写多结果清单(self, 结果们) -> None:
        """把每一条语句的去向逐条写出来。

        **只有一条时不显示**——一条的话状态栏已经说清楚了，多一块东西只是
        碍事。

        **列的是"第几条、回来了什么"，不是"第几条说了什么"。** 引擎给回的
        `Result` 里只有整段文本（`statement` 是**整段**，不是单句），要在这儿
        补上单句原文，就得在界面按分号重新切一遍语句——而按分号切是静默出错
        的源头（字符串字面量里可能有分号）。序号与文本里的语句**自上而下
        一一对应**，对着上面的框数就数得出来。
        """
        if len(结果们) <= 1:
            self._多结果清单.setVisible(False)
            return

        行 = []
        for i, r in enumerate(结果们, 1):
            if not r.ok:
                行.append(f"第 {i} 条  ✗  {r.error}")
            elif r.columns:
                列 = ", ".join(名 for 名, _ in r.columns)
                if len(列) > 56:
                    列 = 列[:56] + "…"
                行.append(f"第 {i} 条  ✓  {len(r.rows)} 行（{列}）")
            elif r.affected:
                行.append(f"第 {i} 条  ✓  影响 {r.affected} 行")
            else:
                行.append(f"第 {i} 条  ✓  完成")
        self._多结果清单.setPlainText("\n".join(行))
        self._多结果清单.setVisible(True)

    def _收结果(self, 结果, 错):
        """工人回话了。**在主线程里被叫。**

        `错` 是"活本身炸了"（连不上、参数个数不对这类）。**语句写错不走
        这里**——那时候 `结果.ok` 是 False，`结果.error` 里是中文错误文本，
        照原样红字显示，一个字不加工。
        """
        import time
        耗时 = (time.perf_counter() - self._起表) * 1000

        if 错 is not None:
            self._status_label.setStyleSheet("color: #DC2626; font-size: 12px;")
            self._status_label.setText(f"✗ {错}")
            return

        # 批状态**不看成功失败**：`批量 开始` 之后就是在批里（成了是刚进，
        # 没成是本来就在），`批量 提交` 之后就是不在。所以放在错误那条
        # 早返回**前面**。
        self._报批状态()

        if not 结果.ok:
            # **错误本来就是中文，原样显示。** 这儿不碰结果表——参照项目那边
            # 语法错也是在 `Executor` 里就抛了，结果表根本没被清。
            self._status_label.setStyleSheet("color: #DC2626; font-size: 12px;")
            self._status_label.setText(f"✗ {结果.error}")
            return

        self._display_exec_result(结果)
        self._上次结果 = 结果

        if 结果.columns:
            label = f"✓ 完成 — {len(结果.rows)} 行"
            if 结果.affected > 0:
                label += f", 影响 {结果.affected} 行"
        else:
            label = f"✓ {结果}"
        self._status_label.setStyleSheet("color: #16A34A; font-size: 12px;")
        self._status_label.setText(f"{label} ({耗时:.1f}ms)")

        # 改动过就要把目录重拉一遍（新表/新列得进下拉框）。查询不用——
        # 那是白跑一趟。
        if not _是取数语句(self._上次语句):
            self.refresh()

        # DDL/DML 执行后通知主窗口刷新面板
        self._notify_data_changed()

    def _报批状态(self) -> None:
        """这条语句要是 `批量 开始` / `批量 提交`（含 `开始` / `提交` 简写），
        把批量页那条横幅换掉。判据见模块级的 `_批动向`。"""
        动向 = _批动向(self._上次语句)
        if 动向 is not None:
            self._builder.批状态(动向)

    def _execute_remote(self, sql: str) -> None:
        """远程模式：通过 MySQLClient 执行 SQL。

        **霸下走不到这里**：`client` 形参留着是为了主窗那行
        `panel_cls(client=backend)` 不改，但霸下只有一个内核、没有第二条腿，
        主窗永远按本地那条传参。这一段原样保留，别当它是活的。
        """
        import time
        t0 = time.perf_counter()

        try:
            result = self._client.query(sql)
            elapsed = (time.perf_counter() - t0) * 1000

            if result.get("error"):
                self._status_label.setStyleSheet("color: #DC2626; font-size: 12px;")
                self._status_label.setText(f"✗ {result['error']}")
                return

            if result.get("is_query"):
                # 结果集
                self._display_remote_result(result)
                self._status_label.setStyleSheet("color: #16A34A; font-size: 12px;")
                self._status_label.setText(
                    f"✓ {len(result.get('rows', []))} 行 ({elapsed:.1f}ms)"
                )
            else:
                # OK/受影响行
                self._result_table.clear()
                self._result_table.setColumnCount(1)
                self._result_table.setHorizontalHeaderLabels(["消息"])
                self._result_table.setRowCount(1)
                self._result_table.setItem(
                    0, 0, QTableWidgetItem(result.get("message", "OK"))
                )
                self._status_label.setStyleSheet("color: #16A34A; font-size: 12px;")
                self._status_label.setText(
                    f"✓ {result.get('message', 'OK')} ({elapsed:.1f}ms)"
                )

        except Exception as e:
            self._status_label.setStyleSheet("color: #DC2626; font-size: 12px;")
            self._status_label.setText(f"✗ 错误: {e}")

    def _display_remote_result(self, result: dict) -> None:
        """显示远程查询结果集。（霸下走不到，见 `_execute_remote`。）"""
        self._result_table.clear()
        columns = result.get("columns", [])
        rows = result.get("rows", [])
        if columns:
            self._result_table.setColumnCount(len(columns))
            self._result_table.setHorizontalHeaderLabels(columns)
            self._result_table.setRowCount(max(len(rows), 1))
            for r, row in enumerate(rows):
                if isinstance(row, dict):
                    for c, cn in enumerate(columns):
                        v = row.get(cn)
                        self._result_table.setItem(r, c, QTableWidgetItem(
                            str(v) if v is not None else "NULL"))
                else:
                    for c, v in enumerate(row):
                        self._result_table.setItem(r, c, QTableWidgetItem(
                            str(v) if v is not None else "NULL"))
        elif rows:
            self._result_table.setColumnCount(1)
            self._result_table.setHorizontalHeaderLabels(["(无列名)"])
            self._result_table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                val = str(row) if row else "NULL"
                self._result_table.setItem(r, 0, QTableWidgetItem(val))

    # ── 结果显示 ──────────────────────────────────────

    def _display_exec_result(self, result) -> None:
        """把霸下的 `Result` 铺到表格上。

        **填表用 `as_lists()`**：那一份是渲染成人看的文本（空是 None，
        二进制仍是 bytes），正好是表格要的。
        `rows` 那一份是带类型号的**真值**——排序、比大小、导出要用它，
        别拿这一份去算。
        """
        if result is None:
            return
        if result.columns:
            self._result_table.display_result(
                [名 for 名, _类型号 in result.columns], result.as_lists())
        else:
            # 没有结果集（建表/删表/插入/更新/删除这类）：一行话，表头
            # 照参照项目那个 `消息`。用 `str(结果)` 当正文——那是引擎自己
            # 给的说法（`影响 N 行`），不另编一句。
            self._result_table.clear()
            self._result_table.setColumnCount(1)
            self._result_table.setHorizontalHeaderLabels(["消息"])
            self._result_table.setRowCount(1)
            self._result_table.setItem(0, 0, QTableWidgetItem(str(result)))

    def _on_clear(self) -> None:
        self._result_table.clear()
        self._result_table.setRowCount(0)
        self._result_table.setColumnCount(0)
        self._status_label.setText("就绪")
        self._status_label.setStyleSheet("color: #64748B; font-size: 12px;")

    def _on_export_csv(self) -> None:
        """把上一次的结果导成 CSV。

        **从 `结果` 对象导，不从表格控件里抄。** 控件里二进制已经被显示成
        `<12 字节>` 了，那是给人看的；写进 CSV 得是 `0x` 加十六进制，不然
        导出去的东西谁也读不回去。空值写 `NULL`，跟表格上显示的一个样。
        """
        结果 = self._上次结果
        if 结果 is None or not 结果.columns:
            QMessageBox.information(self, "提示", "没有数据可导出")
            return
        import os, csv
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "导出结果", os.getcwd(), "CSV 文件 (*.csv)")
        if not path:
            return
        # 霸下没有 `baxia.utils.encoding.safe_write_text`：写盘就是按 UTF-8
        # 写、带 BOM（Excel 认这个）——参照项目那个 `bom=True` 就是这个意思。
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([名 for 名, _类型号 in 结果.columns])
            for 行 in 结果.as_lists():
                w.writerow([_单元格文本(值) for 值 in 行])
        self._status_label.setText(f"已导出到: {os.path.basename(path)}")
        self._status_label.setStyleSheet("color: #16A34A; font-size: 12px;")

    def refresh(self) -> None:
        """重拉一遍库表列表（各面板的下拉框都从 `引擎.catalog` 那份缓存读）。

        **投完就返回。** 目录刷新是投给工人去干的，回话在主线程里被叫，
        那时候才去动下拉框。这里不 `processEvents`、也不循环等结果——
        那样会把窗口冻住，冻多久取决于库有多大。编辑器里的语句一个字不动。
        """
        if self.engine is None:
            return
        # **投出去之后不能立刻接着读目录**，要在回话里读。
        self.engine.catalog.刷新(self._目录刷完)

    def _目录刷完(self, 错) -> None:
        if 错 is not None:
            self._status_label.setStyleSheet("color: #DC2626; font-size: 12px;")
            self._status_label.setText(f"✗ {错}")
            return
        self._builder.refresh_db_list()

    def _notify_data_changed(self) -> None:
        """DDL/DML 执行后通知主窗口刷新其他面板。"""
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, 'data_changed'):
                try:
                    parent.data_changed.emit()
                except Exception:
                    pass
                break
            parent = parent.parent()


def _单元格文本(值):
    """一格值 -> CSV 里的一个字段。

    **二进制写成 `0x` 加十六进制**，不当文本塞（塞进去的是控制字符，
    连读它的程序都可能被搞坏）。空值写 `NULL`，和表格上一致。
    """
    if 值 is None:
        return "NULL"
    if isinstance(值, bytes):
        return "0x" + 值.hex()
    return str(值)
