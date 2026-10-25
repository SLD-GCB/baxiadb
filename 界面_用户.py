#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面_用户.py — 用户与权限面板（第 4 页）+ 开库时的登录框

参照项目 `baxiadb-v1.6/config_gui/ui/user_manager.py` **有一页现成的**，
形状（标题 + 一排按钮 + 表格）是跟它借的。**底下的东西一行都没抄**——
那一页有三处是错的，抄过来就是把错的抄进来：

    参照项目那边                          为什么不能抄
    ────────────────────────────────      ──────────────────────────────
    check_table_privilege(...) 收了        表级权限是空壳——而"精细"要的
      table 参数一个字没用，注释里写着       恰恰就是这一块
      "delegates to global check for now"
    认不出的操作一律放行（fail-open）       `压实`/`批量`/`重建索引` 这几样
                                          在它的表里根本没有，全默认放行
    口令明文与哈希并排存，默认口令是空串     空口令 + 明文

**这一页自己不判权限。** 它是一张会发 SQL 的表单：读走 `显示 用户`，
写走 `建用户` / `删用户` / `改密` / `授权` / `收回`。权限判在语法层
（`syntax._run`），这里再判一次就是两处会分岔的规矩。所以下面那些
"按钮该不该灰"纯粹是**界面上的省事**——真正的关口在对面。

一条工人线程独占闸口
====================

面板**不许同步取数**（见 `界面_接驳` 文件头那条铁律）。参照项目那一页
第 196 行写的是 `self.engine.user_manager.list_users()` —— **同步阻塞**，
那一行不能照抄。这里一律走 `引擎.取 / 取多 / 发 / 发多`，回话在**主线程**
里被叫。

登录为什么在窗口之前
====================

**因为没有身份的话，这个界面一页都动不了**：开库之后第一条 `显示 表列表`
就会被拒，用户看到的是一片"没有这张表"，而真正的原因（还没登录）一个字
都没露出来。所以 `装身份` 排在 `开库` 里、**装工人线程之前**——那一刻主
线程合法地持有闸口，而且任何活都还没投出去。

进窗口之后再要换身份，**必须走 `工位.干(lambda 闸口: 闸口.登录(...), 回话)`**，
不能从主线程直接调（`界面_接驳` 第 65-67 行那句"这是形状，不是纪律"）。
这一版还没做那件东西，所以窗口里没有"切换用户"的入口。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import sql as _sql

# 权限词这份**封闭清单**从语法层借，不在这儿再抄一份：抄一份就是两处
# 会分岔——将来加了第 5 个表级权限词，界面这一份不会有，于是那个权限
# **在界面上授不出去**，而盘上的模型是认的。
#
# （`界面_查询.py` 已经在 import `sql` 了，同样是为了词表。）
表级权限 = _sql.表级权限
库级权限 = _sql.库级权限
权限全部 = _sql.权限全部

# 通配那一行在授权对话框里长什么样。`user.table_privs['*']` 的键就是它
# ——**不是表名**，所以对话框里得把它和真表名分开摆。
通配符 = '*'


# ══ 一、开库时的身份（窗口之前，主线程） ═══════════════════════════════


