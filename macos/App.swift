import AppKit
import Foundation

final class App: NSObject, NSApplicationDelegate {
    var status: NSStatusItem!
    var process: Process?
    let root = Bundle.main.resourceURL!
    var data: URL!
    var log: FileHandle?
    var stopping = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let defaults = UserDefaults.standard
        let preferred = Bundle.main.object(forInfoDictionaryKey: "ExistingWorkspace") as? String ?? ""
        let appData = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Diesheng", isDirectory: true)
        let chosen = defaults.string(forKey: "workspace") ?? (FileManager.default.fileExists(atPath: preferred + "/server/workspace.sqlite3") ? preferred : appData.path)
        defaults.set(chosen, forKey: "workspace")
        data = URL(fileURLWithPath: chosen, isDirectory: true)
        do { try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700]) }
        catch { alert("无法打开数据目录", error.localizedDescription); NSApp.terminate(nil); return }
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let icon = NSImage(contentsOf: root.appendingPathComponent("status.png")) {
            icon.size = NSSize(width: 20, height: 20)
            icon.isTemplate = true
            status.button?.image = icon
            status.button?.toolTip = "叠声"
            status.button?.setAccessibilityLabel("叠声")
        } else { status.button?.title = "叠声" }
        updateMenu("正在启动…")
        start()
    }
    func updateMenu(_ state: String) {
        let menu = NSMenu()
        let label = NSMenuItem(title: state, action: nil, keyEquivalent: "")
        label.isEnabled = false; menu.addItem(label); menu.addItem(.separator())
        add(menu, "打开工作区", #selector(openWorkspace), "o")
        add(menu, "复制局域网地址", #selector(copyAddress), "")
        add(menu, "复制群主密钥", #selector(copyKey), "")
        add(menu, "打开数据目录", #selector(openData), "")
        add(menu, "查看服务日志", #selector(openLog), "")
        add(menu, "重新启动服务", #selector(restart), "r")
        menu.addItem(.separator()); add(menu, "退出叠声", #selector(quit), "q")
        status.menu = menu
    }
    func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key); item.target = self; menu.addItem(item)
    }
    func start() {
        stopping = false
        let file = data.appendingPathComponent("server.log")
        if !FileManager.default.fileExists(atPath: file.path) { FileManager.default.createFile(atPath: file.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
        do {
            log = try FileHandle(forWritingTo: file); try log?.seekToEnd()
            let task = Process()
            task.executableURL = root.appendingPathComponent("python/bin/python3.11")
            task.arguments = ["-u", "-m", "agent_group_chat", "--data-dir", data.path, "serve", "--lan"]
            task.currentDirectoryURL = root.appendingPathComponent("app")
            var env = ProcessInfo.processInfo.environment
            env["PYTHONHOME"] = root.appendingPathComponent("python").path
            env["PYTHONPATH"] = root.appendingPathComponent("app").path
            env["PYTHONNOUSERSITE"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            task.environment = env; task.standardOutput = log; task.standardError = log
            task.terminationHandler = { [weak self] finished in DispatchQueue.main.async {
                guard let self = self, self.process === finished else { return }
                self.process = nil; self.log?.closeFile(); self.log = nil
                if !self.stopping { self.updateMenu("服务已停止"); self.alert("服务未能运行", "请检查网络连接或 7810 端口是否已被占用。可从菜单栏查看服务日志。") }
            }}
            process = task; try task.run()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                guard let self = self, self.process?.isRunning == true else { return }
                self.updateMenu("服务运行中 · 端口 7810")
                self.openWorkspace()
            }
        } catch { process = nil; updateMenu("启动失败"); alert("启动失败", error.localizedDescription) }
    }
    @objc func restart() { if let task = process, task.isRunning { stopping = true; task.terminate(); task.waitUntilExit(); process = nil }; log?.closeFile(); log = nil; start() }
    @objc func openWorkspace() { NSWorkspace.shared.open(URL(string: "http://127.0.0.1:7810/#owner")!) }
    @objc func copyAddress() {
        let output = (try? String(contentsOf: data.appendingPathComponent("server.log"), encoding: .utf8)) ?? ""
        let address = output.components(separatedBy: .newlines).reversed().first(where: { $0.contains("叠声已启动") })?.components(separatedBy: "http://").last
        guard let value = address, value.contains(":") else { alert("暂时没有局域网地址", "请检查服务状态和网络连接。") ; return }
        copy("http://" + value.trimmingCharacters(in: .whitespaces))
    }
    @objc func copyKey() { if let key = try? String(contentsOf: data.appendingPathComponent("owner.key"), encoding: .utf8) { copy(key.trimmingCharacters(in: .whitespacesAndNewlines)) } else { alert("无法读取群主密钥", "请检查服务是否已成功启动。") } }
    func copy(_ value: String) { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(value, forType: .string) }
    @objc func openData() { NSWorkspace.shared.open(data) }
    @objc func openLog() { NSWorkspace.shared.open(data.appendingPathComponent("server.log")) }
    @objc func quit() { NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) { stopping = true; if let task = process, task.isRunning { task.terminate(); task.waitUntilExit() }; log?.closeFile() }
    func alert(_ title: String, _ message: String) { let a = NSAlert(); a.messageText = title; a.informativeText = message; a.runModal() }
}
let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
