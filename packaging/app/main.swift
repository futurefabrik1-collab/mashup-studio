// Mashup Studio.app's executable (packaging/build-app.sh compiles it).
//
// Opening the app:
//   - if a Studio already answers on its port, it only opens the page;
//   - if the Python environment is missing, broken, or was made for other packages (the stamp is the app version and
//     the sha256 of the bundled requirements.lock), it sets it up with the bundled uv: Python 3.12, then exactly the
//     locked packages, hash-checked. A small window says which step, how far, how long, and the last thing uv said;
//     on a failure it says what went wrong and offers "Try again";
//   - then it starts the Studio (which opens in the browser) and quits. Quit in the page stops the Studio.
// Nothing is written inside the app. The environment, the logs, the models and the Studio's runs, briefs and cache
// all go to the data folder, ~/Library/Application Support/Mashup Studio. The bundled ffmpeg, ffprobe and rubberband
// come first on the Studio's PATH.
// Test hooks: MASHUP_STUDIO_PORT (default 8765), MASHUP_STUDIO_NO_BROWSER=1, MASHUP_STUDIO_HEADLESS=1 (no window:
// the same steps, with progress on stderr), and MF_CONFIG is passed through.
import AppKit
import CryptoKit

let env = ProcessInfo.processInfo.environment
let home = env["HOME"] ?? NSHomeDirectory()
let data = home + "/Library/Application Support/Mashup Studio"
// The app's own environment: not .../venv, which a source install (packaging/install.sh) makes and its `mf` prefers.
let venv = data + "/app-venv"
let python = venv + "/bin/python"
let stampFile = venv + "/.mashup-studio-stamp"
let resources = Bundle.main.resourcePath!
let bin = resources + "/bin"
let lockFile = resources + "/app/requirements.lock"
let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0"
let unpackedBytes = 1_000_000_000.0            // what uv's cache grows to while it fetches the packages (measured)
// Keep the port and the Server header in step with `mf app` (cli.py, app.py) and packaging/launcher/mashup-studio.
let port = env["MASHUP_STUDIO_PORT"] ?? "8765"
let pageURL = URL(string: "http://127.0.0.1:\(port)/")!
let path = bin + ":/usr/bin:/bin:/usr/sbin:/sbin"
// Passed on to uv and the Studio: proxies and certificates a network may need, and the test hooks.
let passOn = ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "MF_CONFIG"]
func baseEnv() -> [String: String] {
    var e = ["HOME": home, "PATH": path, "LANG": "en_US.UTF-8", "TMPDIR": env["TMPDIR"] ?? "/tmp"]
    for k in passOn { if let v = env[k] { e[k] = v } }
    return e
}

func tail(_ file: String, _ n: Int) -> String {
    let text = (try? String(contentsOfFile: file, encoding: .utf8)) ?? ""
    return text.split(separator: "\n").suffix(n).joined(separator: "\n")
}
func elapsed(_ since: Date) -> String {
    let s = Int(Date().timeIntervalSince(since))
    return s < 60 ? "\(s) s" : "\(s / 60) min \(s % 60) s"
}
func gigabytes(_ bytes: Double) -> String { String(format: "%.2f GB", bytes / 1e9) }
func appendLine(_ file: String, _ line: String) {
    if !FileManager.default.fileExists(atPath: file) { FileManager.default.createFile(atPath: file, contents: nil) }
    guard let h = FileHandle(forWritingAtPath: file) else { return }
    h.seekToEndOfFile(); h.write(Data((line + "\n").utf8)); try? h.close()
}

