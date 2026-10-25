#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面_设置.py — 设置面板（第 5 页）

参照项目 `baxiadb-v1.6/config_gui/ui/config_panel.py` **有一页现成的**，
左边一棵参数树、右边一个编辑框、存进 `baxia.conf`。形状借了，**底下一样
没抄**——那一页有三处毛病，抄过来就是把毛病抄进来：

    参照项目那边                          为什么不能抄
    ────────────────────────────────      ──────────────────────────────
    60 个参数，**44 个是死开关**            能改、能存、没有任何代码读它。
                                          `fsync_on_commit` 写着"提交后强制
                                          fsync（ACID 持久性）"，其实一个字
                                          都没生效——用户以为自己在管耐久性
    校验不过就**悄悄用默认值**               `manager.py:80-88`。改错一个字，
     (`manager.py`)                        界面上什么都看不出来
    `dynamic=True` 那个"动态修改"标记        空头支票：所有消费方都只在子系统
                                          **启动那一刻**读一次，没有一个订阅
                                          变更。而且它对同一项会同时打出
                                          「动态修改」和「需重启」两个相反的
                                          标记

所以这一页的原则只有一条：**只摆真会被读的数，并且把"什么时候生效"
写死在每一行上。**

这一页自己不碰引擎
==================

它是一张会写文件的表单。**引擎一个字节都不碰 `霸下.conf`**——读文件、
填默认值、把参数翻成 `ArenaConfig` 和 `**limits`，全在 `配置.py` 和
`霸下之躯.开库` 那一侧（见 `配置.py` 头部的三条规矩）。

**全部"重开生效"，没有一个"立即生效"。** 不是省事：引擎是单写者，而
界面只有一条工人线程独占闸口（`界面_接驳` 文件头那条铁律）。从界面线程
去改一个正在被闸口用的引擎状态，本身就是错的形状。所以这一页的保存
**只写文件**，一行引擎代码都不碰。

⚠ 起服务为什么是"开机"而不是"按一下"
======================================

用户要的是「**修改端口**」，而端口在霸下只有一处真的被读——服务端监听
在哪。所以这一节必须连"起服务"一起接，否则就是参照项目那个死 `port`
的重演（改完什么都没发生）。

**但它只能在开界面的时候起，不能在会话中途起。** 理由是硬的：

  1. `起服务` 之后，库归服务端的线程独占（`Service` 的说明：引擎是单
     写者）。而界面这条腿是**已经装好的本地闸口**——`工位` 里的工人
     在 `开库` 那一刻就把闸口捏住了。
  2. 中途一起服务，界面手里那条本地腿**不会自己断**，于是服务端线程
     和界面线程**同踩一个 Arena**——正是 `Service` 那一段明令禁止的。

所以在能中途切换的那件东西做出来之前（要让界面改用环回那条腿，就得
重新登录，而界面**故意不留口令**），这一节做成：**勾上 → 下次开界面
时起**。起完界面自己环回连回来，`形态` 就变成"远程"。

**库里还没有用户时起不来**（`龙龟之心.起服务` 里那道关口：没用户就是
谁都能当管理员）。所以崭新的库第一次开界面起不了服务，会明说一句，
让人先把管理员设好、再开一次。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import 界面零件 as _零件
import 配置 as _配置

# 生效时机那句话的颜色。**红的是真危险的那一档**（改了不报错、读错）。
生效色 = {
    '开机': '#2563EB',
    '重开MySQL口': '#B45309',
    '重开库': '#B45309',
    '立即': '#15803D',
    '只对新库': '#DC2626',
}


生效说法 = {
    '开机': '下次开界面生效',
    '重开MySQL口': '重开 MySQL 口生效',
    '重开库': '关掉库重开生效',
    '立即': '下一次用到就生效',
    '只对新库': '只对新建的库有效',
}

# 那几个"特定项"的占位提示。
# **按类型写会串**：`文本表` 这一版只有 `MySQL允许来源` 一个用户，但它是
# 个类型不是一件事——将来多一个同类项，照类型写死的提示就会跑到不相干的
# 那一行上去。
占位 = {
    'MySQL允许来源': '127.0.0.1, 192.168.1.7 …… 空着 = 谁也不许连',
}


def 拆表(文):
    """
    把那一格文本拆成一串。**半角、全角逗号都算分隔**（全角那个是中文
    输入法下的手滑，不该因此变成一个"永远匹配不上"的地址项）。

    ⛔ **空串拆出 `[]`，那是正当的设置**——`允许来源` 空着就是"谁也不许
    连"，和"没填"是同一件事，不是错。

    ⚠️ 空白**不当分隔**：真打成 `127.0.0.1 192.168.1.7`（没用逗号）时，
    拆出来是整段一个怪东西，`配置.校验` 会**明着报**"不是 IPv4 字面量"。
    要是这儿悄悄按空白拆了，那种手滑就永远不会被看见。
    """
    出 = []
    for 块 in (文 or '').replace('，', ',').split(','):
        块 = 块.strip()
        if 块:
            出.append(块)
    return 出

# 决定区域内部几何的那两条（`store_format.plan_region`）。
#
# ⛔ **它们现在不锁了。** 早先库文件一在盘上就 `setEnabled(False)`，用户
# 拍板：设置页一个设置项都不许锁死。所以这一版只留**说明**——把后果写在
# 控件旁边，改不改由人；真要存那两个数，`_on_save` 会再问一句。
#
# 为什么还值得单列出来：改 `平均记录大小` 的失败方式是**静默的**
# ——开库不报错，然后那个库看起来是空的（表全不见了，数据还在盘上，
# 把数改回去就回来）。别的设置项错了会当场报，这一条不会。
几何键 = ('区域容量MB', '平均记录大小')


