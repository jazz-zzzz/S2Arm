import Foundation
import Observation
import S2ArmShellBridge
import ShellControllers
import ShellUI

/// 我方宿主层的最小实现：清单 → 库界面条目；手柄 / 键盘语义输入（上游 ShellControllers 原样）；Play → C 出口 s2a_shell_play。
/// 只借上游 ShellHostModel 的设计（@Observable 模型 + actions + startControllers），不带它的 EngineLaunchPlan / DM_* / framework 扫描。
/// 设置行：每款游戏的「画面」「虚拟手柄」两组（S2ArmGameSettings，11-07 维护者）；数据目录、云存档、删除等动作本刀不接（ShellActions 缺省空操作）。
@MainActor @Observable
public final class S2ArmShellHostModel {
    /// 清单里的全部款（容器里的）
    private var allGames: [GameDetail]
    /// 这台设备上资源没到位的款：不在库里显示
    private var notOnDevice: Set<String> = []
    /// 库里显示的款：只有这台设备上真能玩的（清单列着、资源已到位）
    public var games: [GameDetail] { allGames.filter { !notOnDevice.contains($0.id) } }
    public private(set) var controllers: [ControllerInfo] = []
    public private(set) var input = ShellInputLog()
    public private(set) var padSnapshot: PadSnapshot?
    @ObservationIgnored private let adapter = GamepadAdapter()
    @ObservationIgnored private let play: (String) -> Void
    /// 本款资源包在不在机上（宿主按 Documents/games/<游戏号> 判，见 s2a_shell.h）；清单写 playable 的款不在就改显示「需要游戏数据」、不给 Play
    @ObservationIgnored private let gameDataReady: (String) -> Bool
    @ObservationIgnored private let pickGameData: (String) -> Void
    /// 清单里写的状态（重判资源包时以它为底，不叠加）
    @ObservationIgnored private let listedStates: [String: GameState]
    @ObservationIgnored private let stores: S2ArmGameSettings.Stores
    @ObservationIgnored private let layoutState: (String) -> S2ArmGameSettings.LayoutState
    /// 正在调整虚拟手柄布局的那款（非空时宿主视图盖上编辑页）
    public var editingLayout: String?
    /// 正在改名字和封面的那款（非空时宿主视图盖上编辑页）
    public var editingIdentity: String?
    @ObservationIgnored private let identityStore: GameIdentityStore
    /// 清单里写的条目：恢复默认时回到它
    @ObservationIgnored private var listedTiles: [String: GameTile] = [:]
    @ObservationIgnored private var listedBanners: [String: URL] = [:]
    /// 编辑页底图：游戏号 → 封面文件
    public private(set) var bannerURLs: [String: URL]

    /// play 缺省走 C 出口；单测可换
    public init(catalogue: Catalogue, play: ((String) -> Void)? = nil, stores: S2ArmGameSettings.Stores = .live,
                layoutState: @escaping (String) -> S2ArmGameSettings.LayoutState = S2ArmGameSettings.layoutState,
                gameDataReady: ((String) -> Bool)? = nil, pickGameData: ((String) -> Void)? = nil, identityStore: GameIdentityStore = .live) {
        self.stores = stores; self.layoutState = layoutState; bannerURLs = catalogue.bannerURLs
        self.identityStore = identityStore
        listedTiles = Dictionary(catalogue.tiles.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        listedBanners = catalogue.bannerURLs
        allGames = catalogue.tiles.map { tile in
            var state = layoutState(tile.id); state.customIdentity = identityStore.load(tile.id) != nil
            return GameDetail(game: tile, sections: S2ArmGameSettings.sections(for: tile.id, stores: stores, state: state))
        }
        listedStates = Dictionary(catalogue.tiles.map { ($0.id, $0.state) }, uniquingKeysWith: { a, _ in a })
        self.play = play ?? { id in _ = id.withCString { s2a_shell_play($0) } }
        self.gameDataReady = gameDataReady ?? { id in id.withCString { s2a_shell_game_data_ready($0) } != 0 }
        self.pickGameData = pickGameData ?? { id in id.withCString { s2a_shell_pick_game_data($0) } }
        for tile in catalogue.tiles { applyIdentity(tile.id) }
        for p in catalogue.problems { NSLog("S2Arm 游戏库：%@", p) }
        refreshGameData()
        // 测试开关（真机自动化要 XCTest 触控 ID 授权、人不在时点不进去；同 diag.m S2A_OPEN_SETTINGS，环境变量只有 devicectl / Xcode 起 App 能给，玩家碰不到）：
        //   S2A_SHELL_OPEN_LAYOUT_EDITOR=<游戏号> 起来直接盖上这款的「调整位置」编辑页
        //   S2A_SHELL_PLAY=<游戏号> 起来 2 秒后替人点这款的 Play（走同一条 requestPlay → C 出口；截图局别带 S2A_AUTOPLAY=1：没有视觉驾驶员时自动玩家判卡住、优雅退出）
        let env = ProcessInfo.processInfo.environment
        if let id = env["S2A_SHELL_OPEN_LAYOUT_EDITOR"], allGames.contains(where: { $0.id == id }) {
            editingLayout = id; NSLog("S2Arm 游戏库：S2A_SHELL_OPEN_LAYOUT_EDITOR 直接开 %@ 的调整位置页", id)
        }
        if let id = env["S2A_SHELL_PLAY"], allGames.contains(where: { $0.id == id }) {
            NSLog("S2Arm 游戏库：S2A_SHELL_PLAY 2 秒后开始 %@", id)
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.requestPlay(id) }
        }
    }