// ---------- The Studio ----------
func studioAnswers() -> Bool {
    var req = URLRequest(url: pageURL, timeoutInterval: 2)
    req.httpMethod = "HEAD"
    let done = DispatchSemaphore(value: 0)
    var ours = false
    URLSession.shared.dataTask(with: req) { _, response, _ in
        ours = ((response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Server") ?? "").hasPrefix("mf-app ")
        done.signal()
    }.resume()
    _ = done.wait(timeout: .now() + 3)
    return ours
}

// Starts the Studio and waits until it answers. Returns nil, or what went wrong.
func startStudio(_ say: (String) -> Void) -> String? {
    let log = data + "/studio.log"
    appendLine(log, "--- Mashup Studio \(version), \(Date())")
    guard let out = FileHandle(forWritingAtPath: log) else { return "Could not write \(log)." }
    out.seekToEndOfFile()
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = ["-m", "mashup_finder.cli", "app", "--port", port] + (env["MASHUP_STUDIO_NO_BROWSER"] != nil ? ["--no-browser"] : [])
    p.currentDirectoryURL = URL(fileURLWithPath: data)
    p.environment = baseEnv().merging(["PYTHONPATH": resources + "/app", "MF_DATA": data, "PYTHONPYCACHEPREFIX": data + "/pycache",
                                       "PYTHONUNBUFFERED": "1", "TORCH_HOME": data + "/models/torch",
                                       "HF_HOME": data + "/models/huggingface"]) { $1 }
    p.standardOutput = out
    p.standardError = out
    say("Starting Mashup Studio")
    do { try p.run() } catch { return "Could not start the Studio: \(error.localizedDescription)" }
    for _ in 0..<240 {                       // up to two minutes: the first start loads the audio libraries
        if studioAnswers() { return nil }
        if !p.isRunning { return "The Studio stopped while starting:\n" + tail(log, 6) + "\n\nThe full log is in \(log)." }
        Thread.sleep(forTimeInterval: 0.5)
    }
    return "The Studio did not answer on port \(port). Is another program using it?\n" + tail(log, 4)
}

// ---------- Setup with the bundled uv ----------
struct Step { let title: String; let args: [String]; let share: Double; let measured: Bool }
let uvEnv = baseEnv().merging(["UV_PYTHON_INSTALL_DIR": data + "/python", "UV_CACHE_DIR": data + "/uv-cache",
                               "UV_PYTHON_PREFERENCE": "only-managed", "UV_NO_CONFIG": "1", "NO_COLOR": "1"]) { $1 }
let steps = [
    Step(title: "Step 1 of 3: downloading Python 3.12", args: ["python", "install", "--no-bin", "3.12"], share: 0.05, measured: false),
    Step(title: "Step 2 of 3: making Mashup Studio's Python environment", args: ["venv", "--clear", "--python", "3.12", venv],
         share: 0.02, measured: false),
    Step(title: "Step 3 of 3: downloading the audio and AI packages (about 250 MB, the long one)",
         args: ["pip", "sync", "--python", python, "--require-hashes", lockFile], share: 0.93, measured: true),
]

final class Setup {
    var report: (_ title: String, _ fraction: Double, _ detail: String) -> Void = { _, _, _ in }
    private(set) var running: Process?
    private var lastLine = ""
    private let lock = NSLock()

    var stamp: String {
        let bytes = (try? Data(contentsOf: URL(fileURLWithPath: lockFile))) ?? Data()
        return version + " " + SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
    }

    func needed() -> Bool {
        let saved = (try? String(contentsOfFile: stampFile, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
        return saved != stamp || !FileManager.default.isExecutableFile(atPath: python)
    }

    // Runs every step; returns nil, or the error to show.
    func run() -> String? {
        try? FileManager.default.createDirectory(atPath: data, withIntermediateDirectories: true)
        let log = data + "/setup.log"
        appendLine(log, "--- Mashup Studio \(version) setup, \(Date())")
        let started = Date()
        var done = 0.0
        for step in steps {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: bin + "/uv")
            p.arguments = ["--system-certs"] + step.args
            p.environment = uvEnv
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            guard let file = FileHandle(forWritingAtPath: log) else { return "Could not write \(log)." }
            file.seekToEndOfFile()
            let note = { (chunk: Data) in
                file.write(chunk)
                if let s = String(data: chunk, encoding: .utf8), let line = s.split(whereSeparator: \.isNewline).last {
                    self.lock.lock(); self.lastLine = String(line).trimmingCharacters(in: .whitespaces); self.lock.unlock()
                }
            }
            pipe.fileHandleForReading.readabilityHandler = { h in note(h.availableData) }
            do { try p.run() } catch { return "Could not run the bundled uv: \(error.localizedDescription)" }
            running = p
            var tick = 0
            while p.isRunning {
                if tick % 6 == 0 {                                    // every 3 s: how far, how long, what uv said
                    var detail = elapsed(started)
                    var within = 0.5
                    if step.measured {
                        let got = Double(cacheBytes())
                        within = min(got / unpackedBytes, 0.97)
                        detail += " · \(gigabytes(got)) of about \(gigabytes(unpackedBytes)) unpacked"
                    }
                    lock.lock(); let line = lastLine; lock.unlock()
                    report(step.title, done + step.share * within, line.isEmpty ? detail : detail + "\n" + line)
                }
                tick += 1
                Thread.sleep(forTimeInterval: 0.5)
            }
            running = nil
            pipe.fileHandleForReading.readabilityHandler = nil
            note(pipe.fileHandleForReading.readDataToEndOfFile())     // uv's last lines, then the file is closed
            try? file.close()
            if p.terminationStatus != 0 {
                let why = tail(log, 3)
                let offline = ["dns", "connect", "network", "timed out"].contains { why.lowercased().contains($0) }
                return (offline ? "Setup needs the internet, and it could not reach the download servers.\n\n" : "Setup stopped.\n\n")
                    + why + "\n\nThe full log is in \(log)."
            }
            done += step.share
        }
        try? FileManager.default.removeItem(atPath: data + "/uv-cache")      // about 1 GB, no longer needed
        do { try stamp.write(toFile: stampFile, atomically: true, encoding: .utf8) } catch {
            return "Could not write \(stampFile): \(error.localizedDescription)"
        }
        report("Ready", 1, "Set up in \(elapsed(started)).")
        return nil
    }

    private func cacheBytes() -> Int {
        let du = Process()
        du.executableURL = URL(fileURLWithPath: "/usr/bin/du")
        du.arguments = ["-sk", data + "/uv-cache"]
        let pipe = Pipe()
        du.standardOutput = pipe
        du.standardError = FileHandle.nullDevice
        guard (try? du.run()) != nil else { return 0 }
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        du.waitUntilExit()
        return (Int(out.split(separator: "\t").first ?? "") ?? 0) * 1024
    }
}

// The whole open after the "already running" check, shared by the window and the headless hook: set up when needed,
// then start the Studio. Returns nil once the Studio answers, else the error to show.
func openStudio(_ setup: Setup, say: @escaping (String) -> Void) -> String? {
    // A mounted DMG and a translocated copy are read-only; an app in Applications on an external drive is not.
    let place = Bundle.main.bundlePath
    let readOnly = (try? Bundle.main.bundleURL.resourceValues(forKeys: [.volumeIsReadOnlyKey]))?.volumeIsReadOnly ?? false
    if readOnly || place.contains("/AppTranslocation/") {
        return "Mashup Studio is running from the disk image or a temporary copy.\n\nDrag it to Applications, eject the disk image, and open it from Applications."
    }
    if setup.needed(), let error = setup.run() { return error }
    return startStudio(say)
}

// ---------- Headless (tests): the same open, progress on stderr ----------
if env["MASHUP_STUDIO_HEADLESS"] != nil {
    let say = { (text: String) in FileHandle.standardError.write(Data((text + "\n").utf8)) }
    if studioAnswers() { print("Mashup Studio already answers on \(pageURL)"); exit(0) }
    let setup = Setup()
    setup.report = { title, fraction, detail in say("setup: \(Int(fraction * 100))% \(title) · \(detail.replacingOccurrences(of: "\n", with: " · "))") }
    if let error = openStudio(setup, say: say) { say("open failed: \(error)"); exit(1) }
    print("Mashup Studio answers on \(pageURL)")
    exit(0)
}

// ---------- The window: shown while setting up, or when something went wrong ----------
final class Window: NSObject, NSApplicationDelegate {
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 250), styleMask: [.titled, .closable],
                          backing: .buffered, defer: false)
    let heading = NSTextField(labelWithString: "")
    let step = NSTextField(wrappingLabelWithString: "")
    let bar = NSProgressIndicator()
    let spinner = NSProgressIndicator()
    let detail = NSTextField(wrappingLabelWithString: "")
    let again = NSButton(title: "Try again", target: nil, action: nil)
    let quit = NSButton(title: "Quit", target: nil, action: nil)
    let setup = Setup()

    func applicationDidFinishLaunching(_ note: Notification) {
        if studioAnswers() { NSWorkspace.shared.open(pageURL); NSApp.terminate(nil); return }
        open()
    }

    func open() {
        again.isHidden = true
        if setup.needed() {
            show()
            heading.stringValue = "Setting up Mashup Studio"
            step.stringValue = "The first open downloads what the Studio runs on. Later opens start straight away."
            detail.stringValue = ""
            bar.doubleValue = 0
            spinner.startAnimation(nil)
        }
        setup.report = { title, fraction, text in
            DispatchQueue.main.async { self.step.stringValue = title; self.bar.doubleValue = fraction; self.detail.stringValue = text }
        }
        DispatchQueue.global().async {
            let error = openStudio(self.setup) { text in DispatchQueue.main.async { self.step.stringValue = text } }
            DispatchQueue.main.async { if let error { self.fail(error) } else { NSApp.terminate(nil) } }
        }
    }

    func show() {
        if heading.superview == nil {                                  // built on first use
            window.title = "Mashup Studio"
            heading.font = .boldSystemFont(ofSize: 17)
            detail.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
            detail.textColor = .secondaryLabelColor
            detail.maximumNumberOfLines = 9
            bar.isIndeterminate = false; bar.minValue = 0; bar.maxValue = 1
            spinner.style = .spinning; spinner.controlSize = .small; spinner.isDisplayedWhenStopped = false
            again.target = self; again.action = #selector(tryAgain); again.keyEquivalent = "\r"
            quit.target = self; quit.action = #selector(stop)
            let row = NSStackView(views: [bar, spinner])
            let buttons = NSStackView(views: [NSView(), quit, again])
            let stack = NSStackView(views: [heading, step, row, detail, buttons])
            stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 12
            stack.edgeInsets = NSEdgeInsets(top: 20, left: 22, bottom: 18, right: 22)
            window.contentView = stack
            for v in [step, row, detail, buttons] { v.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -44).isActive = true }
            window.center()
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func fail(_ error: String) {
        show()
        spinner.stopAnimation(nil)
        heading.stringValue = "Mashup Studio could not start"
        step.stringValue = "Nothing is lost: try again once the problem is fixed."
        detail.stringValue = error
        again.isHidden = false
    }

    @objc func tryAgain() { open() }
    @objc func stop() { NSApp.terminate(nil) }
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }
    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool { show(); return true }
    func applicationWillTerminate(_ note: Notification) { setup.running?.terminate() }   // a setup in progress stops
}

let app = NSApplication.shared
let delegate = Window()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
