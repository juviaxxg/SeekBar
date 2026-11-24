import os
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import AppKit as A
import ApplicationServices as AX
import Quartz as Q
import objc
from Foundation import NSBundle, NSObject, NSOperationQueue, NSURL

from seekbar.search import is_navigable_folder


FINDER = "com.apple.finder"
WPS = "com.kingsoft.wpsoffice.mac"
DIALOG_TITLES = {"open", "open file", "save", "save as", "choose folder", "select folder", "另存为", "打开", "打开文件", "保存", "选择文件夹", "存储", "存储为", "选取文件夹"}
CANCEL_TITLES = {"cancel", "取消", "キャンセル", "annuler", "abbrechen", "cancelar"}
WPS_NEW_FOLDER = {"新建文件夹", "new folder"}
WPS_CLOUD = {"我的云文档", "共享文件夹", "my cloud documents", "shared folders"}
PANEL_WIDTH = 398
PANEL_HEIGHT = 82
DIALOG_BUTTONS = {
    "open", "save", "choose", "select", "export", "import", "attach",
    "打开", "存储", "保存", "选取", "选择", "导出", "导入", "附加",
    "開く", "保存", "ouvrir", "enregistrer", "öffnen", "sichern", "abrir", "guardar",
}
PICKER_CONTROLS = {"AXBrowser", "AXOutline", "AXList", "AXTable", "AXCollection", "AXPopUpButton"}
TEXT_CONTROLS = {"AXTextField", "AXComboBox", "AXSearchField", "AXTextArea"}


def brand_icon(frame):
    path = NSBundle.mainBundle().pathForResource_ofType_("SeekBar", "icns")
    if not path:
        path = str(Path(__file__).resolve().parent.parent / "assets" / "SeekBar.icns")
    image = A.NSImage.alloc().initWithContentsOfFile_(path)
    view = A.NSImageView.alloc().initWithFrame_(frame)
    view.setImageScaling_(A.NSImageScaleProportionallyDown)
    view.setImage_(image)
    return view


def value(element, attribute):
    if element is None:
        return None
    error, result = AX.AXUIElementCopyAttributeValue(element, attribute, None)
    return result if error == AX.kAXErrorSuccess else None


def label(element):
    return str(value(element, "AXTitle") or value(element, "AXDescription") or "").casefold().strip(" ….…")


def descendants(element, limit=112, max_depth=5):
    children = deque((child, 1) for child in (value(element, "AXChildren") or ()))
    for _ in range(limit):
        if not children:
            break
        child, depth = children.popleft()
        yield child
        if depth < max_depth:
            children.extend((item, depth + 1) for item in (value(child, "AXChildren") or ()))


def file_dialog_kind(element, bundle=None):
    identifier = str(value(element, "AXIdentifier") or "").lower()
    if any(marker in identifier for marker in ("openpanel", "savepanel", "open-panel", "save-panel", "filepicker", "file-pick")):
        return "dialog"
    role = value(element, "AXRole")
    if role not in ("AXSheet", "AXDialog", "AXWindow"):
        return None
    title = label(element)
    default = label(value(element, "AXDefaultButton"))
    if default not in DIALOG_BUTTONS and title not in DIALOG_TITLES:
        return None
    primary = default in DIALOG_BUTTONS
    cancel = value(element, "AXCancelButton") is not None
    chooser = False
    wps_toolbar = False
    for child in descendants(element):
        child_role = value(child, "AXRole")
        chooser |= child_role in PICKER_CONTROLS
        if child_role == "AXButton":
            name = label(child)
            primary |= name in DIALOG_BUTTONS
            cancel |= name in CANCEL_TITLES
            wps_toolbar |= bundle == WPS and name in WPS_NEW_FOLDER
        if primary and cancel and chooser and (wps_toolbar or bundle != WPS):
            return "wps-dialog" if wps_toolbar else "dialog"
    return "dialog" if primary and cancel and chooser else None


