# -*- coding: utf-8 -*-
"""
霸下数据库 — 一体式打包脚本
============================

一条命令出一个能直接发出去的文件夹：

    python 打包.py

**打出来的 exe 一律带控制台**（`--console`）：双击会跟一个黑框，
`print` 和没接住的报错全落在那个框里。GUI 程序出问题最坏的情况是
"点了没反应、一个字都没有"，那个框就是治它的——**报错得看得见**。
`调试` 那个参数还留着（只影响最后那句提示），现在跟正常版打的一样。

打出来长这样：

    发布/baxiadb-正式版-v2.0/
      baxiadb-正式版-v2.0.exe  ← 代码全在里面，就一个 exe
      依赖库/                ← 依赖（PySide6 那一大坨）全在里面
      软件图标图片/          ← 随行资源
      备份/  钥匙/           ← 随行数据目录（空的，摆在那儿告诉你东西放哪）
      霸下.conf              ← 第一次在设置页点保存才生出来
      霸下.db/               ← 第一次开库才生出来

⛔ **为什么是 onedir 不是 onefile。** onefile 是"单文件"，看着更利索，
但它每次启动都把几百 MB 解到临时目录，而且 **`sys._MEIPASS` 指向那个
用完就删的临时目录** ⇒ `配置.py:698` 按 `__file__` 算出来的
`霸下.conf` 会落进临时目录：**用户每改一次设置、关掉、再开，全回默认，
而且一声不响**（详细的三个后果见 `打包入口.py` 顶上那段）。
onedir 的 `依赖库/` 是实打实的文件夹，加上 `打包入口.py` 的锚定，
配置就稳稳落在 exe 旁边。

⛔ **排除清单里那几个必须排。** 这台机器上同时装着 **PyQt6 + pyqtgraph
（那是爬虫那边的）**，而本项目**一个都不 import**（全项目扫过一遍：
第三方只有 `PySide6` 和 `shiboken6`）。不排的话 PyInstaller 有可能
顺着钩子把它们一起收进去，包凭空胖一两百 MB。

⚠️ **`claude.exe` 那 337MB 不用管** —— PyInstaller 只收 `打包入口.py`
import 得到的东西，项目根那些开发文件（`开发日志/`、`上下文对话记录/`、
`备份/`、`钥匙/`、`霸下.db/`、`__pycache__/`、`x`、两个会话 txt、
`.claude/`）它压根不看。**唯一要手动搬的是 `软件图标图片/`**（见下面第 ③ 步）。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

项目根 = Path(__file__).resolve().parent

#: 打出来的 exe 叫什么。**中文名在 Windows 上没问题**，bootloader 认的是
#: 文件系统，不是控制台的代码页。
#:
#: ⚠️ 这个名字同时决定三样：exe 的名字、`dist/` 里那一层的目录名、以及
#: `发布/` 里那个文件夹的名字（`组装` 三处都照它算）。**改名就等于换一个
#: 发布文件夹**，旧的 `发布/霸下数据库/` 不会自己消失——发新版本之前，
#: 旧的那份要么删掉、要么另存。
程序名 = 'baxiadb-正式版-v2.0'

#: 依赖那个文件夹叫什么。PyInstaller 6 默认叫 `_internal`，这里改成中文 ——
#: 用户打开这个文件夹，得一眼看出来"这一坨是依赖，别动"。
依赖目录名 = '依赖库'

#: 随行资源。`软件图标图片/` 里有 `images.ico`（exe 图标）和
#: `red_start-baxia.png`（窗口图标，代码里按 `__file__` 找它 ——
#: 所以那张 png 另外还要靠 `--add-data` 进依赖库，见下面第 ② 步）。
随行资源 = ('软件图标图片',)

#: 随行数据目录。**建出来是空的**，纯粹是"东西会放这儿"的一个提示 ——
#: 备份根默认是 `<库目录>/备份`、钥匙根是配置里那条 `备份钥匙目录`，
#: 两个都不填也照样能跑。
随行数据 = ('备份', '钥匙')

#: 明确排除的模块。**分三类：**
#:   ① 这台机器上装了但本项目不 import 的（PyQt6 / pyqtgraph 那一路）
#:   ② GUI 用不上却是大件/常被误收的（tkinter / matplotlib / numpy …）
#:   ③ 只在开发时用得着的（pytest / setuptools / pip）
排除模块 = (
    # ① 同机装了、本项目不用
    'PyQt6', 'PyQt6.QtCore', 'PyQt6.sip', 'PyQt6_sip', 'pyqtgraph',
    # ② 大件、GUI 用不上
    'tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy', 'PIL',
    'sqlite3', 'IPython', 'pydoc_data',
    # ③ 开发时才用的
    'pytest', 'setuptools', 'pip', 'wheel',
)


def 说(话):
    print(话, flush=True)


def 清旧的(目标们):
    """每次从零打。**不清的后果是"改了代码没进包"** —— 那种岔子最难查。"""
    for 目标 in 目标们:
        路 = 项目根 / 目标
        if 路.exists():
            说('清掉 %s' % 路.name)
            shutil.rmtree(路, ignore_errors=True)


def 造命令(带控制台, 图标的):
    """
    拼 PyInstaller 那一条命令。

    ⚠️ **路径一律给绝对的。** 用了 `--specpath` 之后，spec 里那些相对路径
    是相对 **spec 文件**算的，不是相对项目根 —— 差一层目录就是"找不到
    图标/找不到那张 png"，而报错只会说文件不存在，不会说它去哪儿找了。
    """
    命令 = [sys.executable, '-m', 'PyInstaller',
            '--noconfirm', '--clean',
            '--onedir',
            '--name', 程序名,
            '--contents-directory', 依赖目录名,
            '--console',
            # 生成的 .spec / 中间产物全塞进 build，项目根保持干净
            '--specpath', str(项目根 / 'build'),
            '--workpath', str(项目根 / 'build' / 'work'),
            '--distpath', str(项目根 / 'dist'),
            # 窗口图标：代码按 `dirname(__file__)` 找它，而打包后那个
            # 目录 = `依赖库/`（= `sys._MEIPASS`）⇒ 放这儿正好对上。
            '--add-data', '%s%s.' % (项目根 / 'red_start-baxia.png', os.pathsep),
            ]
    if 图标的 is not None:
        命令 += ['--icon', str(图标的)]
    for 名字 in 排除模块:
        命令 += ['--exclude-module', 名字]
    命令.append(str(项目根 / '打包入口.py'))
    return 命令


def 组装(带控制台):
    """
    ② 之之后：把 `dist/` 里那一份搬到 `发布/`，再把随行文件夹摆进去。

    ⚠️ **exe 名字必须以 `--name` 为准。** `--name` 给的是 `程序名`，
    所以产物是 `dist/<程序名>/<程序名>.exe` —— 不是入口那个
    `打包入口.py` 的名字（**入口文件名跟 exe 名没有关系**）。
    """
    源 = 项目根 / 'dist' / 程序名
    到 = 项目根 / '发布' / 程序名
    if not 源.is_dir():
        raise SystemExit('❌ 没找到 %s —— PyInstaller 那一步没成功，'
                         '往上看它的报错。' % 源)
    if 到.exists():
        shutil.rmtree(到, ignore_errors=True)
    到.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(源), str(到))

    exe = 到 / ('%s.exe' % 程序名)
    if not exe.exists():
        raise SystemExit('❌ %s 里没有 %s.exe，打出来的东西不对。'
                         % (到, 程序名))

    # 随行资源：整个文件夹搬过去。**`软件图标图片/` 是唯一一个要手动搬的** ——
    # 代码不 import 它，PyInstaller 收不到它。
    for 名 in 随行资源:
        路 = 项目根 / 名
        if 路.is_dir():
            shutil.copytree(str(路), str(到 / 名))
        else:
            说('⚠️ 少了随行资源 %s（不影响能不能跑，只是少了那几张图）' % 名)

    # 随行数据目录：空的也建。**`备份/` 是留给"备份根默认落在库旁边"的**，
    # `钥匙/` 是留给配置里那条 `备份钥匙目录` 的 —— 建出来纯粹是给个提示。
    for 名 in 随行数据:
        (到 / 名).mkdir(exist_ok=True)

    return 到


def 报(到):
    """把发出去的那个文件夹念一遍。**顺手报体积** —— 打包最常见的意外就是体积。"""
    总 = 0
    说('')
    说('打好了：%s' % 到)
    说('-' * 60)
    for 路 in sorted(到.iterdir(), key=lambda p: (p.is_file(), p.name)):
        大小 = _体积(路)
        总 += 大小
        说('  %-14s %10s' % (路.name, _人读(大小)))
    说('-' * 60)
    说('  合计 %s' % _人读(总))
    说('')


def _体积(路):
    if 路.is_file():
        try:
            return 路.stat().st_size
        except OSError:
            return 0
    总 = 0
    for 根, _目录们, 文件们 in os.walk(str(路)):
        for 名 in 文件们:
            try:
                总 += os.path.getsize(os.path.join(根, 名))
            except OSError:
                pass
    return 总


def _人读(字节):
    for 单位 in ('B', 'KB', 'MB', 'GB'):
        if 字节 < 1024 or 单位 == 'GB':
            return '%.1f %s' % (字节, 单位)
        字节 /= 1024.0


def main():
    带控制台 = '调试' in sys.argv[1:]

    try:
        import PyInstaller                                  # noqa: F401
    except ImportError:
        raise SystemExit('❌ 没装 PyInstaller。装一条：\n'
                         '   %s -m pip install pyinstaller' % sys.executable)

    图标 = 项目根 / '软件图标图片' / 'images.ico'
    if not 图标.exists():
        说('⚠️ 没有 %s —— exe 就用默认图标了。' % 图标)
        图标 = None

    说('=== 霸下数据库 打包（%s） ===' % ('带控制台' if 带控制台 else '正常版'))
    清旧的(('build', 'dist', '发布'))

    命令 = 造命令(带控制台, 图标)
    说('跑 PyInstaller……（PySide6 那一大坨，第一次要几分钟）')
    结果 = subprocess.run(命令, cwd=str(项目根))
    if 结果.returncode != 0:
        raise SystemExit('❌ PyInstaller 失败（退出码 %d）。'
                         '加 `--console` 看不出的话，就 `python 打包.py 调试` '
                         '打一个带控制台的再跑一次。' % 结果.returncode)

    到 = 组装(带控制台)
    报(到)
    说('发出去就发 `%s` 这整个文件夹。' % 到.parent.name)
    说('（exe 带控制台：起不来、或者双击没反应的时候，'
       '那个黑框里就是报错本身。）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
