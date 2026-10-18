import Foundation

enum BuildInfo {
    static let version = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "0.1.0-dev"
    /// v2（Worker 统一调度）：hello 增报 roles；job.start 带 class。
    /// Mac 转码器只做实时转码，roles 恒为 ["realtime"]。
    static let protocolVersion = 2

    /// 系统与架构，如「macOS 26.0 · arm64」。配对时上报（docs/design/login-devices.md §4），
    /// 网页的批准页与「设置 → 设备」据此写清是哪台机器。
    static let platform: String = {
        let os = ProcessInfo.processInfo.operatingSystemVersion
        var release = "\(os.majorVersion).\(os.minorVersion)"
        if os.patchVersion > 0 {
            release += ".\(os.patchVersion)"
        }
        #if arch(arm64)
        let arch = "arm64"
        #else
        let arch = "x86_64"
        #endif
        return "macOS \(release) · \(arch)"
    }()
}
