// Mashup Studio.app's executable (packaging/build-app.sh compiles it).
//
// Opening the app:
//   - if a Studio already answers on its port, it only opens the page (and, if another build of the app started that
//     Studio, says to quit it and try again, since it keeps serving that build's code);
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
// This build: the app version and the sha256 of its lock. The environment is stamped with it, and the Studio it starts
// names it in its Server header ("mf-app app/<stamp>", app.py).
let stamp = version + " " + SHA256.hash(data: (try? Data(contentsOf: URL(fileURLWithPath: lockFile))) ?? Data())
    .map { String(format: "%02x", $0) }.joined()
let build = "app/" + stamp
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
    _ = try? h.seekToEnd(); try? h.write(contentsOf: Data((line + "\n").utf8)); try? h.close()   // a full disk throws, never aborts
}

// ---------- The Studio ----------
// The answer of the Studio's port to one request, or nil when nothing answers there.
func ask(_ method: String, _ path: String, _ headers: [String: String] = [:]) -> HTTPURLResponse? {
    var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)\(path)")!, timeoutInterval: 2)
    req.httpMethod = method
    for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
    let done = DispatchSemaphore(value: 0)
    var answer: HTTPURLResponse?
    URLSession.shared.dataTask(with: req) { _, response, _ in answer = response as? HTTPURLResponse; done.signal() }.resume()
    _ = done.wait(timeout: .now() + 3)
    return answer
}

// How a Studio on the port names itself after "mf-app " in its Server header ("app/<stamp>" when an app started it,
// Python's version for `mf app`), or nil when no Studio answers.
func runningStudio() -> String? {
    let server = ask("HEAD", "/")?.value(forHTTPHeaderField: "Server") ?? ""
    return server.hasPrefix("mf-app ") ? String(server.dropFirst("mf-app ".count)) : nil
}


// Why the Studio was not opened: the window's heading and the details.
struct Stop { var heading = "Mashup Studio could not start"; let text: String }