class 设管理员框(QDialog):
    """
    库还没初始化时弹的：**强制**用户名 + 非空口令。

    空口令在霸下这儿从一开始就挡住了（`user.建` 只拒空，见那里的说明）——
    而那正是参照项目 `user_manager.py:34` 的默认值。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('设置管理员 — 这个库还没有用户')
        self.setModal(True)
        self.setMinimumWidth(420)

        布局 = QVBoxLayout(self)
        说明 = QLabel(
            '这个库里一个用户都没有。\n\n'
            '**第一件事是设一个管理员**：他是唯一不受权限限制的账号，'
            '以后的人由他建。口令不能是空的。')
        说明.setWordWrap(True)
        说明.setStyleSheet('color: #64748B;')
        布局.addWidget(说明)

        表 = QFormLayout()
        self._名字 = QLineEdit()
        self._名字.setPlaceholderText('管理员用户名')
        self._口令 = QLineEdit()
        self._口令.setEchoMode(QLineEdit.Password)
        self._确认 = QLineEdit()
        self._确认.setEchoMode(QLineEdit.Password)
        表.addRow('用户名', self._名字)
        表.addRow('口令', self._口令)
        表.addRow('再输一遍', self._确认)
        布局.addLayout(表)

        self._错 = QLabel('')
        self._错.setStyleSheet('color: #DC2626;')
        self._错.setWordWrap(True)
        布局.addWidget(self._错)

        按钮 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        按钮.button(QDialogButtonBox.Ok).setText('建好它')
        按钮.button(QDialogButtonBox.Cancel).setText('不开了')
        按钮.accepted.connect(self.accept)
        按钮.rejected.connect(self.reject)
        布局.addWidget(按钮)

    def accept(self) -> None:
        """**校验在这儿，落盘不在这儿。** 这里只保证"填的东西自洽"，
        真正的拒绝（库里已经有用户了）由 `Syntax.设初始管理员` 说。"""
        if not self._名字.text().strip():
            self._错.setText('用户名不能是空的')
            return
        if not self._口令.text():
            self._错.setText('口令不能是空的——空口令等于没有口令')
            return
        if self._口令.text() != self._确认.text():
            self._错.setText('两遍口令不一样')
            return
        super().accept()

    def 取值(self):
        return self._名字.text().strip(), self._口令.text()


class 登录框(QDialog):
    """
    报身份那一框。**口令不对不关框**——关掉的话调用方只能把"口令错"和
    "人点了取消"当成同一件事，而这两件事下一步动作正好相反。
    """

    def __init__(self, 核对, 形态='本地', parent=None):
        """
        `核对(名字, 口令) -> 用户名 或 None`。**这是唯一碰闸口的地方**，
        由 `装身份` 传进来（那一头才是主线程合法持有闸口的时刻）。
        """
        super().__init__(parent)
        self._核对 = 核对
        self.setWindowTitle('登录 — 霸下DB%s'
                            % ('（远程）' if 形态 == '远程' else ''))
        self.setModal(True)
        self.setMinimumWidth(420)

        布局 = QVBoxLayout(self)
        说明 = QLabel('这个库有用户，先报身份。\n\n'
                      '**身份是服务端（本机就是内核自己）从会话里取的**，'
                      '在这儿报的是用户名和口令，不是"我是谁"这句话本身。')
        说明.setWordWrap(True)
        说明.setStyleSheet('color: #64748B;')
        布局.addWidget(说明)

        表 = QFormLayout()
        self._名字 = QLineEdit()
        self._名字.setPlaceholderText('用户名')
        self._口令 = QLineEdit()
        self._口令.setEchoMode(QLineEdit.Password)
        表.addRow('用户名', self._名字)
        表.addRow('口令', self._口令)
        布局.addLayout(表)

        self._错 = QLabel('')
        self._错.setStyleSheet('color: #DC2626;')
        self._错.setWordWrap(True)
        布局.addWidget(self._错)

        按钮 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        按钮.button(QDialogButtonBox.Ok).setText('登录')
        按钮.button(QDialogButtonBox.Cancel).setText('不开了')
        按钮.accepted.connect(self.accept)
        按钮.rejected.connect(self.reject)
        布局.addWidget(按钮)

        # 回车直接登录——这一框每天都在用，多点一下就是每天多一下。
        self._口令.returnPressed.connect(self.accept)

    def accept(self) -> None:
        try:
            名字 = self._核对(self._名字.text(), self._口令.text())
        except Exception as exc:                     # 连不上 / 库坏了
            self._错.setText('%s：%s' % (type(exc).__name__, exc))
            return
        if 名字 is None:
            # **"用户名不对"和"口令不对"说的是同一句话。** 分开说就等于
            # 告诉外面哪个用户名是存在的（见 `protocol.BAD_LOGIN`）。
            self._错.setText('用户名或口令不对')
            self._口令.clear()
            self._口令.setFocus()
            return
        super().accept()


def 装身份(内核, 形态='本地'):
    """
    开库之后、装工人线程之前，**在主线程里**报一次身份。

    成了返回用户名；人点了取消返回 `None`（调用方该把内核停掉再退出，
    不能接着装窗口——那会装出一个每句话都被拒的界面）。

    **远程那个形态只弹登录框。** 对面开成服务就说明它已经有用户了
    （没用户的库不许起服务，见 `龙龟之心.起服务`），所以"设管理员"
    那一支在远程这条路上**够都够不着**；就算够得着，管理员也只能在
    库所在的那台机器上设——那是个本地动作。
    """
    闸口 = 内核.进出

    def 核对(名字, 口令):
        return 闸口.登录(名字, 口令)

    if 形态 != '远程' and not 内核.有用户():
        while True:
            框 = 设管理员框()
            if not 框.exec():
                return None
            名字, 口令 = 框.取值()
            try:
                内核.设初始管理员(名字, 口令)
            except Exception as exc:
                QMessageBox.critical(
                    None, '设不了',
                    '这个管理员没设上：%s\n\n再试一次。' % exc)
                continue
            return 核对(名字, 口令) or 名字

    while True:
        框 = 登录框(核对, 形态)
        if not 框.exec():
            return None
        return 框._名字.text()


# ══ 二、授权对话框 ═════════════════════════════════════════════════════


class 授权框(QDialog):
    """
    给一个人勾权限：**上面一组库级勾，下面一张表 × 四样勾的网格。**

    勾完按"和原来比变了什么"发出 `授权` / `收回` 两种语句。**只发变了的
    那些**：全量重发一遍看着更整齐，但它会把"没勾到"变成"收回"——而
    `全部` 那种通配展开出来的 `table_privs['*']` 在界面上只是**一行**，
    全量重发的意思就变成"把通配换成逐表列出的"，用户没打算干这件事。
    """

    def __init__(self, engine, 用户名, 现状, parent=None):
        """
        `现状` 是 `(库级集合, {表名或'*': 权限集合})`，从 `显示 用户`
        那一行解析出来的（见 `_解析现状`）。
        """
        super().__init__(parent)
        self.engine = engine
        self.用户名 = 用户名
        self._原库级, self._原表级 = 现状
        self.setWindowTitle('授权 — %s' % 用户名)
        self.setModal(True)
        self.setMinimumWidth(560)

        布局 = QVBoxLayout(self)
        顶 = QLabel('给 **%s** 勾权限。勾上就是授，取消勾就是收回。' % 用户名)
        顶.setWordWrap(True)
        布局.addWidget(顶)

        # ── 库级那九样 ──
        库框 = QGroupBox('库级（不分表）')
        库格 = QGridLayout(库框)
        self._库勾 = {}
        for i, 词 in enumerate(库级权限):
            c = QCheckBox(词)
            c.setChecked(词 in self._原库级)
            库格.addWidget(c, i // 5, i % 5)
            self._库勾[词] = c
        布局.addWidget(库框)

        # ── 表级那张网 ──
        表框 = QGroupBox('表级（每张表四样）')
        表布 = QVBoxLayout(表框)
        提示 = QLabel('第一行 `%s` 是通配：勾上它等于所有表一起授。'
                      '它和逐表列出的那几行是**两回事**，可以同时存在。'
                      % 通配符)
        提示.setStyleSheet('color: #64748B;')
        提示.setWordWrap(True)
        表布.addWidget(提示)

        self._网 = QTableWidget()
        self._网.setColumnCount(1 + len(表级权限))
        self._网.setHorizontalHeaderLabels(['表'] + list(表级权限))
        self._网.verticalHeader().setVisible(False)
        self._网.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._网.setAlternatingRowColors(True)
        self._网.setEditTriggers(QTableWidget.NoEditTriggers)
        表布.addWidget(self._网)
        布局.addWidget(表框)

        self._表勾 = {}          # 表名 -> {权限词: QCheckBox 所在格的 item}
        self._装表()

        按钮 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        按钮.button(QDialogButtonBox.Ok).setText('就这么定')
        按钮.button(QDialogButtonBox.Cancel).setText('取消')
        按钮.accepted.connect(self.accept)
        按钮.rejected.connect(self.reject)
        布局.addWidget(按钮)

    def _装表(self):
        """
        网格里列哪些表：**通配那一行 + 目录缓存里看得见的那几张**。

        读的是 `引擎.catalog` 那份**内存缓存**（同步读，不卡），和
        `界面_浏览` 读表名是同一个来源。**不是"所有表"**：缓存里本来就
        只有这个人看得见的那些（`显示 表列表` 在服务端就按 `可读` 滤过）。
        看不见的表授不出去，这是对的——他连它存不存在都不该知道。
        """
        名字们 = [通配符]
        try:
            库 = self.engine.catalog.get_database(self.engine.current_database)
            名字们 += [n for n in 库.table_names if n != 通配符]
        except Exception:
            pass                               # 目录还没刷出来：只剩通配那一行

        # 现状里有、缓存里没有的（表被删了 / 权限是在别处授的），也列出来
        # ——不然那些权限在这张网里**看不见**，用户会以为它们不存在。
        for n in sorted(self._原表级):
            if n not in 名字们:
                名字们.append(n)

        self._网.setRowCount(len(名字们))
        for r, 名 in enumerate(名字们):
            格 = QTableWidgetItem(名)
            if 名 == 通配符:
                字 = 格.font()
                字.setBold(True)
                格.setFont(字)
                格.setForeground(Qt.darkBlue)
            self._网.setItem(r, 0, 格)
            有 = self._原表级.get(名, set())
            勾们 = {}
            for c, 词 in enumerate(表级权限):
                # 勾用**格子里那个 checkbox**（不是 `setCellWidget`）：
                # cellWidget 拿值要遍历整套控件，而这一张网要来回读好几遍。
                格 = QTableWidgetItem()
                格.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                格.setCheckState(Qt.Checked if 词 in 有 else Qt.Unchecked)
                格.setTextAlignment(Qt.AlignCenter)
                self._网.setItem(r, c + 1, 格)
                勾们[词] = 格
            self._表勾[名] = 勾们

    def 要发的语句(self):
        """把"变了什么"摊成一串 `授权`/`收回`。**没变的不发。**"""
        发 = []
        for 词, 勾 in self._库勾.items():
            现在 = 勾.isChecked()
            if 现在 == (词 in self._原库级):
                continue
            发.append('授权 `%s` %s' % (self.用户名, 词) if 现在
                      else '收回 `%s` %s' % (self.用户名, 词))
        for 名, 勾们 in self._表勾.items():
            有 = self._原表级.get(名, set())
            for 词, 格 in 勾们.items():
                现在 = (格.checkState() == Qt.Checked)
                if 现在 == (词 in 有):
                    continue
                表 = '' if 名 == 通配符 else ' `%s`' % 名
                发.append('授权 `%s` %s%s' % (self.用户名, 词, 表) if 现在
                          else '收回 `%s` %s%s' % (self.用户名, 词, 表))
        return 发


# ══ 三、用户面板 ═══════════════════════════════════════════════════════


class 用户面板(QWidget):
    """
    第 4 页。**和别的面板一样：公开的无参 `refresh()`，投活不阻塞。**

    主窗按 `panel_cls(引擎壳)` 造它，类名之外别的地方不用管。
    """

    列 = ('用户名', '管理员', '库级权限', '表级权限')

    def __init__(self, engine=None, client=None, parent=None):
        """
        签名和别的面板一样（`界面_浏览.py:162`），**但 `client` 这一页用不着**：
        另外那几页要在意"本地还是远程"（远程没有备份、没有就地修复这类
        区别），用户和权限在两条腿上是**同一套语句、同一套答复**——所以
        这里不存 `_is_remote`，也没有一处因为站在哪条腿上而分支。
        """
        super().__init__(parent)
        self.engine = engine
        self._现状 = {}          # 用户名 -> (库级集合, {表: 权限集合})
        self._顺序 = []          # 表格里第 i 行是哪个用户
        self._setup_ui()

    # ── 搭界面 ──────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        布局 = QVBoxLayout(self)

        顶 = QHBoxLayout()
        标题 = QLabel('用户管理')
        标题.setStyleSheet('font-size: 16px; font-weight: bold;')
        顶.addWidget(标题)
        顶.addStretch()
        self._当前 = QLabel('')
        self._当前.setStyleSheet('color: #64748B;')
        顶.addWidget(self._当前)
        刷新 = QPushButton('刷新')
        刷新.setToolTip('重新拉一遍用户清单 (F5)')
        刷新.setMinimumWidth(60)
        刷新.clicked.connect(self.refresh)
        顶.addWidget(刷新)
        布局.addLayout(顶)

        排 = QHBoxLayout()
        排.setSpacing(6)

        def 钮(文案, 提示, 色, 槽, 宽=100):
            b = QPushButton(文案)
            b.setToolTip(提示)
            b.setMinimumWidth(宽)
            if 色:
                b.setStyleSheet('QPushButton { font-weight: bold; color: %s; }' % 色)
            b.clicked.connect(槽)
            排.addWidget(b)
            return b

        self._建钮 = 钮('＋ 建用户', '新建一个用户', '#2563EB', self._on_create)
        self._删钮 = 钮('－ 删用户', '删掉选中的用户', '#DC2626', self._on_drop, 90)
        self._密钮 = 钮('🔑 改密', '给选中的用户换口令', None, self._on_password, 90)
        self._权钮 = 钮('🔐 授权', '给选中的用户勾权限', None, self._on_grant, 90)
        排.addStretch()
        布局.addLayout(排)

        self._表 = QTableWidget()
        self._表.setColumnCount(len(self.列))
        self._表.setHorizontalHeaderLabels(list(self.列))
        self._表.setAlternatingRowColors(True)
        self._表.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._表.setSelectionBehavior(QTableWidget.SelectRows)
        self._表.setSelectionMode(QTableWidget.SingleSelection)
        self._表.setEditTriggers(QTableWidget.NoEditTriggers)
        self._表.itemSelectionChanged.connect(self._按选中改钮)
        self._表.itemDoubleClicked.connect(lambda _i: self._on_grant())
        布局.addWidget(self._表, 1)

        self._状态 = QLabel('')
        self._状态.setWordWrap(True)
        布局.addWidget(self._状态)

        提示 = QLabel(
            '**权限分两层**：库级那几样不分表（建表/删表/压实/管用户…），'
            '表级是"哪张表 × 哪四样"（查/插/改/删）。\n'
            '管理员那行的权限不用授也收不掉——他本来就不受限。')
        提示.setStyleSheet('color: #64748B;')
        提示.setWordWrap(True)
        布局.addWidget(提示)

        # **这一页不挂 F5 / Ctrl+R。** 主窗那两条（`霸下之躯._setup_shortcuts`
        # 和「视图」菜单）已经是"把所有面板叫一遍"，而 `界面_浏览` 自己又挂
        # 了一条 F5——同一扇窗里两条同键的快捷键是歧义的，Qt 那边结果是
        # **一条都不触发**（`activatedAmbiguously`）。这里再挂第三条，既不
        # 能让它变好，也修不了那条旧账；不挂的话，焦点在这页时 F5 走的正是
        # 主窗那条全局刷新，本来就会叫到这里来。
        self.refresh()

    # ── 拉一遍 ──────────────────────────────────────────────────────

    def refresh(self) -> None:
        """
        公开、无参、不阻塞。别的面板什么形状，它就是什么形状。

        **"我是谁"顺手在这里写。** 这是整个界面上唯一说得出这句话的地方
        ——它读的是 `引擎.当前用户`，而那个值是开库时登录完填进去的
        （见 `霸下之躯.开库`）。它**只是给人看的**：权限一个字都不靠它
        （判据在对面服务端的会话里）。
        """
        谁 = getattr(self.engine, '当前用户', '') or '(没报身份)'
        self._当前.setText('当前登录：%s' % 谁)
        self._状态.setText('正在拉用户清单…')
        self._状态.setStyleSheet('color: #64748B;')
        self.engine.取('显示 用户', self._收用户)

    def _收用户(self, 结果, 错) -> None:
        """回话（**在主线程里被叫**）。"""
        if 错 is not None:
            self._报错(str(错))
            return
        if 结果 is None or not 结果.ok:
            self._报错(结果.error if 结果 is not None else '(没有回结果)')
            return

        self._现状 = {}
        self._顺序 = []
        self._表.setRowCount(len(结果.rows))
        for r, 行 in enumerate(结果.rows):
            格 = [self._格(v) for _t, v in 行]
            名 = 行[0][1]
            self._顺序.append(名)
            self._现状[名] = _解析现状(行[3][1], 行[2][1])
            for c, g in enumerate(格):
                self._表.setItem(r, c, g)

        self._状态.setText('共 %d 个用户。' % len(self._顺序))
        self._状态.setStyleSheet('color: #16A34A;')
        self._按选中改钮()

    def _格(self, 值) -> QTableWidgetItem:
        if 值 is None:
            项 = QTableWidgetItem('(无)')
            项.setForeground(Qt.gray)
            字 = QFont()
            字.setItalic(True)
            项.setFont(字)
            return 项
        return QTableWidgetItem(str(值))

    def _报错(self, 文本: str) -> None:
        """
        拉用户清单失败。

        **`显示 用户` 卡在哪儿由语法层说，这儿一个字都不补。**

        这儿原本补了一句"要 `管用户` 权限"——**那是错的**：`显示 用户` 上
        挂着两道关口（`Show` 先要库级 `显示`，`_show` 里再要 `管用户`），
        两句报错本来就不同（见 `syntax._无权文案`）。补一句"要 `管用户`"
        于是出现了这种话：

            无权 没有 显示 权限（`显示 用户` 要 `管用户` 权限）

        前后打架。**对面已经把缺的那样点名了**，这儿再猜一次，猜错的
        那一天就是这句话的样子。所以只端原文，加一句怎么办。
        """
        self._表.setRowCount(0)
        self._顺序 = []
        self._现状 = {}
        self._状态.setText(
            '拉不到用户清单：%s\n（用管理员登录，或者让管理员给你加上）' % 文本)
        self._状态.setStyleSheet('color: #DC2626;')
        self._按选中改钮()

    # ── 选中了谁 ────────────────────────────────────────────────────

    def _选中(self):
        """选中的用户名；没选或者这行已经没了就 `None`。"""
        r = self._表.currentRow()
        if r < 0 or r >= len(self._顺序):
            return None
        return self._顺序[r]

    def _是管理员(self, 名) -> bool:
        """
        **"管理员"这个标记不在 `显示 用户` 的那两列权限里**（管理员身上
        一个权限都没有，他是不受限），它是表格第 2 列那个 1/0。这里从
        表格里原样读回来，不自己推——推的话就是第二条判据。
        """
        r = self._表.currentRow()
        if r < 0 or r >= len(self._顺序) or self._顺序[r] != 名:
            return False
        格 = self._表.item(r, 1)
        return bool(格 is not None and 格.text() == '1')

    def _按选中改钮(self) -> None:
        """
        按选中的是谁，把几个按钮点亮或灰掉。

        **这是界面上的省事，不是关口。** 管理员不可删、不可授权那条规矩
        在语法层（`_drop_user` / `_改授权` 里各有一道），这里灰掉只是让人
        少走一趟冤枉路——就算这儿漏了，按下去也只会拿到一句人话。
        """
        名 = self._选中()
        有 = 名 is not None
        管 = 有 and self._是管理员(名)
        self._删钮.setEnabled(有 and not 管)
        self._权钮.setEnabled(有 and not 管)
        self._密钮.setEnabled(有)
        if 管:
            self._删钮.setToolTip('管理员不能删——删了之后这个库就没人能管用户了')
            self._权钮.setToolTip('管理员的权限授不了也收不掉——他本来就不受限')
        else:
            self._删钮.setToolTip('删掉选中的用户')
            self._权钮.setToolTip('给选中的用户勾权限')

    # ── 四件写操作 ──────────────────────────────────────────────────

    def _on_create(self) -> None:
        框 = QDialog(self)
        框.setWindowTitle('建用户')
        框.setModal(True)
        框.setMinimumWidth(380)
        布 = QVBoxLayout(框)
        表 = QFormLayout()
        名 = QLineEdit()
        名.setPlaceholderText('用户名')
        密 = QLineEdit()
        密.setEchoMode(QLineEdit.Password)
        表.addRow('用户名', 名)
        表.addRow('口令', 密)
        布.addLayout(表)
        错 = QLabel('')
        错.setStyleSheet('color: #DC2626;')
        错.setWordWrap(True)
        布.addWidget(错)
        钮 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        钮.button(QDialogButtonBox.Ok).setText('建')
        钮.button(QDialogButtonBox.Cancel).setText('取消')
        钮.accepted.connect(框.accept)
        钮.rejected.connect(框.reject)
        布.addWidget(钮)

        while 框.exec():
            n, p = 名.text().strip(), 密.text()
            if not n:
                错.setText('用户名不能是空的')
                continue
            if not p:
                错.setText('口令不能是空的——空口令等于没有口令（`建用户` 也不收）')
                continue
            # 口令直接拼成 `'…'`，单引号写成两个：这是**这门语言的字面量
            # 规矩**，不是在这儿自己发明转义（同 `界面_设计表.字面量`）。
            self.engine.发("建用户 `%s` '%s'" % (n, p.replace("'", "''")),
                           lambda 结果, 错_, n=n: self._收写('建用户', 结果, 错_, n))
            return

    def _on_drop(self) -> None:
        名 = self._选中()
        if 名 is None:
            return
        r = QMessageBox.question(
            self, '确认删除',
            '确定要删掉用户 %s 吗？\n\n'
            '他正在用的连接**不会当场断**——身份挂在会话上，删用户不会把'
            '已经在用的人踢出去。' % 名,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        self.engine.发('删用户 `%s`' % 名,
                       lambda 结果, 错: self._收写('删用户', 结果, 错, 名))

    def _on_password(self) -> None:
        名 = self._选中()
        if 名 is None:
            return
        框 = QDialog(self)
        框.setWindowTitle('改密 — %s' % 名)
        框.setModal(True)
        框.setMinimumWidth(380)
        布 = QVBoxLayout(框)
        表 = QFormLayout()
        密 = QLineEdit()
        密.setEchoMode(QLineEdit.Password)
        确 = QLineEdit()
        确.setEchoMode(QLineEdit.Password)
        表.addRow('新口令', 密)
        表.addRow('再输一遍', 确)
        布.addLayout(表)
        错 = QLabel('')
        错.setStyleSheet('color: #DC2626;')
        布.addWidget(错)
        钮 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        钮.button(QDialogButtonBox.Ok).setText('改')
        钮.button(QDialogButtonBox.Cancel).setText('取消')
        钮.accepted.connect(框.accept)
        钮.rejected.connect(框.reject)
        布.addWidget(钮)

        while 框.exec():
            if not 密.text():
                错.setText('口令不能是空的')
                continue
            if 密.text() != 确.text():
                错.setText('两遍口令不一样')
                continue
            self.engine.发(
                "改密 `%s` '%s'" % (名, 密.text().replace("'", "''")),
                lambda 结果, 错_, 名=名: self._收写('改密', 结果, 错_, 名))
            return

    def _on_grant(self) -> None:
        名 = self._选中()
        if 名 is None or 名 not in self._现状:
            return
        框 = 授权框(self.engine, 名, self._现状[名], self)
        if not 框.exec():
            return
        语句们 = 框.要发的语句()
        if not 语句们:
            self._状态.setText('权限和原来一模一样，没发语句。')
            return
        self.engine.发多(
            语句们,
            lambda 结果们, 错, 名=名: self._收权(结果们, 错, 名, 语句们))

    def _收权(self, 结果们, 错, 名, 语句们) -> None:
        """`发多` 回来的结果**和投出去的语句一一对位**（同 `界面_浏览`）。"""
        if 错 is not None:
            QMessageBox.critical(self, '授权失败', str(错))
            return
        结果们 = 结果们 or []
        坏 = []
        for i, 语句 in enumerate(语句们):
            r = 结果们[i] if i < len(结果们) else None
            if r is None or not r.ok:
                坏.append('%s → %s' % (语句, r.error if r is not None
                                       else '(没有回结果)'))
        if 坏:
            # **一条坏不代表全坏**：批里没有回滚（见 `Gate.send_many`），
            # 成功那几条是**真生效了**。所以报的是"这几条没成"，不是
            # "都没成"——说反了会让人重发一遍已经生效的授权。
            QMessageBox.warning(
                self, '部分没成',
                '这 %d 条没生效（其余 %d 条已经生效了）：\n\n%s'
                % (len(坏), len(语句们) - len(坏), '\n'.join(坏)))
        self.refresh()

    def _收写(self, 什么, 结果, 错, 名) -> None:
        if 错 is not None:
            QMessageBox.critical(self, '%s失败' % 什么, str(错))
            return
        if 结果 is None or not 结果.ok:
            QMessageBox.critical(
                self, '%s失败' % 什么,
                str(结果.error) if 结果 is not None else '(没有回结果)')
            return
        self._状态.setText('%s：%s 好了。' % (什么, 名))
        self._状态.setStyleSheet('color: #16A34A;')
        self.refresh()


# ══ 四、把 `显示 用户` 那两列读回来 ═══════════════════════════════════


def _解析现状(表级文本, 库级文本):
    """
    `显示 用户` 回来那两列 -> `(库级集合, {表名: 权限集合})`。

    格式是 `syntax._show` 里那句拼出来的：库级用 `/` 连，表级每张表
    `名: 权/权`，表与表之间用 `；`。**是照着那句话写的**，所以那句话要是
    改了，这儿也得跟着改——两处都留着这一条注释，好互相对上。

    **两头都要 `strip`。** `'%s: %s'` 那个冒号后面自带一个空格，不剥的话
    剥出来的是 `' 插'`——一个带前导空格的权限词。后果不是报错，是**授权
    对话框里那个勾没勾上**（`' 插' not in 已有`），于是用户看见一项自己
    明明有的权限是空的，一勾一存就把同一条权限又授了一遍。

    **名字里带 `；`、`:`、`/` 的表会解析歪。** 后果同样只是**对话框里那个
    勾的初始状态不对**，不是盘上的权限被改坏：勾错了、按下去发的是
    `授权`/`收回`，对面照发照判。所以这是一处"看得见、改得回来"的毛病，
    不是静默错数据——真碰上了把那张表的名字换掉。
    """
    库级 = set(w.strip() for w in (库级文本 or '').split('/') if w.strip())
    表级 = {}
    for 块 in (表级文本 or '').split('；'):
        if not 块.strip():
            continue
        名, _, 权 = 块.partition(':')
        名 = 名.strip()
        if 名:
            表级[名] = set(w.strip() for w in 权.split('/') if w.strip())
    return 库级, 表级