    /// 重判各款资源包（建好、回前台、宿主说明里「重新检查」后）：清单 playable 且资源包不在 → needsGameData；放好了 → 回到清单状态
    public func refreshGameData() {
        var missing = Set<String>()
        defer { if missing != notOnDevice { notOnDevice = missing }; NSLog("S2Arm 游戏库：这台设备上 %d / %d 款可玩", allGames.count - missing.count, allGames.count) }
        for i in allGames.indices {
            let id = allGames[i].id, listed = listedStates[id] ?? allGames[i].game.state, ready = gameDataReady(id)
            if !ready { missing.insert(id) }
            let want: GameState = listed == .playable && !ready ? .needsGameData : listed
            if allGames[i].game.state != want {
                NSLog("S2Arm 游戏库：%@ 资源包%@", id, want == .needsGameData ? "不在机上，显示需要游戏数据" : "已就绪")
                allGames[i].game.state = want
            }
        }
    }

    public var actions: ShellActions {
        ShellActions(onPlay: { [weak self] id in self?.requestPlay(id) },
                     onChange: { [weak self] id, setting, value in self?.change(id, setting, value) },
                     onPickGameData: { [weak self] id in self?.requestGameData(id) },
                     onButtonTest: { [weak self] open in self?.buttonTest(open) })
    }

    /// 只放行可运行的那款（界面本身也只在 playable 时调 onPlay，这里再守一道）；放行前当场重判资源包（库开着时资源被删 / 换机拷走）
    public func requestPlay(_ id: String) {
        refreshGameData()
        if allGames.first(where: { $0.id == id })?.game.state == .needsGameData { requestGameData(id); return }
        guard let g = allGames.first(where: { $0.id == id }), g.game.state == .playable else {
            NSLog("S2Arm 游戏库：Play %@ 被拒（不在列表或不可运行）", id)
            return
        }
        play(id)
    }

    /// 「需要游戏数据」那款：交宿主给导入说明（宿主「重新检查」后经 s2a_shell_refresh 回到 refreshGameData）
    public func requestGameData(_ id: String) { pickGameData(id) }

    /// 设置行改了：存下、刷新这一款的设置行；选「调整位置…」打开编辑页
    public func change(_ id: String, _ setting: String, _ value: String) {
        if setting == S2ArmGameSettings.identity {
            if value == S2ArmGameSettings.editIdentity { editingIdentity = id }
            refresh(id)
            return
        }
        if S2ArmGameSettings.apply(id: id, setting: setting, value: value, stores: stores) { editingLayout = id }
        refresh(id)
    }
    /// 编辑页关了（存了 / 取消 / 恢复默认）：布局行跟着刷新
    public func closeLayoutEditor() { if let id = editingLayout { editingLayout = nil; refresh(id) } }
    func refresh(_ id: String) {
        guard let i = allGames.firstIndex(where: { $0.id == id }) else { return }
        var state = layoutState(id); state.customIdentity = identityStore.load(id) != nil
        allGames[i].sections = S2ArmGameSettings.sections(for: id, stores: stores, state: state)
    }

    /// 改名字和封面的编辑页要用：当前显示的名字，以及有没有自定义
    public func identityName(_ id: String) -> String { allGames.first(where: { $0.id == id })?.game.title ?? "" }
    public func hasCustomIdentity(_ id: String) -> Bool { identityStore.load(id) != nil }

    public func saveIdentity(_ id: String, name: String, cover: Data?) {
        // 名字没动时不存成自定义，否则电脑端以后改名就传不到这台手机
        let keep = name == listedTiles[id]?.title ? "" : name
        do { try identityStore.save(id, name: keep, cover: cover) }
        catch { NSLog("S2Arm 游戏库：保存 %@ 的名字和封面失败：%@", id, error.localizedDescription) }
        closeIdentityEditor(id)
    }
    public func resetIdentity(_ id: String) { identityStore.clear(id); closeIdentityEditor(id) }
    public func closeIdentityEditor(_ id: String) { editingIdentity = nil; applyIdentity(id); refresh(id) }

    /// 把这台手机上的名字和封面叠到清单条目上；没有就回到清单原样，状态保持不变。
    private func applyIdentity(_ id: String) {
        guard let i = allGames.firstIndex(where: { $0.id == id }), var tile = listedTiles[id] else { return }
        tile.state = allGames[i].game.state
        bannerURLs[id] = listedBanners[id]
        if let custom = identityStore.load(id) {
            if !custom.name.isEmpty { tile.title = custom.name }
            if let url = custom.cover, let data = try? Data(contentsOf: url), let art = Artwork(id: id + ".custom.\(data.hashValue)", data: data) {
                tile.banner = art; tile.icon = art; bannerURLs[id] = url
            }
        }
        allGames[i].game = tile
    }

    public func startControllers() {
        adapter.onInput = { [weak self] action, device in self?.input.append(action, device: device) }
        adapter.onRelease = { [weak self] action, device in self?.input.append(action, device: device, released: true) }
        adapter.onControllersChange = { [weak self] connected in
            self?.controllers = connected.map {
                ControllerInfo(id: $0.id, name: $0.name, player: "P\($0.player)", batteryPercent: $0.batteryPercent, charging: $0.charging)
            }
        }
        adapter.start()
    }

    public func stopControllers() { adapter.stop() }

    func buttonTest(_ open: Bool) {
        adapter.onSnapshot = open ? { [weak self] in self?.padSnapshot = $0 } : nil
        if !open { padSnapshot = nil }
    }
}