def geometry(element):
    position = value(element, "AXPosition")
    size = value(element, "AXSize")
    if position is None or size is None:
        return None
    position_ok, point = AX.AXValueGetValue(position, AX.kAXValueCGPointType, None)
    size_ok, dimensions = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
    if not position_ok or not size_ok or dimensions.width <= 0 or dimensions.height <= 0:
        return None
    return point.x, point.y, dimensions.width, dimensions.height


@dataclass
class Target:
    pid: int
    app: object
    element: object
    frame: tuple
    kind: str


def local_picker_target(picker):
    screens = A.NSScreen.screens()
    if not screens:
        return None
    frame = picker.frame()
    top = screens[0].frame().size.height - frame.origin.y - frame.size.height
    return Target(os.getpid(), picker, None, (frame.origin.x, top, frame.size.width, frame.size.height), "local-picker")


def focused_target(running_app, finder_enabled):
    pid = running_app.processIdentifier()
    ax_app = AX.AXUIElementCreateApplication(pid)
    AX.AXUIElementSetMessagingTimeout(ax_app, 0.12)
    window = value(ax_app, "AXFocusedWindow") or value(ax_app, "AXMainWindow")
    if window is None or value(window, "AXMinimized"):
        return None
    sheets = list(value(window, "AXSheets") or ())
    if not sheets:
        sheets = [child for child in (value(window, "AXChildren") or ()) if value(child, "AXRole") == "AXSheet"]
    bundle = running_app.bundleIdentifier()
    for element in (*sheets, window):
        kind = file_dialog_kind(element, bundle)
        if kind:
            frame = geometry(element)
            return Target(pid, ax_app, element, frame, kind) if frame else None
    if not sheets and finder_enabled and running_app.bundleIdentifier() == FINDER and value(window, "AXSubrole") == "AXStandardWindow":
        frame = geometry(window)
        return Target(pid, ax_app, window, frame, "finder") if frame else None
    return None


