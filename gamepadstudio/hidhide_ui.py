"""设备隐身 (HidHide) 配置面板：驱动状态、应用程序、设备列表、总开关。

界面只通过 owner.hidhide(action, **payload) 访问驱动，本身不碰 IOCTL：远程模式
由后台 Agent 执行，独立运行时在本地执行，保证同一时刻只有一个写入者。
每个禁用状态和空列表都要写明原因，不能只留灰控件。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QListWidget, QListWidgetItem, QMessageBox, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .glass import TOKENS, Toggle
from .i18n import tr
from . import hidhide_devices

HIDHIDE_RELEASE_URL = 'https://github.com/nefarius/HidHide/releases/latest'

GROUP_ROLE = Qt.UserRole + 1
NODE_ROLE = Qt.UserRole + 2

# 全局样式表没有覆盖 QTreeWidget/QListWidget，不写就会得到亮色交替行。
LIST_STYLE = f'''
QTreeWidget, QListWidget {{
    background: {TOKENS['base']};
    border: 1px solid {TOKENS['border']};
    border-radius: 8px;
    color: {TOKENS['ink']};
    outline: 0;
}}
QTreeWidget::item, QListWidget::item {{ min-height: 26px; padding: 2px 4px; }}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background: {TOKENS['accent_bg']}; color: {TOKENS['ink']};
}}
QHeaderView::section {{
    background: {TOKENS['surface_lo']}; color: {TOKENS['ink_3']};
    border: 0; border-bottom: 1px solid {TOKENS['border']}; padding: 6px 8px;
}}
'''
TREE_STYLE = LIST_STYLE + '''
QTreeWidget::indicator { width: 18px; height: 18px; }
'''


class Page(QWidget):
    """Tab content with a ready vertical layout."""

    def __init__(self):
        super().__init__()
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(14, 12, 14, 12)
        self.box.setSpacing(8)



class HidHidePanel(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        from .studio import button, label
        self.owner = owner
        self.data = {}
        self.loading = False
        self.access_warning = ''
        self.setWindowTitle(tr('设备隐身 (HidHide)'))
        self.setMinimumSize(560, 430)
        self.resize(880, 640)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.status_pill = QLabel('')
        self.status_pill.setStyleSheet(
            f'background: {TOKENS["elevated"]}; color: {TOKENS["ink_2"]}; '
            f'border: 1px solid {TOKENS["border"]}; border-radius: 6px; padding: 4px 10px; font-weight: 700;')
        header.addWidget(self.status_pill)
        self.version_label = label('', 'muted')
        header.addWidget(self.version_label, 1)
        self.btn_refresh = button(tr('刷新'), lambda: self.refresh(), icon='refresh', pill=True)
        header.addWidget(self.btn_refresh)
        self.btn_client = button(tr('打开 HidHide 配置界面'), lambda: self.run_action('open_client', {}), pill=True)
        header.addWidget(self.btn_client)
        self.btn_driver = button(tr('一键安装驱动'), lambda: self.install_driver(), icon='plus', primary=True)
        header.addWidget(self.btn_driver)
        self.btn_release = button(tr('官方发布页'), lambda: QDesktopServices.openUrl(QUrl(HIDHIDE_RELEASE_URL)))
        self.btn_release.setObjectName('nav')
        header.addWidget(self.btn_release)
        root.addLayout(header)

        self.banner = label('', 'caption', True)
        self.banner.setStyleSheet(f'color: {TOKENS["amber"]};')
        self.banner.hide()
        root.addWidget(self.banner)

        self.btn_register = button(tr('重新登记本程序'), lambda: self.run_action('register_self', {}),
                                   icon='refresh', pill=True)
        self.btn_register.hide()
        root.addWidget(self.btn_register)

        master = QHBoxLayout()
        master.setSpacing(10)
        self.master = Toggle(tr('向其他应用隐藏已勾选的设备'))
        self.master.toggled.connect(self.on_master)
        master.addWidget(self.master)
        master.addStretch(1)
        self.btn_hide_current = button(tr('隐藏当前手柄'), lambda: self.hide_current(), pill=True)
        master.addWidget(self.btn_hide_current)
        root.addLayout(master)
        self.master_reason = label('', 'caption', True)
        self.master_reason.setStyleSheet(f"color: {TOKENS['ink_3']};")
        root.addWidget(self.master_reason)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        apps_page = Page()
        apps_bar = QHBoxLayout()
        apps_bar.setSpacing(8)
        self.btn_add_app = button(tr('添加程序'), lambda: self.add_app(), icon='plus', pill=True)
        self.btn_remove_app = button(tr('移除所选'), lambda: self.remove_app(), icon='trash', pill=True, danger=True)
        apps_bar.addWidget(self.btn_add_app)
        apps_bar.addWidget(self.btn_remove_app)
        apps_bar.addStretch(1)
        apps_page.box.addLayout(apps_bar)
        self.apps_hint = label(tr('列表中的程序仍能看到被隐藏的手柄。本程序会自动登记自身，否则开启隐身后我们自己就读不到手柄了。'),
                               'caption', True)
        self.apps_hint.setStyleSheet(f"color: {TOKENS['ink_3']};")
        apps_page.box.addWidget(self.apps_hint)
        self.apps = QListWidget()
        self.apps.setSelectionMode(QListWidget.SingleSelection)
        self.apps.setStyleSheet(LIST_STYLE)
        apps_page.box.addWidget(self.apps, 1)
        self.tabs.addTab(apps_page, tr('应用程序'))

        devices_page = Page()
        devices_bar = QHBoxLayout()
        devices_bar.setSpacing(12)
        self.filter_gaming = QCheckBox(tr('只显示游戏设备'))
        self.filter_present = QCheckBox(tr('隐藏已断开设备'))
        self.filter_present.setChecked(True)
        for control in (self.filter_gaming, self.filter_present):
            control.toggled.connect(self.fill_devices)
            devices_bar.addWidget(control)
        devices_bar.addStretch(1)
        devices_page.box.addLayout(devices_bar)
        self.devices_hint = label(tr('勾选整行会隐藏该设备的整套 HID 节点（含 XInput 通道）；物理与虚拟设备一样受控。'), 'caption', True)
        self.devices_hint.setStyleSheet(f"color: {TOKENS['ink_3']};")
        devices_page.box.addWidget(self.devices_hint)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([tr('设备'), tr('连接'), tr('VID:PID')])
        self.tree.setRootIsDecorated(True)
        self.tree.setTextElideMode(Qt.ElideMiddle)
        self.tree.setStyleSheet(TREE_STYLE)
        self.tree.itemChanged.connect(self.on_tree_changed)
        header_view = self.tree.header()
        header_view.setSectionResizeMode(0, QHeaderView.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        devices_page.box.addWidget(self.tree, 1)
        self.tabs.addTab(devices_page, tr('设备'))

        details_page = Page()
        self.details = label('', 'caption', True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        details_page.box.addWidget(self.details)
        details_page.box.addStretch(1)
        self.tabs.addTab(details_page, tr('驱动与状态'))

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.btn_restore_all = button(tr('一键恢复所有共享访问'), lambda: self.run_action('restore_all', {}),
                                      icon='restore', danger=True)
        footer.addWidget(self.btn_restore_all)
        self.last_message = label('', 'muted', True)
        footer.addWidget(self.last_message, 1)
        root.addLayout(footer)

        self.refresh()

    # ── 数据刷新 ────────────────────────────────────────────────────
    def request(self, action: str, **payload):
        try:
            return self.owner.hidhide(action, **payload) or {}
        except Exception as exc:
            return {'ok': False, 'message': tr('无法访问设备隐身服务：') + str(exc)}

    def refresh(self):
        self.loading = True
        data = self.request('snapshot')
        if isinstance(data, dict) and data.get('info'):
            self.data = data
        check = self.data.get('reconcile') or {}
        self.access_warning = check.get('message') or '' if check.get('status') == 'access_missing' else ''
        info = self.data.get('info', {})
        state = self.data.get('state', {})
        installed = bool(info.get('installed'))
        loaded = bool(info.get('loaded'))
        occupied = bool(state.get('occupied'))
        self.status_pill.setText(tr('已就绪') if loaded else (tr('驱动未运行') if installed else tr('未安装')))
        version = info.get('package_version') or ''
        driver = info.get('driver_version') or ''
        self.version_label.setText(f'HidHide {version} · ' + tr('驱动') + f' {driver}' if installed else tr('未检测到驱动'))
        self.btn_release.setVisible(not loaded)
        self.btn_driver.setVisible(not loaded)
        if not installed:
            self.btn_driver.setText(tr('一键安装驱动'))
        else:
            self.btn_driver.setText(tr('修复：启动驱动'))
        self.btn_client.setEnabled(loaded or installed)

        reasons = []
        if not installed:
            reasons.append(tr('未安装 HidHide 驱动。可一键安装，或先到官方发布页下载安装。'))
        elif not loaded:
            reasons.append(tr('驱动已安装但未运行。可尝试修复启动服务，或重启系统。'))
        elif occupied:
            holders = state.get('holders') or []
            reasons.append(tr('设备隐身配置正被其他程序占用，本面板只读。') +
                           ((' ' + tr('疑似占用：') + ' ' + '、'.join(holders)) if holders else ''))
            reasons.append(state.get('error') or '')
        if self.access_warning:
            reasons.append(self.access_warning)
        if state.get('inverse'):
            reasons.append(tr('HidHide 处于反向白名单模式：列表里的程序是被禁止访问的，本程序不会被登记。'))
        self.banner.setText('\n'.join(item for item in reasons if item))
        self.banner.setVisible(bool(reasons))
        self.btn_register.setVisible(bool(self.access_warning))

        writable = loaded and not occupied
        self.master.blockSignals(True)
        self.master.setChecked(bool(state.get('active')))
        self.master.setEnabled(writable)
        self.master.blockSignals(False)
        self.master_reason.setText(self.master_reason_text(writable, state))
        self.btn_hide_current.setEnabled(writable and bool(self.data.get('devices')))
        self.btn_restore_all.setEnabled(writable or occupied)
        self.btn_add_app.setEnabled(writable)
        self.btn_remove_app.setEnabled(writable)

        self.fill_apps()
        self.fill_devices()
        self.fill_details(info, state)
        self.loading = False

    def master_reason_text(self, writable: bool, state: dict) -> str:
        if not writable:
            return tr('当前无法修改驱动配置，原因见上方提示。')
        if state.get('active'):
            return tr('总开关已开启：未登记在应用程序列表里的程序，看不到已勾选隐藏的设备。')
        return tr('总开关未开启：即使已勾选设备，其他程序仍能访问。')

    # ── 应用程序页 ──────────────────────────────────────────────────
    def fill_apps(self):
        self.apps.clear()
        entries = self.data.get('apps') or []
        if not entries:
            item = QListWidgetItem(tr('列表为空。本程序会在需要时自动登记自身；这里添加的程序仍能看到被隐藏的手柄。'))
            item.setFlags(Qt.NoItemFlags)
            self.apps.addItem(item)
            return
        markers = {'self': tr('本程序'), 'self_pending': tr('本程序 · 待登记'),
                   'managed': tr('本程序添加'), 'external': tr('外部程序')}
        names = [entry['name'].lower() for entry in entries]
        for entry in entries:
            # 同名条目（例如两个 python.exe）必须能分辨，否则用户不知道删的是哪一个。
            title = entry['name']
            if names.count(entry['name'].lower()) > 1:
                parent = os.path.dirname(entry['display']).rstrip('\\')
                tail = '\\'.join(parent.split('\\')[-2:]) or parent
                title = f'{entry["name"]}  ({tail})'
            suffix = '' if entry['exists'] else tr('（文件不存在）')
            item = QListWidgetItem(f'{title}  ·  {markers.get(entry["source"], entry["source"])}{suffix}')
            item.setToolTip(entry['display'])
            item.setData(Qt.UserRole, entry['nt_path'])
            item.setData(Qt.UserRole + 1, entry['editable'])
            if not entry['exists'] or entry['source'] == 'self_pending':
                item.setForeground(QBrush(QColor(TOKENS['ink_3'])))
            self.apps.addItem(item)

    def add_app(self):
        start = ''
        path, _ = QFileDialog.getOpenFileName(self, tr('选择要允许访问手柄的程序'), start,
                                              tr('可执行程序 (*.exe);;所有文件 (*)'))
        if not path:
            return
        result = self.request('add_apps', paths=[path])
        self.report(result)
        self.refresh()

    def remove_app(self):
        item = self.apps.currentItem()
        if item is None or not item.data(Qt.UserRole + 1):
            self.last_message.setText(tr('请选中一条由本程序或你手动添加的条目。'))
            return
        if QMessageBox.question(self, tr('确认移除'), tr('移除后该程序将无法访问被隐藏的手柄，确定吗？')) != QMessageBox.Yes:
            return
        result = self.request('remove_app', nt_path=item.data(Qt.UserRole))
        self.report(result)
        self.refresh()

    # ── 设备页 ──────────────────────────────────────────────────────
    @staticmethod
    def origin_badges():
        # 本程序创建的由 created_by_us 单独覆盖；这里只标驱动血统，不做归属断言。
        return {hidhide_devices.ORIGIN_VIGEM: tr('虚拟手柄 · ViGEmBus'),
                hidhide_devices.ORIGIN_VHF: tr('虚拟设备 · VHF')}

    def fill_devices(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        groups = self.data.get('devices') or []
        gaming_only = self.filter_gaming.isChecked()
        hide_absent = self.filter_present.isChecked()
        badges = self.origin_badges()
        shown = 0
        for group in groups:
            if gaming_only and not group['gaming']:
                continue
            if hide_absent and not group['connected']:
                continue
            shown += 1
            badge = (tr('本程序创建') if group.get('created_by_us')
                     else badges.get(group.get('origin', ''), ''))
            title = f'{group["name"]}  ·  {badge}' if badge else f'{group["name"]}'
            parent = QTreeWidgetItem([title,
                                      tr('已连接') if group['connected'] else tr('已断开'),
                                      group['vid_pid']])
            parent.setData(0, GROUP_ROLE, group)
            if badge:
                detail = group.get('origin_detail') or ''
                parent.setToolTip(0, f'{tr("来源")}：{badge}'
                                   + (f'\n{tr("判定依据")}：{detail}' if detail else ''))
            parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable)
            if group['hidden']:
                parent.setCheckState(0, Qt.Checked)
            elif group['partly_hidden']:
                parent.setCheckState(0, Qt.PartiallyChecked)
            else:
                parent.setCheckState(0, Qt.Unchecked)
            for node in group['interfaces']:
                child = QTreeWidgetItem([node['label'],
                                         tr('已连接') if node['present'] else tr('已断开'),
                                         ''])
                child.setData(0, NODE_ROLE, {'group': group, 'node': node})
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked if node['hidden'] else Qt.Unchecked)
                note = tr('无法读取（可能被其他程序独占或本程序未被授权）') if node['denied'] else ''
                if note:
                    child.setToolTip(0, note)
                if not node['present']:
                    child.setForeground(0, QBrush(QColor(TOKENS['ink_dim'])))
                parent.addChild(child)
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(shown <= 2)
        if not shown:
            empty = QTreeWidgetItem([tr('没有符合条件的设备。连接手柄后点「刷新」，或取消筛选查看已断开设备。'), '', ''])
            empty.setFlags(Qt.NoItemFlags)
            self.tree.addTopLevelItem(empty)
        self.tree.blockSignals(False)

    def on_tree_changed(self, item, column):
        if self.loading:
            return
        group = item.data(0, GROUP_ROLE)
        node = item.data(0, NODE_ROLE)
        if group is None and node is None:
            return
        wanted = item.checkState(0) == Qt.Checked
        if group is not None:
            result = self.request('set_group_hidden', key=group['key'], hidden=bool(wanted))
        else:
            # 单个集合点也要带上它的 XInput 祖先节点，否则游戏仍能从 XInput 读到同一支手柄。
            entry = node['node']
            paths = [entry['instance_id']]
            if entry['xinput']:
                paths.append(entry['xinput'])
            result = self.request('set_node_hidden', paths=paths, hidden=bool(wanted))
        self.report(result)
        self.refresh()

    def hide_current(self):
        state = getattr(self.owner, 'snapshot', None) or {}
        result = self.request('hide_current', vendor=state.get('vendor'), product=state.get('product'),
                              device_path=state.get('device_path') or '')
        self.report(result)
        self.refresh()

    def fill_details(self, info: dict, state: dict):
        rows = [
            (tr('安装状态'), tr('已安装') if info.get('installed') else tr('未安装')),
            (tr('驱动可访问'), tr('是') if info.get('loaded') else tr('否')),
            (tr('安装包版本'), info.get('package_version') or '-'),
            (tr('驱动文件版本'), info.get('driver_version') or '-'),
            (tr('安装目录'), info.get('install_path') or '-'),
            (tr('守护服务'), info.get('watchdog') or '-'),
            (tr('过滤驱动服务'), info.get('service_state') or '-'),
            (tr('总开关'), tr('已开启') if state.get('active') else tr('未开启')),
            (tr('反向模式'), tr('开启（列表含义反转）') if state.get('inverse') else tr('关闭')),
            (tr('已登记程序'), str(len(state.get('whitelist') or []))),
            (tr('已隐藏节点'), str(len(state.get('blacklist') or []))),
            (tr('本程序添加的节点'), str(len((self.data.get('managed') or {}).get('paths') or []))),
        ]
        self.details.setText('\n'.join(f'{key}：{value}' for key, value in rows))

    # ── 动作 ────────────────────────────────────────────────────────
    def install_driver(self):
        """安装/修复都要提权，所以先说清会发生什么，再交给用户确认。"""
        info = self.data.get('info') or {}
        installed = bool(info.get('installed'))
        action = 'repair_driver' if installed else 'install_driver'
        explain = (tr('修复需要管理员权限，用于启动已安装但未运行的驱动服务。') if installed else
                   tr('安装需要管理员权限，安装完成后通常需要重启系统。这是内核驱动唯一一次提权，之后的配置不再需要管理员。'))
        if QMessageBox.question(self, tr('确认'), explain) != QMessageBox.Yes:
            return
        result = self.request(action, {})
        self.report(result)
        if result.get('reboot_required'):
            self.last_message.setText(tr('驱动已安装，请重启系统后再使用设备隐身。'))
        self.refresh()

    def run_action(self, action: str, payload: dict):
        if action == 'open_client':
            result = self.request('open_client', {})
            self.report(result)
            self.refresh()
            return
        result = self.request(action, **payload)
        if action == 'restore_all' and result.get('ok'):
            self.last_message.setText(tr('已清空隐藏列表并关闭总开关。'))
        self.report(result)
        self.refresh()

    def on_master(self, checked: bool):
        if self.loading:
            return
        result = self.request('set_enabled', enabled=bool(checked))
        self.report(result)
        self.refresh()

    def report(self, result: dict):
        message = (result or {}).get('message') or ''
        if message:
            self.last_message.setText(message)
            self.last_message.setStyleSheet(
                f"color: {TOKENS['ink_3'] if result.get('ok') else TOKENS['red']};")