class 设置面板(QWidget):
    """
    第 5 页。**和别的面板一样：公开的无参 `refresh()`。**

    主窗按 `panel_cls(引擎壳)` 造它，类名之外别的地方不用管。

    `client` 这一页用不着：设置是这台机器上的一个文件，和站在哪条腿上
    无关（远程腿改的是**本机**下次开界面时的样子）。所以不存 `_is_remote`。
    """

    def __init__(self, engine=None, client=None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._件 = {}            # 键 -> 控件
        self._型 = {}            # 键 -> 类型（`_收` 光看控件分不出"文本"和"文本表"）
        # 键 -> (读, 写)。**只给整数这一档记**：`QSpinBox` 和"文本框兜底"
        # 是两种控件（见 `界面零件.整数框`），`_摆` / `_收` 认这一对，
        # 就不用到处 `isinstance` 猜它是哪一种。
        self._整 = {}
        self._行 = {}            # 键 -> (标签, 说明标签)
        self._脏 = False         # 手上这份和盘上那份不一样（还没保存）
        self._在装 = False       # 正在往控件里填（填的过程中不算"人改过"）
        self._值 = {}            # 现在界面上这份（保存时用）
        self._setup_ui()

    # ── 搭界面 ──────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        外 = QVBoxLayout(self)
        外.setContentsMargins(0, 0, 0, 0)

        # 参照项目那页没有滚动区，参数一多就憋住。这里套一层。
        卷 = QScrollArea()
        卷.setWidgetResizable(True)
        卷.setFrameShape(QScrollArea.NoFrame)
        里 = QWidget()
        卷.setWidget(里)
        外.addWidget(卷, 1)

        布局 = QVBoxLayout(里)

        顶 = QHBoxLayout()
        标题 = QLabel('设置')
        标题.setStyleSheet('font-size: 16px; font-weight: bold;')
        顶.addWidget(标题)
        顶.addStretch()
        self._在哪 = QLabel('')
        self._在哪.setStyleSheet('color: #64748B;')
        顶.addWidget(self._在哪)
        布局.addLayout(顶)

        # 下面这些标签里的 `<b>` 是**真 HTML**（QLabel 认出来就按富文本
        # 渲染）。**不写 Markdown 那种双星号**——QLabel 不认它，摆出来就
        # 是字面的两个星号。（另外几页的标签里有这么写的，那是搬参照项目
        # 带过来的旧账，这一页不跟着写。）
        引导 = QLabel(
            '这里的每一条<b>都真的会被读</b>——指不出读它的那一行，这一页'
            '就不摆。\n'
            '存进 <b>%s</b>（和 `.py` 并排）。<b>全部下次开界面生效</b>，'
            '这一页不碰正在开着的库。\n'
            '把那个文件删掉就是全套默认值，库照开。'
            % os.path.basename(_配置.配置文件))
        引导.setStyleSheet('color: #64748B;')
        引导.setWordWrap(True)
        布局.addWidget(引导)

        # ── 服务：先摆状态，再摆参数 ──
        布局.addWidget(self._服务框())
        # MySQL 口紧跟在服务后面：两条都是"开界面时要不要开一个口"，
        # 摆在一起才看得出分别（一个是霸下自己的协议，一个是 MySQL 的）。
        布局.addWidget(self._MySQL框())
        # 其余各组一律照 `配置.分组()` 遍历着生成 —— **这个文件里不再写组名**。
        #
        # ⛔ 早先这一行写死的是 `('存储区', '语句上限')`，于是 `城防` 那 13 项
        # 和 `备份` 那 2 项**从来没有过控件**：手改 `霸下.conf` 是唯一的改法。
        # 更坏的是按一次「保存」还会把它们冲回默认值（`配置.写()` 是整份
        # 重写，而 `_on_save` 只交得出这一页收得到的键）。配置里多一个组、
        # 界面上就少一批设置项，这个错不该有第二次。
        for 组 in _配置.分组():
            if 组 in ('服务', 'MySQL'):
                # 这两组在上面有专用框（多一行实况标签），不走通用框。
                continue
            布局.addWidget(self._参数框(组))
        布局.addWidget(self._建库时用的框())
        布局.addStretch()

        # ── 底下那排按钮：**钉在滚动区外面** ──
        #
        # ⛔ 早先这一排摆在滚动区**里面**、在所有框的最后。参数十几条，
        # 得一路滚到底才看得见有个「保存」——加进 MySQL 那一节之后更长，
        # 于是"设置页没有保存功能"这句话就出现了（它一直在，只是没露过
        # 面）。钉在外面这一页多长它都在，和滚动条滚到哪儿无关。
        排 = QHBoxLayout()
        self._存钮 = QPushButton('保存')
        self._存钮.setToolTip('把这一页**所有**设置项写进 %s'
                              % _配置.配置文件)
        self._存钮.setMinimumWidth(100)
        self._存钮.setStyleSheet(
            'QPushButton { font-weight: bold; background-color: #2563EB;'
            ' color: white; padding: 6px 14px; border: none;'
            ' border-radius: 5px; }')
        self._存钮.clicked.connect(self._on_save)
        排.addWidget(self._存钮)

        self._默钮 = QPushButton('恢复默认')
        self._默钮.setToolTip('删掉配置文件——文件不在就是全套默认值')
        self._默钮.setMinimumWidth(100)
        self._默钮.clicked.connect(self._on_default)
        排.addWidget(self._默钮)

        self._载钮 = QPushButton('重新载入')
        self._载钮.setToolTip('把盘上那份重新读一遍，丢掉没保存的改动')
        self._载钮.setMinimumWidth(100)
        self._载钮.clicked.connect(lambda _c=False: self._装(强制=True))
        排.addWidget(self._载钮)

        # **一共多少项**摆在按钮旁边：这一页"全都存"这件事得看得见。
        self._共 = QLabel('')
        self._共.setStyleSheet('color: #64748B;')
        排.addWidget(self._共)
        排.addStretch()
        外.addLayout(排)

        self._状态 = QLabel('')
        self._状态.setWordWrap(True)
        外.addWidget(self._状态)

        self.refresh()

    # ── 服务那一节 ──────────────────────────────────────────────────

    def _服务框(self) -> QGroupBox:
        框 = QGroupBox('服务（这台机器开界面时怎么看这个库）')
        布 = QVBoxLayout(框)

        # 一行只读的实况。**它在最上面**，因为"现在到底跑没跑"是这一节
        # 唯一一个没人愿意猜的事。
        self._实况 = QLabel('')
        self._实况.setWordWrap(True)
        布.addWidget(self._实况)

        for 条 in _配置.条('服务'):
            布.addLayout(self._一行(条))

        注 = QLabel(
            '⚠ <b>起服务只在开界面那一刻做。</b> 会话中途去起，会让服务端'
            '线程和界面自己手里那条腿同踩一个存储区（引擎是单写者），所以'
            '这一页不做"点一下现在起"。\n'
            '库还没有用户时起不来（没用户就是谁都能当管理员）——先把管理员'
            '设好，再开一次界面。端口被占了会在开界面时明说，不会闷着。')
        注.setStyleSheet('color: #64748B;')
        注.setWordWrap(True)
        布.addWidget(注)
        return 框

    # ── MySQL 那一节 ────────────────────────────────────────────────

    def _MySQL框(self) -> QGroupBox:
        """
        MySQL 兼容口那一节。

        ⛔ **这一节是这一套配置的唯一一处。** 早先它只摆两个标量，账号映射
        和来源白名单在另一个文件（`霸下-mysql.conf`）里——而那个文件是
        **必需件却长得像可选项**：它不存在就是"账号映射是空表"，也就是
        **谁连都是 1045**，界面上那一格照样绿着。现在整组都在这儿，见
        `配置.py` 头部"轮到 MySQL 那一组时，规矩一差点被破"。
        """
        框 = QGroupBox('MySQL 兼容口（让爬虫直接用 pymysql 连进来）')
        布 = QVBoxLayout(框)

        # 实况摆最上面——"现在到底听着没有"是这一节唯一一个没人愿意猜的事。
        self._mysql实况 = QLabel('')
        self._mysql实况.setWordWrap(True)
        布.addWidget(self._mysql实况)

        for 条 in _配置.条('MySQL'):
            布.addLayout(self._一行(条))

        注 = QLabel(
            '⚠ <b>账号映射那一格是空的时候，谁连都是 1045</b>'
            '（那是"这个用户名不对"和"口令不对"共用的一个码，故意分不出'
            '来）——空表不是"谁都可以"，是"谁也不许"。\n'
            '⚠ 映射那一格里<b>一个口令都不存</b>：口令由爬虫自己填，'
            '服务端拿到之后交给霸下自己的登录去判。\n'
            '⚠ <b>这几条都在"重开这个口"的时候读一遍</b>——开界面时，'
            '或按工具栏上那个「MySQL 口」按钮时。设置页只管存，不碰正在'
            '跑的那个口（引擎是单写者）。\n'
            '库还没有用户时起不来（没用户就是谁都能当管理员）——'
            '先把管理员设好。')
        注.setStyleSheet('color: #64748B;')
        注.setWordWrap(True)
        布.addWidget(注)
        return 框

    # ── 参数那一节 ──────────────────────────────────────────────────

    def _参数框(self, 组) -> QGroupBox:
        框 = QGroupBox(组)
        布 = QVBoxLayout(框)
        for 条 in _配置.条(组):
            布.addLayout(self._一行(条))
        return 框

    def _一行(self, 条) -> QVBoxLayout:
        """
        一行 = `参数名 [____]` + 下面一行灰字说明 + 生效时机。

        **说明摆在控件的正下方，不是右边**：参照项目是右边一栏，参数一多
        就得左右来回扫，而这里每一句都短。
        """
        键, 名, 类型 = 条['键'], 条['名'], 条['类型']
        低, 高, 生效 = 条['下限'], 条['上限'], 条['生效']

        单位 = ''
        if 名.endswith(('（秒）', '（MB）', '（字节）')):
            # 名字里**已经写着单位了**，别再补一遍：不然 `玄武拉黑秒`
            # 会显示成「拉黑多久（秒）  (秒)」。
            pass
        elif 键.endswith('MB'):
            单位 = '  (MB)'
        elif 键 in ('平均记录大小',):
            单位 = '  (字节)'
        elif 键.endswith('秒'):
            单位 = '  (秒)'

        横 = QHBoxLayout()
        标 = QLabel('%s%s' % (名, 单位))
        标.setMinimumWidth(140)
        if 生效 == '只对新库':
            标.setStyleSheet('color: #DC2626; font-weight: bold;')
        横.addWidget(标)

        垫 = True            # 这一行末尾要不要补一根弹簧
        if 类型 == '开关':
            件 = QCheckBox('开着')
        elif 类型 == '整数':
            # **上下限越出 int32 的那几格由 `整数框` 自己退回文本框**
            # （今天只有 `玄武饵纪元` 一格：毫秒时间戳，十三个数字）。
            # 理由和那两条路的分工写在 `界面零件.整数框` 上面。
            件, 读, 写 = _零件.整数框(低, 高)
            self._整[键] = (读, 写)
        elif 类型 == '小数':
            件 = QDoubleSpinBox()
            件.setRange(float(低), float(高))
            件.setDecimals(4)
            件.setSingleStep(0.01)
            件.setMinimumWidth(160)
        elif 类型 == '账号表':
            # **它自己占满剩下的宽度**，后面不补弹簧——一张表补弹簧会被
            # 挤成一根细条，"加一行"那个按钮也跟着飘到中间去。
            件 = self._账号表控件()
            横.addWidget(件, 1)
            self._账号表按钮(横, 件)
            垫 = False
        else:
            件 = QLineEdit()
            件.setMinimumWidth(260)
            件.setPlaceholderText(占位.get(键, ''))
        件.setToolTip(条['说明'])
        if 垫:
            横.addWidget(件)
            横.addStretch()

        时 = QLabel(生效说法[生效])
        时.setStyleSheet('color: %s; font-weight: bold;' % 生效色[生效])
        横.addWidget(时)

        竖 = QVBoxLayout()
        竖.addLayout(横)
        说 = QLabel(条['说明'])
        说.setStyleSheet('color: #64748B;')
        说.setWordWrap(True)
        说.setContentsMargins(140, 0, 0, 8)
        竖.addWidget(说)

        self._件[键] = 件
        self._型[键] = 类型
        self._行[键] = (标, 说, 时)
        self._接脏(件, 键, 类型)
        return 竖

    # ── 账号映射那张表的编辑器 ──────────────────────────────────────

    def _账号表控件(self) -> QTableWidget:
        """
        账号映射那张表的编辑器。

        **做成一张真表**（左边 MySQL 用户名、右边霸下账号），不是一根
        逗号分隔的文本框：这两个名字都可能含空格和符号，挤在一根文本里
        就得分词、就得转义，而那是"看着能用、碰到某个名字就裂"的一类。

        ⛔ **这儿没有第三列"口令"，要加之前先看 `mysql_配置.py` 的文件头。**
        这一层一个口令都不存——加一列就是加一个"填了也不生效"的格子，
        而填它的人一定以为它生效。
        """
        表 = QTableWidget(0, 2)
        表.setHorizontalHeaderLabels(['MySQL 用户名（爬虫填的）',
                                      '霸下账号（库里真有的）'])
        表.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        表.verticalHeader().setVisible(False)
        表.setMinimumHeight(130)
        表.setMaximumHeight(260)
        return 表

    def _账号表按钮(self, 横, 表) -> None:
        """加一行 / 删选中。**摆在表右边**——它俩编辑的是同一个东西。"""
        竖 = QVBoxLayout()
        加 = QPushButton('加一行')
        加.setToolTip('添一行空的映射')
        加.clicked.connect(lambda _c=False: self._账号表加(表))
        删 = QPushButton('删选中')
        删.setToolTip('把表里选中的行删掉（多选就一起删）')
        删.clicked.connect(lambda _c=False: self._账号表删(表))
        for 钮 in (加, 删):
            钮.setMinimumWidth(80)
            竖.addWidget(钮)
        竖.addStretch()
        横.addLayout(竖)

    def _账号表加(self, 表) -> None:
        行 = 表.rowCount()
        表.insertRow(行)
        # **`insertRow` 只加格子不加内容项**，`item()` 还是 `None`；不
        # 自己铺上空项，`editItem(item(行, 0))` 就是拿 `None` 去点，当场炸。
        for 列 in (0, 1):
            表.setItem(行, 列, QTableWidgetItem(''))
        表.setCurrentCell(行, 0)
        表.editItem(表.item(行, 0))
        self._改过()

    def _账号表删(self, 表) -> None:
        行 = sorted({i.row() for i in 表.selectedIndexes()}, reverse=True)
        if not 行:
            self._状态.setText('先在表里点一行，再按「删选中」。')
            self._状态.setStyleSheet('color: #B45309;')
            return
        for i in 行:
            表.removeRow(i)
        self._改过()

    def _摆账号表(self, 表, 值) -> None:
        表.setRowCount(0)
        for 名, 项 in (值 or {}).items():
            行 = 表.rowCount()
            表.insertRow(行)
            表.setItem(行, 0, QTableWidgetItem(str(名)))
            账 = 项.get('霸下账号') if isinstance(项, dict) else ''
            表.setItem(行, 1, QTableWidgetItem('' if 账 is None else str(账)))

    def _收账号表(self, 表):
        """
        从表里读回来。

        两边都空的整行直接跳掉——"按下加一行、还没填就顺手保存"是常事，
        不该为这个报错。只填了一边的**当场报**：那是真填漏了。

        **用户名重了也当场报。** 收进 dict 的话后一行会静静盖掉前一行，
        而那一行还在屏幕上摆着——看着是两条规则，实际只有一条，另一条
        永远匹配不上。
        """
        出 = {}
        for 行 in range(表.rowCount()):
            甲 = 表.item(行, 0)
            乙 = 表.item(行, 1)
            名 = (甲.text().strip() if 甲 is not None else '')
            账 = (乙.text().strip() if 乙 is not None else '')
            if not 名 and not 账:
                continue
            if not 名:
                raise ValueError('第 %d 行填了霸下账号，MySQL 用户名空着' % (行 + 1))
            if not 账:
                raise ValueError('第 %d 行填了 MySQL 用户名，霸下账号空着' % (行 + 1))
            if 名 in 出:
                raise ValueError('MySQL 用户名「%s」在第 %d 行又出现了一次'
                                 % (名, 行 + 1))
            出[名] = {'霸下账号': 账}
        return 出

    def _接脏(self, 件, 键, 类型) -> None:
        """控件一动就记一笔"手上有没存的东西"，好让 `refresh()` 别把人
        正在敲的字冲掉。"""
        if 类型 == '开关':
            件.toggled.connect(lambda _v: self._改过())
        elif isinstance(件, (QSpinBox, QDoubleSpinBox)):
            件.valueChanged.connect(lambda _v: self._改过())
        elif 类型 == '账号表':
            # **`itemChanged` 填表的时候也会响。** 所以走 `_动了` 那道闸，
            # 不然 `_装` 一遍下来这一页就"永远有没保存的改动"了。
            件.itemChanged.connect(lambda _i: self._动了())
        else:
            件.textEdited.connect(lambda _s: self._改过())
            # 库路径一改，那两条旁边的说明就得重算——盘上有没有那个文件
            # 决定了 `区域容量` / `平均记录大小` 是"建新库"还是"动老库"。
            # **这一步不锁控件**（用户拍板一个都不锁），只换说明。
            if 键 == '库路径':
                件.textEdited.connect(lambda _s: self._按盘上说过())

    def _动了(self) -> None:
        """人在改（不是我们在往控件里填）才记脏。"""
        if not self._在装:
            self._改过()

    def _改过(self) -> None:
        if not self._脏:
            self._脏 = True
            self._状态.setText('有没保存的改动。')
            self._状态.setStyleSheet('color: #B45309;')

    # ── 只读：这个库建的时候用的是哪套 ──────────────────────────────

    def _建库时用的框(self) -> QGroupBox:
        """
        **盘上真有的那几条**，不是把配置再念一遍。

        这几条是从超级块读出来的（`arena.sb`）——**"这个库建的时候用的是
        哪套"只有盘上说了算**，配置里写的是什么不回答这个问题。读不到的
        那两条照实说读不到，不编。
        """
        框 = QGroupBox('这个库建的时候用的是哪套')
        布 = QVBoxLayout(框)
        self._建库 = QLabel('')
        self._建库.setWordWrap(True)
        self._建库.setTextFormat(Qt.RichText)
        布.addWidget(self._建库)

        注 = QLabel(
            '<b>超级块里只有这几条。</b> `平均记录大小` 是决定区域内部几何'
            '的两个数之一（`plan_region(容量, 平均记录大小)`），但它<b>既'
            '没写进超级块、开库时也不校验</b>——所以这里读不出来，也不猜。\n'
            '正因为盘上不记，在已有的库上改它才会<b>开库不报错、直接按错的'
            '几何读数据</b>。这就是盘上已经有库时，那两行的名字会变成琥珀色'
            '的原因——**只是提醒，没锁**：真要改，保存时会再问一句。')
        注.setStyleSheet('color: #64748B;')
        注.setWordWrap(True)
        布.addWidget(注)
        return 框

    def _刷建库(self) -> None:
        """
        摆 `引擎壳.建库参数` 那一份。

        **它是开界面那一刻从超级块抄下来的**（`霸下之躯.开库` 里抄的），
        不是每回现读。理由有两条：一是**主线程不许碰引擎**（`界面_接驳`
        文件头那条铁律——库归工人线程用），二是超级块里那几条里只有
        `区域个数`/`记录总数` 会变，而它们本来就不是这块要回答的问题。
        所以摆出来的写法标清楚是哪一刻的读数。
        """
        路 = getattr(self.engine, '库文件路径', None)
        参 = getattr(self.engine, '建库参数', None)
        if 参 is None:
            if 路 is None:
                self._建库.setText(
                    '库在对面那个进程里，这边读不到它的超级块——'
                    '<b>远程腿看不到这几条</b>，这是实情，不是坏了。')
            elif not os.path.exists(路):
                self._建库.setText('这个库文件还不存在——开界面时会按上面'
                                   '那套参数把它建出来。')
            else:
                self._建库.setText('这个库的超级块读不出来（库文件在，但'
                                   '开的时候没能读它）。')
            return
        self._建库.setText(
            '区域容量 <b>%d MB</b>　·　块大小 <b>%d 字节</b>　·　'
            '格式版本 <b>%d</b><br>'
            '<span style="color:#64748B;">下面两条是开界面那一刻的读数，'
            '之后会一直长：</span>　区域个数 <b>%d</b>　·　'
            '记录总数 <b>%d</b><br>'
            '平均记录大小 <b>盘上不记</b>（超级块里没有这一项）'
            % (参['区域容量MB'], 参['块大小'], 参['版本'],
               参['区域个数'], 参['记录总数']))

    # ── 实况那一行 ──────────────────────────────────────────────────

    def _刷实况(self) -> None:
        形 = getattr(self.engine, '形态', '本地')
        口 = getattr(self.engine, '服务端口', 0) or 0
        路 = getattr(self.engine, '库文件路径', None)
        if 形 == '远程' and 口:
            self._实况.setText(
                '✅ <b>这个库正开成服务</b>，听着 <b>%s:%d</b>，界面自己'
                '环回连回来（这条腿在这里是"远程"）。'
                % (getattr(self.engine, '服务主机', '127.0.0.1'), 口))
            self._实况.setStyleSheet('color: #16A34A;')
        elif 形 == '远程':
            self._实况.setText('✅ 连的是别人的服务端，库在对面。')
            self._实况.setStyleSheet('color: #16A34A;')
        else:
            self._实况.setText(
                '现在<b>没在起服务</b>——这个库只有界面这一条腿在用。'
                '（库文件：%s%s）'
                % (路 or '(没说)',
                   '' if (路 and os.path.exists(路)) else '，还不存在'))
            self._实况.setStyleSheet('color: #64748B;')
        self._在哪.setText('库文件在 %s' % (路 or '(非本地)'))
        self._刷MySQL实况()

    def _刷MySQL实况(self) -> None:
        """
        MySQL 口那一行的实况。**只读，不阻塞。**

        `门` 挂在引擎壳上（装配层填的），这一页只读它的两个数。
        没有那个属性就说明这一版装配层还没接上——**照实说"没接"**，
        不假装"没在听"：那两件事在用户眼里差很远。
        """
        门 = getattr(self.engine, 'MySQL口', None)
        if 门 is None:
            self._mysql实况.setText('这一版没有 MySQL 口。')
            self._mysql实况.setStyleSheet('color: #64748B;')
            return
        if not 门.听着():
            self._mysql实况.setText(
                '现在<b>没在听</b>。要开，按工具栏上那个「MySQL 口」按钮。')
            self._mysql实况.setStyleSheet('color: #64748B;')
            return
        self._mysql实况.setText(
            '✅ <b>正听着 %s:%s</b>，现在有 %d 条连接。'
            % (getattr(self.engine, 'MySQL地址', '127.0.0.1'),
               门.端口(), 门.连接数()))
        self._mysql实况.setStyleSheet('color: #16A34A;')

    # ── 拉一遍 ──────────────────────────────────────────────────────

    def refresh(self) -> None:
        """
        公开、无参、不阻塞——别的面板什么形状，它就是什么形状。

        **手上还有没存的改动时，绝不冲掉重填。** 全局刷新（F5）会把每
        一页都叫一遍，而这一页正开着的时候多半是人在敲字——冲掉就是"按
        一下 F5，刚填的东西没了"。所以那一支只刷只读那两块，并说一句。
        """
        self._刷建库()
        self._刷实况()
        if self._脏:
            self._状态.setText(
                '有没保存的改动——<b>没动你正在编辑的内容</b>。'
                '要丢掉重来，按「重新载入」。')
            self._状态.setStyleSheet('color: #B45309;')
            return
        self._装()

    def _装(self, 强制=False) -> None:
        """把盘上那份填进控件。**填之前把"脏"清掉。**"""
        值 = getattr(self.engine, '配置', None)
        问题 = []
        if 值 is None or 强制:
            值, 问题 = _配置.读()
         # **这本面板认的是引擎壳上那份**（开界面时读的、已经传给引擎的
        # 那一份）——它和盘上的文件这会儿是一回事。没有它就现读一遍。
        self._值 = dict(值)
        # 引擎壳上那份**跟着这次读到的换掉**：它是本方法下一次的首选来源
        # （上面那行 `getattr`），不换的话「重新载入」和「恢复默认」之后
        # 再按一次 F5，控件又被读回改动前那份——盘上新的、屏上旧的。
        # 全项目读这个属性的只有本文件。
        self.engine.配置 = dict(值)
        self._在装 = True
        try:
            for 键, 件 in self._件.items():
                self._摆(件, 键, self._值[键])
        finally:
            self._在装 = False
        self._脏 = False
        self._按盘上说过()
        self._刷实况()
        self._刷建库()
        self._共.setText('这一页一共 %d 项，保存一次全存。' % len(self._件))

        好 = _配置.配置文件
        在 = os.path.exists(好)
        self._状态.setText(
            '配置来自 %s%s。' % (好, '' if 在 else
                                 '（<b>文件不存在，现在是全套默认值</b>）'))
        self._状态.setStyleSheet('color: #64748B;')
        if 问题:
            self._报问题(问题)

    def _摆(self, 件, 键, 值) -> None:
        型 = self._型.get(键)
        if 键 in self._整:
            # 整数这一档由控件自己摆（`QSpinBox` 走 setValue，文本框走
            # setText），调用方不用管它是哪一种。
            self._整[键][1](值)
        elif isinstance(件, QCheckBox):
            件.setChecked(bool(值))
        elif isinstance(件, (QSpinBox, QDoubleSpinBox)):
            件.setValue(值)
        elif 型 == '账号表':
            self._摆账号表(件, 值)
        elif 型 == '文本表':
            # 盘上那串 IP 用 `,` 连起来摆。**用半角逗号**：这一格里装的
            # 就是 IPv4 字面量，全角逗号只会是手滑打出来的。
            件.setText(','.join(值 or []))
        else:
            件.setText(str(值))

    def _收(self, 键):
        """
        从控件读回来。

        **控件类型分不出"文本"和"文本表"**（都是 `QLineEdit`），所以
        除控件本身之外还照 `self._型` 看一眼。
        """
        件 = self._件[键]
        型 = self._型.get(键)
        if 键 in self._整:
            # 整数这一档：两条路（`QSpinBox` / 文本框兜底）在 `整数框`
            # 里就已经对齐了 —— 一律回 `int`，一律夹在配置的上下限里。
            return self._整[键][0]()
        if isinstance(件, QCheckBox):
            return 件.isChecked()
        if isinstance(件, QSpinBox):
            return int(件.value())
        if isinstance(件, QDoubleSpinBox):
            return float(件.value())
        if 型 == '账号表':
            return self._收账号表(件)
        if 型 == '文本表':
            return 拆表(件.text())
        return 件.text().strip()

    def _库在盘上(self):
        """
        盘上**已经有这个库**的那几个路径。没有就回空表。

        **两条路都算**，缺一条就不准：

          1. 配置里那个 `库路径` 的位置上已经有东西；
          2. **这次真开的那个库**（`引擎壳.库文件路径`）在盘上。

        第二条不能省：`霸下之躯 --库 别的.db` 的**命令行优先**（见 `开库`），
        这时配置里那个 `库路径` 和手上开着的库**是两个地方**，而开这个库时
        用的**正是配置里这份存储区参数**。只看第一条的话，`--库` 一给就漏了
        ——而那恰好是最危险的一种情况。
        """
        路 = self._收('库路径')
        现在 = getattr(self.engine, '库文件路径', None)
        在 = []
        for p in (路, 现在):
            if p and os.path.exists(p) and p not in 在:
                在.append(p)
        return 在

    def _按盘上说过(self) -> None:
        """
        把 `区域容量MB` / `平均记录大小` 这两条的**后果**写在它们旁边。

        ⛔ **这里不再 `setEnabled(False)`。** 早先盘上只要有库就把这两格
        锁死，用户拍板：设置页一个设置项都不许锁死。所以现在只剩说明——
        控件照样能改，改不改由人；真要存这两个数，`_on_save` 会再问一句。

        为什么还值得费这些字：改 `平均记录大小` 的失败方式是**静默的**
        （`配置.py` 头部那条红线）。别的设置项错了会当场报，这一条不会。
        """
        在 = self._库在盘上()

        for 键 in 几何键:
            # 有库的时候把名字标成琥珀色——**只是提醒**，`setEnabled`
            # 一个字都不许出现（那正是用户拍掉的东西）。
            #
            # ⚠ 没有库的时候要**还原成 `_一行` 当初那种红**（这两条的生效
            # 档是 `只对新库`），不是清空——清空就把那个标记抹掉了，而这
            # 一行是页面初次搭起来时立的，不该被这一趟刷新顺手擦掉。
            标 = self._行[键][0]
            标.setStyleSheet('color: #B45309; font-weight: bold;'
                             if 在 else 'color: #DC2626; font-weight: bold;')

        说 = self._行['区域容量MB'][1]
        if not 在:
            说.setText(
                '一个区域文件多大。建进超级块，开库时必须一致。'
                '<b>这两个数改得动</b>：盘上还没有这个库，下一次开界面会按'
                '这套几何把它建出来。')
            return

        路 = self._收('库路径')
        现在 = getattr(self.engine, '库文件路径', None)
        哪 = '、'.join(os.path.basename(p) for p in 在)
        说.setText(
            '⚠️ <b>%s 已经在盘上了</b>，改这两个数要当心——它们决定区域内部'
            '的几何。实测（造一个库、灌 200 行、再换一套参数开它）：\n'
            '　· 改<b>区域容量</b>：开库当场报错「区域容量不一致」——'
            '响的，改不了。\n'
            '　· 改<b>平均记录大小</b>：开库<b>不报错</b>，然后那个库'
            '<b>看起来是空的</b>——查询回来「库里一张表都没有」。'
            '（盘上那 200 行没动过，把数改回去就回来了。）\n'
            '要换一套几何，只能建一个新库再搬数据。'
            '<b>这两个数现在照样存得下去</b>，按「保存」时会再问一句。' % 哪)
        if 现在 and 路 and os.path.abspath(现在) != os.path.abspath(路):
            说.setText(说.text() + (
                '<br>（<b>注意</b>：这次开的是 `%s`，是启动参数指定的；'
                '上面「库文件」那一栏写的是 `%s`，是配置里那份。'
                '下一次不带 `--库` 开界面，用的才是配置里那个。）'
                % (现在, 路)))

    # ── 校验 ────────────────────────────────────────────────────────

    def _报问题(self, 问题) -> None:
        """
        **一处不合法就说一处，绝不悄悄用默认值。** 参照项目那边校验不过
        直接回落默认（`manager.py:80-88`），用户改错一个字界面上什么都看
        不出来——这一条是这一页最不该学的地方。
        """
        行 = []
        for 键, 说 in 问题:
            名 = 键 or '（整份文件）'
            if 键 in self._行:
                self._行[键][0].setStyleSheet(
                    'color: #DC2626; font-weight: bold;')
                名 = '%s（%s）' % (self._行[键][0].text(), 名)
            行.append('%s：%s' % (名, 说))
        self._状态.setText('⚠ %s' % '\n'.join(行))
        self._状态.setStyleSheet('color: #DC2626;')

    # ── 三件写操作 ──────────────────────────────────────────────────

    def _on_save(self) -> None:
        """**先整页校一遍，有一项不合法就一个字都不写。**"""
        try:
            值 = {键: self._收(键) for 键 in self._件}
        except ValueError as exc:
            # 账号那张表**自己**能看出错（填了一半、用户名重了），而
            # `配置.校验` 拿到的已经是一张 dict、看不出这两样。所以那
            # 两条在这一层报——待遇一样：一个字都不写。
            self._状态.setText('⚠ %s\n\n<b>一个字都没写。</b>' % exc)
            self._状态.setStyleSheet('color: #DC2626;')
            return
        坏 = []
        for 键, v in 值.items():
            错 = _配置.校验(键, v)
            if 错:
                坏.append((键, '%s：%s' % (self._行[键][0].text(), 错)))
                标 = self._行[键][0]
                标.setStyleSheet('color: #DC2626; font-weight: bold;')
        if 坏:
            self._状态.setText(
                '⚠ 这几项不合法，<b>一个字都没写</b>：\n%s'
                % '\n'.join(s for _k, s in 坏))
            self._状态.setStyleSheet('color: #DC2626;')
            return

        # ── 几何那两条：**问一句，不是拦** ──
        #
        # 用户拍板这一页不许锁死任何一项，所以改得动；但改 `平均记录大小`
        # 的失败方式是静默的（见 `几何键` 上面那段），所以存之前把话说明白。
        # **只是"确定/取消"，拦不住也不该拦**——盘上没库的时候根本不必问。
        动 = [k for k in 几何键 if k in 值 and 值[k] != self._值.get(k)]
        在 = self._库在盘上()
        if 动 and 在:
            r = QMessageBox.question(
                self, '要改区域几何',
                '你动了 %s，而 %s 已经在盘上了。\n\n'
                '这两个数决定区域内部的几何：\n'
                '　· 改<b>区域容量</b>：开库当场报错，响的。\n'
                '　· 改<b>平均记录大小</b>：开库<b>不报错</b>，然后那个库'
                '<b>看起来是空的</b>——表全不见，数据还在盘上，把数改回去'
                '就回来。\n\n'
                '要换一套几何，只能建一个新库再搬数据。\n\n'
                '还是要存吗？'
                % ('、'.join(self._行[k][0].text() for k in 动),
                   '、'.join(os.path.basename(p) for p in 在)),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                self._状态.setText(
                    '⚠ 没保存。区域几何那两个数还是原样。\n'
                    '（这一页别的改动<b>也没写</b>——一次保存是一整份。）')
                self._状态.setStyleSheet('color: #B45309;')
                return

        # ⛔ **合着盘上那份一起写。** `配置.写()` 是"默认值 + 你给我的键"
        # 整份重写，而这一页只交得出**它有控件的**那些键 —— 早先直接把页面
        # 收的 `值` 递过去，等于把这一页没露面的键**统统写回默认值**。
        # 现在以盘上那份为底、页面收到什么就覆盖什么，收不到的**原样留着**。
        # （这正是 `城防` 13 项和 `备份目录` 挨过的那一刀。）
        全, _ = _配置.读()
        页上 = len(值)
        全.update(值)
        try:
            _配置.写(全)
        except Exception as exc:
            self._状态.setText('写不进 %s：%s' % (_配置.配置文件, exc))
            self._状态.setStyleSheet('color: #DC2626;')
            return

        self._值 = dict(全)
        # 引擎壳上那份也是"配置"的一份，存完跟着换。它是 `_装()` 的首选
        # 来源，不换的话存完按一下 F5，控件就刷回**开库那一刻**的旧值——
        # 盘上是新的、屏上是旧的。全项目读这个属性的只有本文件。
        self.engine.配置 = dict(全)
        self._脏 = False
        # **报"这一页一共多少项、这次存了多少项"。** 「保存」这个词在
        # 用户那儿一直有个疑影（他原话是"我要的是保存我在设置界面的所有
        # 设置的东西，不是只保存这一个"），所以把数目直接写在回话里：
        # 一项都不少，全在这个文件里。
        #
        # 后半句同样要紧：这一页只写文件，引擎手上那份是开界面时给的——
        # 不说这句，人就会以为刚才改的已经生效了。
        self._状态.setText(
            '✅ 存好了 <b>%d 项</b>（这一页 %d 项 + 盘上原有 %d 项）→ %s\n'
            '<b>现在开着的这个库一个字都没变</b>——改的是下次开界面时用的'
            '那一份。要它生效，关掉界面重开。'
            % (len(全), 页上, len(全) - 页上, _配置.配置文件))
        self._状态.setStyleSheet('color: #16A34A;')
        self._按盘上说过()
        self._共.setText('这一页一共 %d 项，保存一次全存。' % len(self._件))

    def _on_default(self) -> None:
        """
        恢复默认 —— **就是把这个文件删掉**（`配置.py` 第二条规矩：文件不在
        就是全套默认，库照开）。
        """
        r = QMessageBox.question(
            self, '恢复默认',
            '把 %s 删掉？\n\n'
            '删掉之后这一页回到全套默认值，库照开——那个文件本来就是可选'
            '的（它不在，就是"全用默认"）。\n\n'
            '想留一份就先把内容抄走。' % _配置.配置文件,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        删了 = _配置.删()
        self._装(强制=True)
        self._状态.setText(
            '✅ 已恢复默认（那个文件%s）。<b>库和现在开着的这一份都没动</b>，'
            '下次开界面按默认值来。'
            % ('删掉了' if 删了 else '本来就不在'))
        self._状态.setStyleSheet('color: #16A34A;')
