import argparse
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import AppKit as A
import ApplicationServices as AX
import CoreFoundation as CF
import Quartz as Q
import objc
from Foundation import NSObject, NSTimer, NSURL, NSUserDefaults

from seekbar.hotkey import DoubleCommand
from seekbar.quickfolders import FolderPanel, FolderStrip, brand_icon, focused_target
from seekbar.search import classify, discover_apps, is_navigable_folder, search


SCOPES = ("all", "app", "folder", "file")
SCOPE_NAMES = ("全部", "应用", "文件夹", "文件")
TYPE_NAMES = {"app": "应用", "folder": "文件夹", "file": "文件"}
APP_NAME = "寻径"
FOLDER_ACCESS = (("Desktop", "桌面"), ("Documents", "文稿"), ("Downloads", "下载"))


class Preferences:
    def __init__(self, suite=None):
        self.defaults = NSUserDefaults.alloc().initWithSuiteName_(suite) if suite else NSUserDefaults.standardUserDefaults()
        self.defaults.registerDefaults_({
            "Favorites": [os.path.expanduser("~/" + name) for name, _ in FOLDER_ACCESS],
            "Recent": [], "EnableFolderStrip": True, "ShowInFinder": True, "SearchRoot": "",
        })

    @property
    def favorites(self):
        return list(self.defaults.arrayForKey_("Favorites"))

    @property
    def recent(self):
        return list(self.defaults.arrayForKey_("Recent"))

    @property
    def enabled(self):
        return self.defaults.boolForKey_("EnableFolderStrip")

    @property
    def finder(self):
        return self.defaults.boolForKey_("ShowInFinder")

    @property
    def root(self):
        return self.defaults.stringForKey_("SearchRoot") or None

    def set_favorite(self, path, add):
        paths = self.favorites
        if add and path not in paths:
            paths = [path, *paths]
        elif not add:
            paths = [item for item in paths if item != path]
        self.defaults.setObject_forKey_(paths, "Favorites")

    def remember(self, path):
        paths = [path, *(item for item in self.recent if item != path)]
        self.defaults.setObject_forKey_(paths[:35], "Recent")

    @property
    def last_folder(self):
        path = self.defaults.stringForKey_("LastFolder")
        if is_navigable_folder(path):
            return path
        return next((path for path in self.recent if is_navigable_folder(path)), None)

    def record_folder(self, path):
        if path != self.defaults.stringForKey_("LastFolder") and is_navigable_folder(path):
            self.defaults.setObject_forKey_(path, "LastFolder")


class SearchWindow(A.NSWindow):
    owner = None

    def sendEvent_(self, event):
        if self.owner and event.type() == A.NSEventTypeKeyDown and self.owner.key_event(event):
            return
        objc.super(SearchWindow, self).sendEvent_(event)

    def acceptsPreviewPanelControl_(self, _panel):
        return True

    def beginPreviewPanelControl_(self, panel):
        panel.setDataSource_(self.owner)

    def endPreviewPanelControl_(self, panel):
        panel.setDataSource_(None)


class ResultTable(A.NSTableView):
    def menuForEvent_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        row = self.rowAtPoint_(point)
        if row < 0:
            return None
        self.selectRowIndexes_byExtendingSelection_(A.NSIndexSet.indexSetWithIndex_(row), False)
        return self.delegate().result_menu()