class FolderPanel(A.NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class FolderSearchPanel(FolderPanel):
    owner = None

    def canBecomeKeyWindow(self):
        return True

    def sendEvent_(self, event):
        if self.owner and event.type() == A.NSEventTypeKeyDown and self.owner.key_event(event):
            return
        objc.super(FolderSearchPanel, self).sendEvent_(event)


class FolderSearchField(A.NSSearchField):
    def acceptsFirstMouse_(self, _event):
        return True

    def needsPanelToBecomeKey(self):
        return True


class FolderButton(A.NSButton):
    def acceptsFirstMouse_(self, event):
        return True


class FolderStrip(NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(FolderStrip, self).init()
        if self is None:
            return None
        self.owner = owner
        self.target = None
        self.context = None
        self.displayed = None
        self.jumping = False
        self.generation = 0
        self.pending = None
        self.results = []
        self.highlight = 0
        self.choice = None
        self.panel = FolderSearchPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT),
            A.NSWindowStyleMaskBorderless | A.NSWindowStyleMaskNonactivatingPanel,
            A.NSBackingStoreBuffered, False,
        )
        self.panel.owner = self
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        self.panel.setFloatingPanel_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setLevel_(A.NSFloatingWindowLevel)
        self.panel.setCollectionBehavior_(A.NSWindowCollectionBehaviorMoveToActiveSpace | A.NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(A.NSColor.clearColor())
        self.panel.setHasShadow_(True)
        background = A.NSVisualEffectView.alloc().initWithFrame_(A.NSMakeRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT))
        background.setMaterial_(A.NSVisualEffectMaterialMenu)
        background.setBlendingMode_(A.NSVisualEffectBlendingModeBehindWindow)
        background.setState_(A.NSVisualEffectStateActive)
        background.setWantsLayer_(True)
        background.layer().setCornerRadius_(10)
        background.layer().setMasksToBounds_(True)
        self.panel.setContentView_(background)
        background.addSubview_(brand_icon(A.NSMakeRect(10, 48, 23, 23)))
        self.field = FolderSearchField.alloc().initWithFrame_(A.NSMakeRect(38, 46, PANEL_WIDTH - 48, 27))
        self.field.setPlaceholderString_("搜索本地文件夹")
        self.field.setAccessibilityLabel_("搜索本地文件夹")
        self.field.setDelegate_(self)
        background.addSubview_(self.field)
        self.row = FolderButton.buttonWithTitle_target_action_("", self, "choose:")
        self.row.setFrame_(A.NSMakeRect(11, 9, PANEL_WIDTH - 62, 28))
        self.row.setBordered_(False)
        self.row.setAlignment_(A.NSTextAlignmentLeft)
        self.row.setImage_(A.NSImage.imageWithSystemSymbolName_accessibilityDescription_("folder", None))
        self.row.setImagePosition_(A.NSImageLeft)
        self.row.cell().setLineBreakMode_(A.NSLineBreakByTruncatingTail)
        background.addSubview_(self.row)
        self.dropdown = FolderButton.buttonWithTitle_target_action_("⌄", self, "showChoices:")
        arrow = A.NSImage.imageWithSystemSymbolName_accessibilityDescription_("chevron.down", None)
        if arrow is not None:
            self.dropdown.setImage_(arrow)
            self.dropdown.setImagePosition_(A.NSImageOnly)
        self.dropdown.setFrame_(A.NSMakeRect(PANEL_WIDTH - 43, 9, 33, 28))
        self.dropdown.setAccessibilityLabel_("更多文件夹")
        self.dropdown.setToolTip_("选择其他匹配、最近或常用文件夹")
        background.addSubview_(self.dropdown)
        return self

    @objc.python_method
    def hide(self):
        if self.panel.isVisible():
            self.panel.orderOut_(None)
        if self.target is not None:
            self.generation += 1
            if self.pending:
                self.pending.cancel()
        self.target = None
        self.displayed = None

    @objc.python_method
    def update(self):
        if self.context or self.jumping or not self.owner.prefs.enabled:
            self.hide()
            return
        current = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        if current is None:
            self.hide()
            return
        if current.processIdentifier() == os.getpid():
            if self.panel.isKeyWindow() and self.target is not None:
                return
            picker = self.owner.active_picker
            if picker is not None and picker.isVisible() and picker.isKeyWindow():
                target = local_picker_target(picker)
                if target:
                    self.show_for(target)
                    return
            self.hide()
            return
        if not AX.AXIsProcessTrusted():
            self.hide()
            return
        target = focused_target(current, self.owner.prefs.finder)
        if not target:
            self.hide()
            return
        if target.kind == "finder":
            document = value(target.element, "AXDocument") or value(target.element, "AXURL")
            if isinstance(document, str) and document.startswith("file:"):
                document = NSURL.URLWithString_(document)
            if document is not None and hasattr(document, "isFileURL") and document.isFileURL():
                self.owner.prefs.record_folder(document.path())
        self.show_for(target)

    @objc.python_method
    def show_for(self, target):
        previous = self.target
        recent = self.owner.prefs.last_folder
        signature = (target.pid, target.frame, recent)
        self.target = target
        if previous is None or (previous.pid, previous.element, previous.kind) != (target.pid, target.element, target.kind):
            self.generation += 1
            self.field.setStringValue_("")
            self.results = []
            self.highlight = 0
        if not self.field.stringValue().strip():
            self.set_choice(recent, "上次打开", "暂无最近打开的文件夹")
        if signature == self.displayed and self.panel.isVisible():
            return
        self.displayed = signature
        x, y, width, height = target.frame
        screens = A.NSScreen.screens()
        if not screens:
            return
        screen_top = screens[0].frame().size.height
        bottom = screen_top - y - height
        center = A.NSMakePoint(x + width / 2, bottom + height / 2)
        screen = next((item for item in screens if A.NSPointInRect(center, item.frame())), A.NSScreen.mainScreen())
        if screen is None:
            return
        visible = screen.visibleFrame()
        bar_width = self.panel.frame().size.width
        left = min(max(x + 7, visible.origin.x + 5), visible.origin.x + visible.size.width - bar_width - 5)
        origin_y = bottom - PANEL_HEIGHT - 6
        if origin_y < visible.origin.y:
            origin_y = min(bottom + height + 6, visible.origin.y + visible.size.height - PANEL_HEIGHT)
        self.panel.setFrameOrigin_(A.NSMakePoint(left, origin_y))
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()

    @objc.python_method
    def set_choice(self, path, prefix="", empty="没有匹配的文件夹"):
        self.choice = path
        title = " · ".join(item for item in (prefix, self.owner.folder_name(path)) if item) if path else empty
        self.row.setTitle_(title)
        self.row.setToolTip_(path)
        self.row.setEnabled_(bool(path))

    def choose_(self, _sender):
        if self.target and self.choice:
            self.jump(self.target, self.choice)

    @objc.python_method
    def choices_menu(self):
        menu = A.NSMenu.alloc().initWithTitle_("选择文件夹")
        menu.setMinimumWidth_(240)
        if self.field.stringValue().strip():
            paths = [result.path for result in self.results]
        else:
            prefs = self.owner.prefs
            paths = [prefs.last_folder, *prefs.recent, *prefs.favorites]
        for path in dict.fromkeys(paths):
            if not is_navigable_folder(path):
                continue
            parent = os.path.dirname(path)
            name = self.owner.folder_name(path)
            if parent and parent != path:
                name += f"  ·  {self.owner.folder_name(parent)}"
            item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(name, "chooseMenuFolder:", "")
            item.setTarget_(self)
            item.setRepresentedObject_(path)
            item.setToolTip_(path)
            menu.addItem_(item)
        if self.target and self.target.kind != "local-picker":
            if menu.numberOfItems():
                menu.addItem_(A.NSMenuItem.separatorItem())
            browse = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("浏览其他文件夹…", "browseFolder:", "")
            browse.setTarget_(self)
            menu.addItem_(browse)
        if not menu.numberOfItems():
            empty = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("暂无可选文件夹", None, "")
            menu.addItem_(empty)
        return menu

    def showChoices_(self, _sender):
        if self.target:
            menu = self.choices_menu()
            menu.popUpMenuPositioningItem_atLocation_inView_(None, A.NSMakePoint(12, 9), self.panel.contentView())

    def chooseMenuFolder_(self, sender):
        if self.target:
            self.jump(self.target, sender.representedObject())

    def browseFolder_(self, _sender):
        target = self.target
        if target is None or target.kind == "local-picker":
            return
        self.hide()
        self.jumping = True

        def accept(path):
            self.jumping = False
            self.jump(target, path)

        def cancel():
            self.jumping = False
            self.activate_target(target.pid)

        self.owner.pick_folder("跳转到此文件夹", accept, cancelled=cancel)

    def controlTextDidChange_(self, _notice):
        self.search_changed()

    @objc.python_method
    def search_changed(self):
        self.generation += 1
        generation = self.generation
        query = self.field.stringValue().strip()
        self.results = []
        self.highlight = 0
        if self.pending:
            self.pending.cancel()
        if not query:
            self.set_choice(self.owner.prefs.last_folder, "上次打开", "暂无最近打开的文件夹")
            return
        self.set_choice(None, empty="正在搜索文件夹…")
        self.owner.after(0.16, lambda: self.start_search(generation, query))

    @objc.python_method
    def start_search(self, generation, query):
        if self.generation != generation or self.target is None:
            return
        self.pending = self.owner.submit_search(query, "folder")
        self.pending.add_done_callback(lambda future: None if future.cancelled() else NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: self.show_results(generation, future)))

    @objc.python_method
    def show_results(self, generation, future):
        if self.generation != generation or self.target is None:
            return
        try:
            results, inaccessible = future.result()
        except (OSError, ValueError):
            results, inaccessible = [], set()
        self.results = results
        self.highlight = 0
        if not results and inaccessible:
            self.set_choice(None, empty="文件夹访问受限，请检查寻径的权限设置")
        else:
            self.show_match()

    @objc.python_method
    def show_match(self):
        if not self.results:
            self.set_choice(None)
            return
        match = self.results[self.highlight]
        position = f"{self.highlight + 1}/{len(self.results)}" if len(self.results) > 1 else ""
        self.set_choice(match.path, position)

    @objc.python_method
    def key_event(self, event):
        flags = A.NSEventModifierFlagCommand | A.NSEventModifierFlagControl | A.NSEventModifierFlagOption | A.NSEventModifierFlagShift
        if event.modifierFlags() & flags:
            return False
        editor = self.field.currentEditor()
        if editor is not None and editor.hasMarkedText():
            return False
        key = event.keyCode()
        if key == 53:
            self.panel.resignKeyWindow()
            self.field.setStringValue_("")
            self.search_changed()
        elif key in (125, 126) and self.results:
            direction = 1 if key == 125 else -1
            self.highlight = (self.highlight + direction) % len(self.results)
            self.show_match()
        elif key in (36, 76):
            self.choose_(None)
        else:
            return False
        return True

    @objc.python_method
    def jump(self, target, path):
        if not is_navigable_folder(path):
            self.set_choice(None, empty="文件夹不可用")
            return
        self.hide()
        self.context = None
        if target.kind == "local-picker":
            if target.app.isVisible():
                target.app.setDirectoryURL_(NSURL.fileURLWithPath_(path))
                target.app.makeKeyAndOrderFront_(None)
                self.owner.remember(path)
            else:
                self.jump_failed(path)
            return
        self.jumping = True
        self.activate_target(target.pid)
        self.owner.after(0.19, lambda: self.begin_jump(target, path))

    @objc.python_method
    def activate_target(self, pid):
        previous = A.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if previous is not None and not previous.isActive():
            app = A.NSApplication.sharedApplication()
            if app.respondsToSelector_("yieldActivationToApplication:"):
                app.yieldActivationToApplication_(previous)
            previous.activateWithOptions_(0)

    @objc.python_method
    def begin_jump(self, target, path):
        current = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        if current is None or current.processIdentifier() != target.pid or not is_navigable_folder(path):
            self.jump_failed(path)
            return
        active = focused_target(current, True)
        if active is None or active.element != target.element:
            self.jump_failed(path)
            return
        if target.kind == "wps-dialog":
            self.begin_wps_jump(active, path)
            return
        old_input = value(target.app, "AXFocusedUIElement")
        send_key(5, Q.kCGEventFlagMaskCommand | Q.kCGEventFlagMaskShift)
        self.owner.after(0.12, lambda: self.fill_jump(target, path, old_input, 7))

    @objc.python_method
    def begin_wps_jump(self, target, path):
        items = list(descendants(target.element, max_depth=3))
        cloud = any(value(item, "AXRole") == "AXCheckBox" and label(item) in WPS_CLOUD for item in items)
        if cloud:
            # WPS uses a custom sidebar whose items don't expose their labels to VoiceOver.
            x, y, _, height = target.frame
            if height <= 260:
                self.jump_failed(path, target.kind)
                return
            click_at(x + 52, y + 236)
            self.owner.after(0.22, lambda: self.focus_wps_path(target, path))
        else:
            self.focus_wps_path(target, path)

    @objc.python_method
    def focus_wps_path(self, target, path):
        current = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        window = value(target.app, "AXFocusedWindow")
        if current is None or current.processIdentifier() != target.pid or window != target.element:
            self.jump_failed(path, target.kind)
            return
        new_folder = next((item for item in descendants(window, max_depth=3) if value(item, "AXRole") == "AXButton" and label(item) in WPS_NEW_FOLDER), None)
        frame = geometry(new_folder) if new_folder else None
        if frame is None:
            self.jump_failed(path, target.kind)
            return
        x, y, _, height = frame
        click_at(x - 48, y + height / 2)
        self.owner.after(0.14, lambda: self.enter_wps_path(target, path, x, y, height))

    @objc.python_method
    def enter_wps_path(self, target, path, toolbar_x, toolbar_y, toolbar_height):
        current = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        field = value(target.app, "AXFocusedUIElement")
        frame = geometry(field) if field else None
        if (current is None or current.processIdentifier() != target.pid or value(target.app, "AXFocusedWindow") != target.element
                or value(field, "AXRole") != "AXTextField" or frame is None
                or frame[0] >= toolbar_x or abs(frame[1] - toolbar_y) > max(18, toolbar_height)):
            self.jump_failed(path, target.kind)
            return
        error = AX.AXUIElementSetAttributeValue(field, "AXValue", path)
        if error != AX.kAXErrorSuccess or value(field, "AXValue") != path:
            self.jump_failed(path, target.kind)
            return
        if value(target.app, "AXFocusedUIElement") == field:
            send_key(36)
            self.owner.after(0.30, lambda: self.verify_wps_path(target, path))
        else:
            self.jump_failed(path, target.kind)

    @objc.python_method
    def verify_wps_path(self, target, path):
        names = {os.path.basename(path).casefold(), self.owner.folder_name(path).casefold()}
        if value(target.app, "AXFocusedWindow") == target.element and any(
            value(item, "AXRole") == "AXCheckBox" and label(item) in names for item in descendants(target.element, max_depth=3)
        ):
            self.jumping = False
            self.owner.remember(path)
        else:
            self.jump_failed(path, target.kind)

    @objc.python_method
    def fill_jump(self, target, path, old_input, attempts):
        current = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        if current is None or current.processIdentifier() != target.pid:
            self.jump_failed(path)
            return
        field = value(target.app, "AXFocusedUIElement")
        if field is None or field == old_input or value(field, "AXRole") not in TEXT_CONTROLS:
            if attempts:
                self.owner.after(0.12, lambda: self.fill_jump(target, path, old_input, attempts - 1))
            else:
                self.jump_failed(path)
            return
        send_key(0, Q.kCGEventFlagMaskCommand)
        for start in range(0, len(path), 10):
            send_key(0, text=path[start:start + 10])
        self.owner.after(0.30, lambda: self.finish_jump(target, path, field))

    @objc.python_method
    def finish_jump(self, target, path, field):
        current = A.NSWorkspace.sharedWorkspace().frontmostApplication()
        entered = value(field, "AXValue")
        still_focused = value(target.app, "AXFocusedUIElement") == field
        matched = isinstance(entered, str) and unicodedata.normalize("NFC", os.path.normpath(entered)) == unicodedata.normalize("NFC", os.path.normpath(path))
        if current is None or current.processIdentifier() != target.pid or not still_focused or not matched:
            self.jump_failed(path)
            return
        send_key(36)
        self.owner.after(0.45, lambda: self.verify_jump(target, path, field))

    @objc.python_method
    def verify_jump(self, target, path, field):
        if value(target.app, "AXFocusedUIElement") == field:
            self.jump_failed(path)
            return
        self.jumping = False
        self.owner.remember(path)

    @objc.python_method
    def jump_failed(self, path, kind=None):
        self.jumping = False
        self.owner.copy(path)
        message = "路径已复制。可在 WPS 左侧点「浏览」，再按 ⌘⇧G 粘贴。" if kind == "wps-dialog" else "路径已复制，可在原窗口按 ⌘⇧G 并粘贴。"
        self.owner.notify(message)


def send_key(keycode, flags=0, text=None):
    for down in (True, False):
        event = Q.CGEventCreateKeyboardEvent(None, keycode, down)
        Q.CGEventSetFlags(event, flags)
        if text and down:
            Q.CGEventKeyboardSetUnicodeString(event, len(text.encode("utf-16-le")) // 2, text)
        Q.CGEventPost(Q.kCGHIDEventTap, event)


def click_at(x, y):
    point = Q.CGPoint(x, y)
    for kind in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
        Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, kind, point, Q.kCGMouseButtonLeft))