// Starts the Studio and waits until it answers. Returns nil, or what went wrong.
func startStudio(_ say: (String) -> Void) -> String? {
    let log = data + "/studio.log"
    appendLine(log, "--- Mashup Studio \(version), \(Date())")
    guard let out = FileHandle(forWritingAtPath: log) else { return "Could not write \(log)." }
    _ = try? out.seekToEnd()
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = ["-m", "mashup_finder.cli", "app", "--port", port] + (env["MASHUP_STUDIO_NO_BROWSER"] != nil ? ["--no-browser"] : [])
    p.currentDirectoryURL = URL(fileURLWithPath: data)
    p.environment = baseEnv().merging(["PYTHONPATH": resources + "/app", "MF_DATA": data, "PYTHONPYCACHEPREFIX": data + "/pycache",
                                       "PYTHONUNBUFFERED": "1", "TORCH_HOME": data + "/models/torch",
                                       "HF_HOME": data + "/models/huggingface", "MASHUP_STUDIO_BUILD": build]) { $1 }
    p.standardOutput = out
    p.standardError = out
    say("Starting Mashup Studio")
    do { try p.run() } catch { return "Could not start the Studio: \(error.localizedDescription)" }
    for _ in 0..<240 {                       // up to two minutes: the first start loads the audio libraries
        if runningStudio() != nil { return nil }
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

typealias Report = (_ title: String, _ fraction: Double, _ detail: String) -> Void

final class Setup {
    private var current: Process?            // the uv step that is running; current and lastLine are under lock
    private var lastLine = ""
    private let lock = NSLock()

    func needed() -> Bool {
        let saved = (try? String(contentsOfFile: stampFile, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
        return saved != stamp || !FileManager.default.isExecutableFile(atPath: python)
    }

    func stop() { lock.lock(); current?.terminate(); lock.unlock() }

    // Runs every step; returns nil, or the error to show.
    func run(_ report: Report) -> String? {
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
            _ = try? file.seekToEnd()
            let note = { (chunk: Data) in
                try? file.write(contentsOf: chunk)
                if let s = String(data: chunk, encoding: .utf8), let line = s.split(whereSeparator: \.isNewline).last {
                    self.lock.lock(); self.lastLine = String(line).trimmingCharacters(in: .whitespaces); self.lock.unlock()
                }
            }
            pipe.fileHandleForReading.readabilityHandler = { h in note(h.availableData) }
            do { try p.run() } catch { return "Could not run the bundled uv: \(error.localizedDescription)" }
            lock.lock(); current = p; lock.unlock()
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
            lock.lock(); current = nil; lock.unlock()
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

// The whole open, shared by the window and the headless hook: the page of a Studio that already answers (openPage,
// false when it did not open it), else setup when needed, then the Studio. Returns nil once a Studio of this build (or
// a source install's) answers, else why not.
func openStudio(_ setup: Setup, report: Report, say: (String) -> Void, openPage: () -> Bool) -> Stop? {
    if let running = runningStudio() {
        say("Mashup Studio already answers on \(pageURL)")
        guard openPage() else {
            return Stop(heading: "Mashup Studio did not open its page",
                        text: "Something on port \(port) answers as Mashup Studio but did not open its page in time: a Studio from before this version, or another user's program.\n\nTo see whose it is, run lsof -nP -iTCP:\(port) -sTCP:LISTEN in Terminal. If it is yours, Quit it from its page or stop it, then Try again.")
        }
        guard running.hasPrefix("app/"), running != build else { return nil }
        let other = String(running.dropFirst("app/".count).split(separator: " ").first ?? "?")
        let who = other == version ? "Another build of version \(other) of the app" : "Version \(other) of the app"
        return Stop(heading: "Another version of Mashup Studio is running",
                    text: "\(who) started it, and it keeps running that build's code.\n\nSelect Quit in its page, then Try again here.")
    }
    // A mounted DMG and a translocated copy are read-only; an app in Applications on an external drive is not.
    let place = Bundle.main.bundlePath
    let readOnly = (try? Bundle.main.bundleURL.resourceValues(forKeys: [.volumeIsReadOnlyKey]))?.volumeIsReadOnly ?? false
    if readOnly || place.contains("/AppTranslocation/") {
        return Stop(text: "Mashup Studio is running from the disk image or a temporary copy.\n\nDrag it to Applications, eject the disk image, and open it from Applications.")
    }
    if setup.needed(), let error = setup.run(report) { return Stop(text: error) }
    return startStudio(say).map { Stop(text: $0) }
}

// ---------- Headless (tests): the same open, progress on stderr ----------
if env["MASHUP_STUDIO_HEADLESS"] != nil {
    let say = { (text: String) in _ = try? FileHandle.standardError.write(contentsOf: Data((text + "\n").utf8)) }
    let report: Report = { title, fraction, detail in
        say("setup: \(Int(fraction * 100))% \(title) · \(detail.replacingOccurrences(of: "\n", with: " · "))")
    }
    if let why = openStudio(Setup(), report: report, say: say, openPage: { true }) { say("open failed: \(why.heading). \(why.text)"); exit(1) }
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
    var settingUp = false                                              // this attempt has shown the setup window

    func applicationDidFinishLaunching(_ note: Notification) { open() }

    // Opens in the background; the window shows when setup first reports, and on a failure.
    func open() {
        again.isHidden = true
        settingUp = false
        if window.isVisible {                                          // Try again: the old error goes at once
            heading.stringValue = "Opening Mashup Studio"; step.stringValue = ""; detail.stringValue = ""
            bar.doubleValue = 0; spinner.startAnimation(nil)
        }
        let report: Report = { title, fraction, text in DispatchQueue.main.async { self.progress(title, fraction, text) } }
        let say = { (text: String) in DispatchQueue.main.async { self.step.stringValue = text } }
        DispatchQueue.global().async {
            let why = openStudio(self.setup, report: report, say: say, openPage: {
                // Only the Studio knows its page's key, so it opens its page (app.py, OPEN_PAGE). The launcher never
                // opens the address itself: whatever holds the port could be another user's server.
                env["MASHUP_STUDIO_NO_BROWSER"] != nil
                    || ask("POST", "/api/open-page", ["X-Open-Page": "1"])?.statusCode == 200
            })
            DispatchQueue.main.async { if let why { self.fail(why) } else { NSApp.terminate(nil) } }
        }
    }

    func progress(_ title: String, _ fraction: Double, _ text: String) {
        if !settingUp {                                                // once: a hidden window stays hidden
            settingUp = true; show(); heading.stringValue = "Setting up Mashup Studio"; spinner.startAnimation(nil)
        }
        step.stringValue = title; bar.doubleValue = fraction; detail.stringValue = text
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

    func fail(_ why: Stop) {
        show()
        spinner.stopAnimation(nil)
        heading.stringValue = why.heading
        step.stringValue = "Nothing is lost: try again once the problem is fixed."
        detail.stringValue = why.text
        again.isHidden = false
    }

    @objc func tryAgain() { open() }
    @objc func stop() { NSApp.terminate(nil) }
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }
    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool { show(); return true }
    func applicationWillTerminate(_ note: Notification) { setup.stop() }   // a setup in progress stops
}

let app = NSApplication.shared
let delegate = Window()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