class Controller(NSObject):
    def init(self):
        self = objc.super(Controller, self).init()
        if self is None:
            return None
        self.ui_check = "--ui-self-test" in sys.argv
        self.test_domain = f"local.seekbar.test.{os.getpid()}" if self.ui_check else None
        self.prefs = Preferences(self.test_domain)
        self.tap = self.tap_source = None
        self.hotkey_ready = False
        self.recognizer = DoubleCommand()

        @objc.callbackFor(Q.CGEventTapCreate)
        def callback(proxy, kind, event, context):
            return self.hotkey_event(proxy, kind, event, context)

        # Quartz uses the callback after CGEventTapCreate has returned.
        self.hotkey_callback = callback
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Spotlight")
        self.pending = self.debounce = None
        self.generation = 0
        self.displayed_generation = -1
        self.scope = "all"
        self.results = []
        self.return_to = None
        self.timers = set()
        self.preview = None
        self.active_picker = None
        self.permissions_window = None
        return self

    def applicationDidFinishLaunching_(self, _notice):
        self.make_window()
        self.strip = FolderStrip.alloc().initWithOwner_(self)
        self.make_status_item()
        if self.ui_check:
            self.show()
            self.after(0.7, self.check_native_ui)
            return
        self.start_hotkey()
        self.poller = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.38, True, lambda _timer: self.strip.update())
        self.retry = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(4.0, True, lambda _timer: self.start_hotkey())
        self.show()
        if not self.prefs.defaults.boolForKey_("PermissionSetupShown"):
            self.showPermissions_(None)
            self.prefs.defaults.setBool_forKey_(True, "PermissionSetupShown")

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _visible):
        if self.permissions_window and self.permissions_window.isVisible():
            self.permissions_window.makeKeyAndOrderFront_(None)
        elif self.window.isVisible():
            self.window.makeKeyAndOrderFront_(None)
        else:
            self.show()
        return False

    def applicationDidBecomeActive_(self, _notice):
        if not self.ui_check and hasattr(self, "hotkey_enable_button"):
            self.start_hotkey()

    def applicationDidResignActive_(self, _notice):
        setup_visible = self.permissions_window and self.permissions_window.isVisible()
        if self.window.isVisible() and self.displayed_generation == self.generation and not setup_visible:
            self.close_search(return_focus=False)

    def applicationWillTerminate_(self, _notice):
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.test_domain:
            self.prefs.defaults.removePersistentDomainForName_(self.test_domain)
        self.stop_hotkey()

    @objc.python_method
    def after(self, delay, fn):
        def run(timer):
            self.timers.discard(timer)
            fn()
        timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(delay, False, run)
        self.timers.add(timer)
        return timer

    @objc.python_method
    def start_hotkey(self):
        allowed = bool(Q.CGPreflightListenEventAccess())
        if self.tap is not None and (not allowed or not CF.CFMachPortIsValid(self.tap)):
            self.stop_hotkey()
        if allowed and self.tap is None:
            event_types = (Q.kCGEventFlagsChanged, Q.kCGEventKeyDown, Q.kCGEventLeftMouseDown, Q.kCGEventRightMouseDown, Q.kCGEventOtherMouseDown)
            mask = sum(1 << kind for kind in event_types)
            tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap, Q.kCGEventTapOptionListenOnly, mask, self.hotkey_callback, None)
            if tap is not None:
                source = CF.CFMachPortCreateRunLoopSource(None, tap, 0)
                if source is not None:
                    self.tap, self.tap_source = tap, source
                    CF.CFRunLoopAddSource(CF.CFRunLoopGetMain(), source, CF.kCFRunLoopCommonModes)
                else:
                    CF.CFMachPortInvalidate(tap)
        if self.tap is not None and not Q.CGEventTapIsEnabled(self.tap):
            Q.CGEventTapEnable(self.tap, True)
        self.hotkey_ready = self.tap is not None and Q.CGEventTapIsEnabled(self.tap)
        self.refresh_hotkey_indicator(allowed)
        return self.hotkey_ready

    @objc.python_method
    def stop_hotkey(self):
        if self.tap_source is not None:
            CF.CFRunLoopRemoveSource(CF.CFRunLoopGetMain(), self.tap_source, CF.kCFRunLoopCommonModes)
        if self.tap is not None:
            CF.CFMachPortInvalidate(self.tap)
        self.tap = self.tap_source = None
        self.hotkey_ready = False
        self.recognizer.reset()

    @objc.python_method
    def refresh_hotkey_indicator(self, allowed):
        self.hotkey_enable_button.setHidden_(self.hotkey_ready)
        self.scope_label.setFrameSize_(A.NSMakeSize(340 if self.hotkey_ready else 203, 17))
        if hasattr(self, "status"):
            tooltip = "寻径 · 连按两次 Command" if self.hotkey_ready else "寻径 · 双击 Command 需要输入监控"
            self.status.button().setToolTip_(tooltip)
        if hasattr(self, "hotkey_permission_status"):
            status = "已启用" if self.hotkey_ready else "等待系统授权" if not allowed else "监听未启动"
            self.hotkey_permission_status.setStringValue_(status)

    @objc.python_method
    def hotkey_event(self, _proxy, kind, event, _info):
        if kind in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
            self.recognizer.reset()
            if self.tap is not None:
                Q.CGEventTapEnable(self.tap, True)
        elif kind == Q.kCGEventFlagsChanged:
            flags = Q.CGEventGetFlags(event)
            keycode = Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode)
            other = Q.kCGEventFlagMaskControl | Q.kCGEventFlagMaskAlternate | Q.kCGEventFlagMaskShift | Q.kCGEventFlagMaskSecondaryFn
            timestamp = Q.CGEventGetTimestamp(event) / 1_000_000_000
            if self.recognizer.modifier(keycode, bool(flags & Q.kCGEventFlagMaskCommand), bool(flags & other), timestamp):
                self.after(0.005, self.toggle_search)
        else:
            self.recognizer.reset()
        return event

    @objc.python_method
    def make_window(self):
        self.window = SearchWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, 680, 514), A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable,
            A.NSBackingStoreBuffered, False,
        )
        self.window.owner = self
        self.window.setDelegate_(self)
        self.window.setTitle_(APP_NAME)
        self.window.setReleasedWhenClosed_(False)
        self.window.setCollectionBehavior_(A.NSWindowCollectionBehaviorMoveToActiveSpace | A.NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.window.center()
        view = self.window.contentView()
        view.addSubview_(brand_icon(A.NSMakeRect(20, 451, 32, 32)))
        self.search_field = A.NSSearchField.alloc().initWithFrame_(A.NSMakeRect(60, 449, 600, 38))
        self.search_field.setFont_(A.NSFont.systemFontOfSize_(21))
        self.search_field.setDelegate_(self)
        self.search_field.setPlaceholderString_("搜索应用、文件或文件夹")
        self.search_field.setAccessibilityLabel_("搜索")
        view.addSubview_(self.search_field)
        self.segments = A.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            SCOPE_NAMES, A.NSSegmentSwitchTrackingSelectOne, self, "changeScope:"
        )
        self.segments.setFrame_(A.NSMakeRect(20, 414, 292, 25))
        self.segments.setSelectedSegment_(0)
        view.addSubview_(self.segments)
        self.scope_label = A.NSTextField.labelWithString_("")
        self.scope_label.setFrame_(A.NSMakeRect(320, 417, 340, 17))
        self.scope_label.setLineBreakMode_(A.NSLineBreakByTruncatingHead)
        self.scope_label.setAlignment_(A.NSTextAlignmentRight)
        self.scope_label.setTextColor_(A.NSColor.secondaryLabelColor())
        view.addSubview_(self.scope_label)
        self.hotkey_enable_button = A.NSButton.buttonWithTitle_target_action_("启用 ⌘ ⌘…", self, "requestInput:")
        self.hotkey_enable_button.setFrame_(A.NSMakeRect(534, 413, 127, 27))
        self.hotkey_enable_button.setToolTip_("打开 macOS 输入监控设置，启用全局双击 Command")
        view.addSubview_(self.hotkey_enable_button)
        self.refresh_hotkey_indicator(False)
        scroll = A.NSScrollView.alloc().initWithFrame_(A.NSMakeRect(20, 52, 640, 351))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(A.NSBezelBorder)
        self.table = ResultTable.alloc().initWithFrame_(A.NSMakeRect(0, 0, 636, 351))
        column = A.NSTableColumn.alloc().initWithIdentifier_("result")
        column.setWidth_(635)
        self.table.addTableColumn_(column)
        self.table.setHeaderView_(None)
        self.table.setRowHeight_(46)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setDelegate_(self)
        self.table.setDataSource_(self)
        self.table.setTarget_(self)
        self.table.setDoubleAction_("openSelection:")
        scroll.setDocumentView_(self.table)
        view.addSubview_(scroll)
        self.footer = A.NSTextField.labelWithString_("")
        self.footer.setFrame_(A.NSMakeRect(22, 15, 532, 21))
        self.footer.setTextColor_(A.NSColor.secondaryLabelColor())
        view.addSubview_(self.footer)
        actions = A.NSButton.buttonWithTitle_target_action_("操作…", self, "showActions:")
        actions.setFrame_(A.NSMakeRect(560, 13, 101, 26))
        view.addSubview_(actions)

    @objc.python_method
    def make_status_item(self):
        self.status = A.NSStatusBar.systemStatusBar().statusItemWithLength_(A.NSVariableStatusItemLength)
        button = self.status.button()
        button.setImage_(A.NSImage.imageWithSystemSymbolName_accessibilityDescription_("folder.badge.magnifyingglass", APP_NAME))
        if button.image() is None:
            button.setTitle_(APP_NAME)
        button.setToolTip_("寻径 · 连按两次 Command")
        self.menu = A.NSMenu.alloc().initWithTitle_(APP_NAME)
        self.menu.setDelegate_(self)
        self.menuWillOpen_(self.menu)
        self.status.setMenu_(self.menu)
        main_menu = A.NSMenu.alloc().init()
        item = A.NSMenuItem.alloc().init()
        main_menu.addItem_(item)
        app_menu = A.NSMenu.alloc().initWithTitle_(APP_NAME)
        self.add_item(app_menu, "退出寻径", "terminate:", "q", A.NSApplication.sharedApplication())
        main_menu.setSubmenu_forItem_(app_menu, item)
        A.NSApplication.sharedApplication().setMainMenu_(main_menu)

    @objc.python_method
    def add_item(self, menu, title, action, key="", target=None, data=None):
        item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        item.setTarget_(self if target is None else target)
        if data is not None:
            item.setRepresentedObject_(data)
        menu.addItem_(item)
        return item

    def menuWillOpen_(self, menu):
        menu.removeAllItems()
        self.add_item(menu, "打开搜索    ⌘ ⌘", "showSearch:")
        self.add_item(menu, "搜索文件夹", "showFolders:")
        menu.addItem_(A.NSMenuItem.separatorItem())
        favorites = A.NSMenu.alloc().initWithTitle_("常用文件夹")
        for path in self.prefs.favorites:
            self.add_item(favorites, self.folder_name(path), "openFavorite:", data=path)
        favorites.addItem_(A.NSMenuItem.separatorItem())
        self.add_item(favorites, "添加常用文件夹…", "addFavorite:")
        remove = A.NSMenu.alloc().initWithTitle_("移除")
        for path in self.prefs.favorites:
            self.add_item(remove, self.folder_name(path), "removeFavorite:", data=path)
        if self.prefs.favorites:
            favorites.addItem_(A.NSMenuItem.separatorItem())
            favorites.setSubmenu_forItem_(remove, favorites.addItemWithTitle_action_keyEquivalent_("移除常用文件夹", None, ""))
        menu.setSubmenu_forItem_(favorites, menu.addItemWithTitle_action_keyEquivalent_("常用文件夹", None, ""))
        self.add_item(menu, "设置搜索范围…", "chooseRoot:")
        if self.prefs.root:
            self.add_item(menu, "恢复 Spotlight 搜索范围", "clearRoot:")
        menu.addItem_(A.NSMenuItem.separatorItem())
        strip = self.add_item(menu, "在打开／存储窗口下显示文件夹栏", "toggleStrip:")
        strip.setState_(int(self.prefs.enabled))
        finder = self.add_item(menu, "也在 Finder 窗口下显示", "toggleFinder:")
        finder.setState_(int(self.prefs.finder))
        if not AX.AXIsProcessTrusted():
            self.add_item(menu, "启用文件夹栏（辅助功能）…", "requestAccessibility:")
        if not self.hotkey_ready:
            self.add_item(menu, "启用双击 Command（输入监控）…", "requestInput:")
        self.add_item(menu, "权限设置…", "showPermissions:")
        menu.addItem_(A.NSMenuItem.separatorItem())
        self.add_item(menu, "退出寻径", "terminate:", "q", A.NSApplication.sharedApplication())

    def showPermissions_(self, _sender):
        if self.permissions_window is None:
            self.permissions_window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                A.NSMakeRect(0, 0, 555, 338), A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable,
                A.NSBackingStoreBuffered, False,
            )
            self.permissions_window.setTitle_("寻径权限设置")
            self.permissions_window.setReleasedWhenClosed_(False)
            view = self.permissions_window.contentView()

            def label(title, x, y, width):
                field = A.NSTextField.labelWithString_(title)
                field.setFrame_(A.NSMakeRect(x, y, width, 21))
                view.addSubview_(field)
                return field

            def button(title, action, x, y, width):
                item = A.NSButton.buttonWithTitle_target_action_(title, self, action)
                item.setFrame_(A.NSMakeRect(x, y, width, 29))
                view.addSubview_(item)
                return item

            title = label("设置寻径权限", 22, 289, 504)
            title.setFrameSize_(A.NSMakeSize(504, 30))
            title.setFont_(A.NSFont.systemFontOfSize_weight_(21, A.NSFontWeightSemibold))
            label("按需要启用，并在 macOS 提示或系统设置中确认。", 23, 265, 504)
            label("文件搜索", 23, 226, 125)
            for index, (_, name) in enumerate(FOLDER_ACCESS):
                item = button(name, "requestFolderAccess:", 151 + index * 81, 224, 77)
                item.setTag_(index)
            button("文件与文件夹设置", "openFolderSettings:", 399, 224, 134)
            label("窗口下方的文件夹栏", 23, 184, 235)
            button("辅助功能…", "requestAccessibility:", 395, 182, 138)
            label("全局双击 Command", 23, 145, 170)
            self.hotkey_permission_status = label("", 195, 145, 160)
            self.hotkey_permission_status.setTextColor_(A.NSColor.secondaryLabelColor())
            button("输入监控设置…", "requestInput:", 395, 143, 138)
            label("其他受保护文件夹（可选）", 23, 106, 270)
            button("完全磁盘访问权限…", "openFullDiskSettings:", 365, 104, 168)
            self.permission_message = label("点击文件夹名称可检查该位置的读取权限。", 23, 69, 510)
            self.permission_message.setTextColor_(A.NSColor.secondaryLabelColor())
            done = button("完成", "closePermissions:", 436, 21, 97)
            done.setKeyEquivalent_("\r")
            self.permissions_window.center()
        A.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.refresh_hotkey_indicator(bool(Q.CGPreflightListenEventAccess()))
        self.permissions_window.makeKeyAndOrderFront_(None)

    def closePermissions_(self, _sender):
        self.permissions_window.orderOut_(None)
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.search_field)
        if self.search_field.stringValue().strip():
            self.queue_search(0)

    def requestFolderAccess_(self, sender):
        directory, label = FOLDER_ACCESS[sender.tag()]
        self.permission_message.setStringValue_(f"正在检查「{label}」访问权限…")

        def check():
            try:
                with os.scandir(os.path.expanduser("~/" + directory)):
                    return f"「{label}」已可读取，文件名搜索会查找此位置。"
            except OSError:
                return f"「{label}」无法读取，请在「文件与文件夹」设置中允许寻径访问。"

        pending = self.executor.submit(check)

        def update():
            self.permission_message.setStringValue_(pending.result())
            if self.window.isVisible() and self.search_field.stringValue().strip():
                self.queue_search(0)

        pending.add_done_callback(lambda result: None if result.cancelled() else A.NSOperationQueue.mainQueue().addOperationWithBlock_(update))

    def openFolderSettings_(self, _sender):
        self.open_privacy_setting("Privacy_FilesAndFolders")

    def openFullDiskSettings_(self, _sender):
        self.open_privacy_setting("Privacy_AllFiles")

    @objc.python_method
    def open_privacy_setting(self, page):
        url = NSURL.URLWithString_(f"x-apple.systempreferences:com.apple.preference.security?{page}")
        A.NSWorkspace.sharedWorkspace().openURL_(url)

    def requestAccessibility_(self, _sender):
        AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True})

    def requestInput_(self, _sender):
        if not Q.CGPreflightListenEventAccess():
            Q.CGRequestListenEventAccess()
        if not self.start_hotkey():
            self.open_privacy_setting("Privacy_ListenEvent")

    def toggleStrip_(self, _sender):
        self.prefs.defaults.setBool_forKey_(not self.prefs.enabled, "EnableFolderStrip")
        self.strip.update()

    def toggleFinder_(self, _sender):
        self.prefs.defaults.setBool_forKey_(not self.prefs.finder, "ShowInFinder")
        self.strip.update()

    def showSearch_(self, _sender):
        self.show()

    def showFolders_(self, _sender):
        self.show("folder")

    @objc.python_method
    def toggle_search(self):
        if self.permissions_window and self.permissions_window.isKeyWindow():
            self.permissions_window.orderOut_(None)
        if self.window.isVisible() and self.window.isKeyWindow():
            if self.strip.context:
                self.close_search()
            else:
                self.scope = "file" if self.scope == "all" else "all"
                self.segments.setSelectedSegment_(SCOPES.index(self.scope))
                self.queue_search(0)
        else:
            self.show()

    @objc.python_method
    def show(self, scope="all"):
        app = A.NSApplication.sharedApplication()
        current = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        if current is not None and current.processIdentifier() != os.getpid():
            self.return_to = current
        self.strip.hide()
        self.scope = scope
        self.segments.setSelectedSegment_(SCOPES.index(scope))
        self.segments.setEnabled_(self.strip.context is None)
        self.search_field.setPlaceholderString_("搜索文件夹并跳转" if self.strip.context else "搜索应用、文件或文件夹")
        self.search_field.setStringValue_("")
        app.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.search_field)
        self.queue_search(0)

    def windowShouldClose_(self, _window):
        self.close_search()
        return False

    @objc.python_method
    def close_search(self, return_focus=True):
        context = self.strip.context
        self.strip.context = None
        if self.preview and self.preview.isVisible():
            self.preview.orderOut_(None)
        self.window.orderOut_(None)
        if return_focus and context and context.kind == "local-picker":
            context.app.makeKeyAndOrderFront_(None)
        elif return_focus and self.return_to:
            app = A.NSApplication.sharedApplication()
            if app.respondsToSelector_("yieldActivationToApplication:"):
                app.yieldActivationToApplication_(self.return_to)
            self.return_to.activateWithOptions_(0)

    def changeScope_(self, sender):
        self.scope = SCOPES[sender.selectedSegment()]
        self.queue_search(0)
        self.window.makeFirstResponder_(self.search_field)

    def controlTextDidChange_(self, _notification):
        self.queue_search(0.15)

    @objc.python_method
    def key_event(self, event):
        key = event.keyCode()
        modifiers = event.modifierFlags() & (A.NSEventModifierFlagCommand | A.NSEventModifierFlagControl | A.NSEventModifierFlagOption | A.NSEventModifierFlagShift)
        editor = self.search_field.currentEditor()
        if editor is not None and editor.hasMarkedText():
            return False
        if modifiers == 0:
            if key == 53:
                self.close_search()
            elif key in (36, 76):
                self.openSelection_(None)
            elif key in (125, 126):
                self.select_delta(1 if key == 125 else -1)
            else:
                return False
        elif modifiers == A.NSEventModifierFlagCommand:
            if key in (36, 76):
                self.revealSelection_(None)
            elif key == 8 and self.window.firstResponder() != editor:
                self.copySelection_(None)
            elif key == 5:
                self.jumpSelection_(None)
            elif key == 35:
                self.previewSelection_(None)
            else:
                return False
        elif modifiers == A.NSEventModifierFlagControl and key in (35, 45):
            self.select_delta(-1 if key == 35 else 1)
        else:
            return False
        return True

    @objc.python_method
    def queue_search(self, wait):
        self.generation += 1
        if self.debounce:
            self.debounce.invalidate()
            self.timers.discard(self.debounce)
            self.debounce = None
        if self.pending:
            self.pending.cancel()
        self.scope_label.setStringValue_(self.prefs.root or "Spotlight 和常用文件夹")
        self.footer.setStringValue_("正在搜索…")
        self.debounce = self.after(wait, self.start_search)

    @objc.python_method
    def start_search(self):
        self.debounce = None
        generation = self.generation
        query = self.search_field.stringValue()
        scope = self.scope
        self.pending = self.submit_search(query, scope)
        self.pending.add_done_callback(lambda pending: None if pending.cancelled() else A.NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: self.display_search(generation, pending)))

    @objc.python_method
    def submit_search(self, query, scope):
        return self.executor.submit(run_search, query, scope, self.prefs.root, self.prefs.favorites, self.prefs.recent)

    @objc.python_method
    def display_search(self, generation, pending):
        if self.generation != generation:
            return
        try:
            self.results, inaccessible = pending.result()
        except (OSError, ValueError):
            self.results, inaccessible = [], set()
        self.displayed_generation = generation
        self.table.reloadData()
        if self.results:
            self.table.selectRowIndexes_byExtendingSelection_(A.NSIndexSet.indexSetWithIndex_(0), False)
            self.table.scrollRowToVisible_(0)
            status = f"{len(self.results)} 项 · ↵ 跳转" if self.strip.context else f"{len(self.results)} 项 · ↵ 打开 · ⌘↵ 在 Finder 显示 · ⌘P 快速预览"
        else:
            status = "没有找到匹配结果"
        if inaccessible:
            names = "、".join(self.folder_name(path) for path in sorted(inaccessible))
            count = f"{len(self.results)} 项 · " if self.results else ""
            status = f"{count}无法读取「{names}」，请在菜单栏打开权限设置。"
        self.footer.setStringValue_(status)

    def numberOfRowsInTableView_(self, _table):
        return len(self.results)

    def tableView_viewForTableColumn_row_(self, table, _column, index):
        result = self.results[index]
        view = table.makeViewWithIdentifier_owner_("row", self)
        if view is None:
            view = A.NSTableCellView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 634, 46))
            view.setIdentifier_("row")
            image = A.NSImageView.alloc().initWithFrame_(A.NSMakeRect(10, 8, 30, 30))
            title = A.NSTextField.labelWithString_("")
            title.setFrame_(A.NSMakeRect(51, 24, 490, 19))
            title.setFont_(A.NSFont.systemFontOfSize_weight_(14, A.NSFontWeightSemibold))
            subtitle = A.NSTextField.labelWithString_("")
            subtitle.setFrame_(A.NSMakeRect(51, 5, 555, 17))
            subtitle.setFont_(A.NSFont.systemFontOfSize_(11))
            subtitle.setTextColor_(A.NSColor.secondaryLabelColor())
            subtitle.setLineBreakMode_(A.NSLineBreakByTruncatingMiddle)
            for item in (image, title, subtitle):
                view.addSubview_(item)
            view.setImageView_(image)
            view.setTextField_(title)
        view.imageView().setImage_(A.NSWorkspace.sharedWorkspace().iconForFile_(result.path))
        view.textField().setStringValue_(self.folder_name(result.path) if result.kind == "folder" else result.name)
        view.subviews()[2].setStringValue_(f"{TYPE_NAMES[result.kind]}  ·  {result.path}")
        return view

    @objc.python_method
    def select_delta(self, change):
        if self.results:
            index = min(max(0, self.table.selectedRow() + change), len(self.results) - 1)
            self.table.selectRowIndexes_byExtendingSelection_(A.NSIndexSet.indexSetWithIndex_(index), False)
            self.table.scrollRowToVisible_(index)

    @objc.python_method
    def selected(self):
        row = self.table.selectedRow()
        return self.results[row] if self.displayed_generation == self.generation and 0 <= row < len(self.results) else None

    @objc.python_method
    def result_menu(self):
        result = self.selected()
        menu = A.NSMenu.alloc().initWithTitle_("操作")
        if not result:
            return menu
        self.add_item(menu, "跳转到此文件夹" if self.strip.context else "打开", "openSelection:")
        if result.kind == "folder" and not self.strip.context and self.return_to and AX.AXIsProcessTrusted():
            self.add_item(menu, "跳转到原窗口内的文件夹", "jumpSelection:", "g")
        self.add_item(menu, "在 Finder 显示", "revealSelection:")
        self.add_item(menu, "复制路径", "copySelection:")
        self.add_item(menu, "快速预览", "previewSelection:", "p")
        if result.kind == "folder":
            title = "从常用文件夹移除" if result.path in self.prefs.favorites else "加入常用文件夹"
            self.add_item(menu, title, "favoriteSelection:")
        return menu

    def showActions_(self, sender):
        menu = self.result_menu()
        menu.popUpMenuPositioningItem_atLocation_inView_(None, A.NSMakePoint(0, 0), sender)

    def openSelection_(self, _sender):
        result = self.selected()
        if result:
            if self.strip.context and result.kind == "folder":
                context = self.strip.context
                self.close_search()
                self.strip.jump(context, result.path)
            else:
                self.open_path(result.path)

    def jumpSelection_(self, _sender):
        result = self.selected()
        if not result or result.kind != "folder":
            return
        target = self.strip.context
        if target is None and self.return_to and AX.AXIsProcessTrusted():
            target = focused_target(self.return_to, True)
        if target:
            self.close_search()
            self.strip.jump(target, result.path)
        else:
            self.footer.setStringValue_("请从 Finder 或标准打开／存储对话框使用此操作")

    @objc.python_method
    def open_path(self, path):
        if A.NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(path)):
            self.remember(path)
            self.close_search(return_focus=False)
        else:
            self.notify("无法打开该项目")

    def revealSelection_(self, _sender):
        result = self.selected()
        if result:
            A.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(result.path)])
            self.close_search(return_focus=False)

    def copySelection_(self, _sender):
        result = self.selected()
        if result:
            self.copy(result.path)
            self.footer.setStringValue_("已复制路径")

    @objc.python_method
    def copy(self, path):
        board = A.NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(path, A.NSPasteboardTypeString)

    def previewSelection_(self, _sender):
        result = self.selected()
        if result:
            self.preview_url = NSURL.fileURLWithPath_(result.path)
            if not self.preview:
                self.preview = Q.QLPreviewPanel.sharedPreviewPanel()
            self.preview.setDataSource_(self)
            self.preview.reloadData()
            self.preview.makeKeyAndOrderFront_(None)

    def numberOfPreviewItemsInPreviewPanel_(self, _panel):
        return int(hasattr(self, "preview_url"))

    def previewPanel_previewItemAtIndex_(self, _panel, _index):
        return self.preview_url

    @objc.python_method
    def remember(self, path):
        self.prefs.remember(path)
        if not path.lower().endswith(".app"):
            self.prefs.record_folder(path if os.path.isdir(path) else os.path.dirname(path))

    def favoriteSelection_(self, _sender):
        result = self.selected()
        if result and result.kind == "folder":
            add = result.path not in self.prefs.favorites
            self.prefs.set_favorite(result.path, add)
            self.footer.setStringValue_("已加入常用文件夹" if add else "已移除常用文件夹")
            self.strip.update()

    def openFavorite_(self, sender):
        self.open_path(sender.representedObject())

    def removeFavorite_(self, sender):
        self.prefs.set_favorite(sender.representedObject(), False)
        self.strip.update()

    @objc.python_method
    def pick_folder(self, prompt, accepted, cancelled=None):
        if self.active_picker:
            self.active_picker.makeKeyAndOrderFront_(None)
            return self.active_picker
        picker = A.NSOpenPanel.openPanel()
        picker.setCanChooseFiles_(False)
        picker.setCanChooseDirectories_(True)
        picker.setAllowsMultipleSelection_(False)
        picker.setPrompt_(prompt)
        self.active_picker = picker

        def complete(result):
            self.active_picker = None
            self.strip.hide()
            if result == A.NSModalResponseOK:
                accepted(picker.URL().path())
            elif cancelled is not None:
                cancelled()

        A.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        picker.beginWithCompletionHandler_(complete)
        return picker

    def addFavorite_(self, _sender):
        self.pick_folder("加入常用文件夹", lambda path: self.prefs.set_favorite(path, True))

    def chooseRoot_(self, _sender):
        def apply(path):
            self.prefs.defaults.setObject_forKey_(path, "SearchRoot")
            if self.window.isVisible():
                self.queue_search(0)
        self.pick_folder("设为搜索范围", apply)

    def clearRoot_(self, _sender):
        self.prefs.defaults.removeObjectForKey_("SearchRoot")
        if self.window.isVisible():
            self.queue_search(0)

    @objc.python_method
    def folder_name(self, path):
        if path == os.path.expanduser("~"):
            return "个人文件夹"
        return A.NSFileManager.defaultManager().displayNameAtPath_(path) or os.path.basename(path) or path

    @objc.python_method
    def notify(self, message):
        if self.window.isVisible():
            self.footer.setStringValue_(message)
            return
        self.notice = FolderPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, 410, 46), A.NSWindowStyleMaskBorderless | A.NSWindowStyleMaskNonactivatingPanel,
            A.NSBackingStoreBuffered, False,
        )
        self.notice.setLevel_(A.NSFloatingWindowLevel)
        self.notice.setBackgroundColor_(A.NSColor.windowBackgroundColor())
        self.notice.setHasShadow_(True)
        label = A.NSTextField.labelWithString_(message)
        label.setFrame_(A.NSMakeRect(12, 10, 386, 23))
        label.setAlignment_(A.NSTextAlignmentCenter)
        self.notice.contentView().addSubview_(label)
        self.notice.center()
        self.notice.orderFrontRegardless()
        panel = self.notice
        self.after(2.5, lambda: panel.orderOut_(None))

    @objc.python_method
    def check_native_ui(self):
        if not self.window.isVisible() or not self.results:
            raise RuntimeError("The launcher failed to display initial results")
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "开发配置文件" / "开发相关.md"
            expected.parent.mkdir()
            expected.touch()
            results, _ = run_search("开发相关", "file", directory, [], [])
            if not results or results[0].path != str(expected):
                raise RuntimeError("The local search failed to return the Chinese filename")
        self.showPermissions_(None)
        if not self.permissions_window.isVisible():
            raise RuntimeError("The permission setup window failed to open")
        self.closePermissions_(None)
        self.window.orderOut_(None)
        folders = tempfile.TemporaryDirectory()
        recent = Path(folders.name) / "上次打开的目录"
        result = Path(folders.name) / "测试搜索目录"
        alternative = Path(folders.name) / "备用测试搜索目录"
        recent.mkdir()
        result.mkdir()
        alternative.mkdir()
        application = Path(folders.name) / "测试搜索目录.app"
        application.mkdir()
        self.prefs.defaults.setObject_forKey_(str(application), "LastFolder")
        self.prefs.defaults.setObject_forKey_([str(application), str(recent)], "Recent")
        self.prefs.defaults.setObject_forKey_(folders.name, "SearchRoot")
        picker = self.pick_folder("选择", lambda _path: None)
        picker.setDirectoryURL_(NSURL.fileURLWithPath_("/System"))

        def click_recent():
            self.strip.update()
            if not self.strip.panel.isVisible() or self.strip.choice != str(recent):
                raise RuntimeError("The folder strip failed to show the last folder")
            self.strip.row.performClick_(None)
            self.after(0.5, query_folder)

        def query_folder():
            if not picker.isVisible() or os.path.realpath(picker.directoryURL().path()) != os.path.realpath(recent):
                raise RuntimeError("The recent-folder row failed to navigate")
            self.strip.update()
            self.strip.field.setStringValue_("测试搜索目录")
            self.strip.search_changed()
            self.after(2.0, click_match)

        def click_match():
            if self.strip.choice != str(result):
                raise RuntimeError("The inline folder search failed to show the matching folder")
            menu = self.strip.choices_menu()
            options = {str(item.representedObject()): item for item in menu.itemArray()}
            if set(options) != {str(result), str(alternative)}:
                raise RuntimeError("The dropdown failed to show both folders while excluding the app")
            self.strip.chooseMenuFolder_(options[str(alternative)])
            self.after(0.5, verify)

        def verify():
            if not picker.isVisible() or os.path.realpath(picker.directoryURL().path()) != os.path.realpath(alternative):
                raise RuntimeError("The alternative dropdown result failed to navigate in the same open panel")
            folders.cleanup()
            print("Native UI OK: launcher, folder-only history, multiple dropdown results, and alternate folder jump.", flush=True)
            A.NSApplication.sharedApplication().terminate_(None)

        self.after(0.8, click_recent)


def run_search(query, scope, root, favorites, recent):
    with objc.autorelease_pool():
        inaccessible = set()
        results = search(
            query, scope, root, list(discover_apps()),
            [classify(path) for path in favorites if os.path.isdir(path)],
            [classify(path) for path in recent if os.path.exists(path)],
            local_roots=[os.path.expanduser("~/" + name) for name, _ in FOLDER_ACCESS],
            inaccessible=inaccessible,
        )
        return results, inaccessible


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-self-test", action="store_true", help=argparse.SUPPRESS)
    flags, _remaining = parser.parse_known_args()
    if flags.self_test:
        assert Q.CGEventCreateKeyboardEvent(None, 5, True) is not None
        assert search("Finder", "app", None, list(discover_apps()), [], [classify("/System/Library/CoreServices/Finder.app")])[0].kind == "app"
        print(f"SeekBar startup OK. Input Monitoring available: {bool(Q.CGPreflightListenEventAccess())}. Accessibility trusted: {bool(AX.AXIsProcessTrusted())}.", flush=True)
        return
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)
    controller = Controller.alloc().init()
    app.setDelegate_(controller)
    app.run()


if __name__ == "__main__":
    main()
