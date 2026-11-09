// s2a-devpush：经 usbmuxd（USB 或 Wi-Fi）用 house_arrest VendContainer + AFC 读写已装 App 的容器（开发签名的 App 才允许）。
//   s2a-devpush <udid> <包名> put <本地文件> <容器内路径>   逐级建父目录后整文件写入
//   s2a-devpush <udid> <包名> get <容器内路径> <本地文件>
//   s2a-devpush <udid> <包名> ls <容器内目录>
//   s2a-devpush <udid> <包名> put-tar <清单> <本地根> <容器内路径>   清单里的文件（每行一个相对路径，UTF-8）边读边打成 pax tar 直接写进手机，不落临时 tar
//   s2a-devpush - - tar-local <清单> <本地根> <本地输出>          同一字节流写本地文件（单测 / 对照用，不连手机）
//   s2a-devpush <udid> <包名> tar-ok <容器内路径>                 推送包写完整了吗（同 pack_apply.c 契约 ③：大小是 512 的整倍、≥ 1024、末尾 1024 字节全零）：打印 ok / bad
//   s2a-devpush - - tar-ok-local <本地文件>                       同上，本地文件
//   s2a-devpush <udid> - install <ipa>               装已签名的 ipa（AFC 传到 PublicStaging + installation_proxy；测试台经转发的设备服务装机用，玩家装机走 Impactor）
//   s2a-devpush <udid> <包名> uninstall -               卸 App（连容器）：免费账号一台手机最多 3 个 App，测试台腾位用
//   s2a-devpush <udid> <包名> launch [变量=值…]     起 App（先结束在跑的同包进程），带环境变量；打印 pid。Windows 测试台起局 / 只解包用（Mac 用 devicectl）：
//     经 lockdown 的 CoreDeviceProxy 建用户态隧道（不要管理员、不装驱动）→ RSD → CoreDevice app_service（同 Xcode / devicectl 的起 App 服务；iOS 17+，要开发者模式与已挂开发者镜像）
// 退出码：0 成功；1 连不上 / 读写失败（错误打到标准错误）
use idevice::{IdeviceService, afc::opcode::AfcFopenMode, core_device::CoreDeviceServiceClient, core_device_proxy::CoreDeviceProxy, house_arrest::HouseArrestClient, rsd::RsdHandshake, usbmuxd::{UsbmuxdAddr, UsbmuxdConnection}};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncSeek, AsyncSeekExt, AsyncWrite, AsyncWriteExt};

// pax tar（11-06 Windows 端到端线：push_win 原先每批先在出包机盘上打 1 GB 临时 tar 再整文件写进手机，和同机出包抢盘，旷野之息推包最慢 4.6 MB/s）：
//   手机端 runtime/src/pack_apply.c 只认普通文件 + pax 扩展头（path / size），结尾两块全零。每个文件都发一条 pax 头带完整路径（不受 ustar 100 字节名限制），
//   再发 ustar 头 + 内容 + 补齐到 512。手机端解完即删 .incoming 里的这个文件，所以中途断了留下的是截短包，push_win 下次会用空包盖掉（同 tools/push_pack.sh）
fn hdr(name: &[u8], size: u64, typ: u8) -> [u8; 512] {
    let mut h = [0u8; 512];
    let n = name.len().min(100); h[..n].copy_from_slice(&name[..n]);
    h[100..108].copy_from_slice(b"0000644\0"); h[108..116].copy_from_slice(b"0000000\0"); h[116..124].copy_from_slice(b"0000000\0");
    let sz = if size < 0o77777777777 { size } else { 0 };   // 超 8 GB 由 pax size 记
    h[124..136].copy_from_slice(format!("{:011o}\0", sz).as_bytes());
    h[136..148].copy_from_slice(b"00000000000\0");
    h[156] = typ; h[257..263].copy_from_slice(b"ustar\0"); h[263..265].copy_from_slice(b"00");
    for b in &mut h[148..156] { *b = b' '; }
    let sum: u32 = h.iter().map(|&b| b as u32).sum();
    h[148..156].copy_from_slice(format!("{:06o}\0 ", sum).as_bytes());
    h
}

fn pax_record(key: &str, val: &[u8]) -> Vec<u8> {   // 「长度 键=值\n」，长度含自身位数
    let body = 1 + key.len() + 1 + val.len() + 1;
    let mut n = body + 1; while (n.to_string().len() + body) != n { n = n.to_string().len() + body; }
    let mut r = format!("{n} {key}=").into_bytes(); r.extend_from_slice(val); r.push(b'\n'); r
}

async fn write_tar<W: AsyncWrite + Unpin>(w: &mut W, list: &[String], root: &str) -> Result<u64, String> {
    // 写缓冲 4 MB：AFC 每次 write 是一次往返，小文件每个要写头 / 内容 / 补齐三次（11-06 Windows 出包机原生 USB 推王国之泪：第 1 批 15.4 万个文件平均 6 KB，
    //   本地打包 16.6 秒、经 USB 约 1100 秒，全包 30.5 万个文件 19.2 GB 用 2696 秒、平均 7.1 MB/s）。合并成大块再发；大于缓冲的写直接透传
    let mut w = tokio::io::BufWriter::with_capacity(4 << 20, w);
    let mut total = 0u64; let mut buf = vec![0u8; 1 << 20];
    for rel in list {
        let path = std::path::Path::new(root).join(rel);
        let mut f = tokio::fs::File::open(&path).await.map_err(|e| format!("读不了 {}：{e}", path.display()))?;
        let size = f.metadata().await.map_err(|e| e.to_string())?.len();
        let mut pax = pax_record("path", rel.as_bytes());
        if size >= 0o77777777777 { pax.extend(pax_record("size", size.to_string().as_bytes())); }
        let mut out = Vec::with_capacity(2048);
        out.extend_from_slice(&hdr(b"PaxHeader", pax.len() as u64, b'x')); out.extend_from_slice(&pax); out.resize(out.len() + (512 - pax.len() % 512) % 512, 0);
        out.extend_from_slice(&hdr(rel.as_bytes(), size, b'0'));
        w.write_all(&out).await.map_err(|e| format!("写 tar 头失败（{rel}）：{e}"))?; total += out.len() as u64;
        let mut left = size;
        while left > 0 {
            let k = (left.min(buf.len() as u64)) as usize;
            f.read_exact(&mut buf[..k]).await.map_err(|e| format!("读 {rel} 失败：{e}"))?;
            w.write_all(&buf[..k]).await.map_err(|e| format!("写 {rel} 失败：{e}"))?;
            left -= k as u64; total += k as u64;
        }
        let pad = ((512 - size % 512) % 512) as usize;
        if pad > 0 { w.write_all(&vec![0u8; pad]).await.map_err(|e| e.to_string())?; total += pad as u64; }
    }
    w.write_all(&[0u8; 1024]).await.map_err(|e| format!("写归档尾失败：{e}"))?; total += 1024;
    w.flush().await.map_err(|e| e.to_string())?;
    Ok(total)
}

async fn tar_ok<R: AsyncRead + AsyncSeek + Unpin>(r: &mut R) -> Result<bool, String> {
    let size = r.seek(std::io::SeekFrom::End(0)).await.map_err(|e| e.to_string())?;
    if size < 1024 || size % 512 != 0 { return Ok(false); }
    r.seek(std::io::SeekFrom::Start(size - 1024)).await.map_err(|e| e.to_string())?;
    let mut tail = [0u8; 1024]; r.read_exact(&mut tail).await.map_err(|e| e.to_string())?;
    Ok(tail.iter().all(|&b| b == 0))
}

fn read_list(p: &str) -> Result<Vec<String>, String> {
    Ok(std::fs::read_to_string(p).map_err(|e| format!("读不了清单 {p}：{e}"))?.lines().filter(|l| !l.is_empty()).map(String::from).collect())
}

async fn run(a: &[String]) -> Result<(), String> {
    let (udid, bundle, op) = (&a[1], &a[2], a[3].as_str());
    if op == "tar-ok-local" {
        let mut f = tokio::fs::File::open(&a[4]).await.map_err(|e| format!("读不了 {}：{e}", a[4]))?;
        println!("{}", if tar_ok(&mut f).await? { "ok" } else { "bad" }); return Ok(());
    }
    if op == "tar-local" {
        let mut f = tokio::fs::File::create(&a[6]).await.map_err(|e| format!("建不了 {}：{e}", a[6]))?;
        let n = write_tar(&mut f, &read_list(&a[4])?, &a[5]).await?;
        println!("tar-local {n} 字节 → {}", a[6]); return Ok(());
    }
    // 找设备与连设备走同一个设备服务地址（USBMUXD_SOCKET_ADDRESS，缺省本机）：原先找设备固定连本机、连设备才看环境变量，
    //   设了地址时两边设备编号对不上（11-06 Windows 端到端线：手机插在别的机器上、经它的设备服务推包）
    let addr = UsbmuxdAddr::from_env_var().map_err(|e| e.to_string())?;
    let sock = addr.to_socket().await.map_err(|e| format!("连不上 Apple 设备服务：{e}"))?;
    let mut mux = UsbmuxdConnection::new(sock, 0);
    let dev = mux.get_device(udid).await.map_err(|e| format!("找不到设备 {udid}：{e}"))?;
    let provider = dev.to_provider(addr, "s2a-devpush");
    if op == "launch" {
        // CoreDevice app_service：idevice（钉的提交）自带的 launch_application 请求缺新系统要的字段（iOS 27 实测依次报缺 standardIOIdentifiers、
        //   platformSpecificOptions 不能是空数据），这里按 Xcode 同款请求自己拼
        let mut env = plist::Dictionary::new();
        for kv in &a[4.min(a.len())..] { if let Some((k, v)) = kv.split_once('=') { env.insert(k.to_string(), plist::Value::String(v.to_string())); } }
        let proxy = CoreDeviceProxy::connect(&provider).await.map_err(|e| format!("CoreDeviceProxy 连不上（要 iOS 17+、开发者模式）：{e}"))?;
        let rsd_port = proxy.tunnel_info().server_rsd_port;
        let mut adapter = proxy.create_software_tunnel().map_err(|e| format!("建不了用户态隧道：{e}"))?.to_async_handle();
        let stream = adapter.connect(rsd_port).await.map_err(|e| format!("连不上 RSD：{e}"))?;
        let hs = RsdHandshake::new(stream).await.map_err(|e| format!("RSD 握手失败：{e}"))?;
        let port = hs.services.get("com.apple.coredevice.appservice").ok_or("RSD 里没有 app_service（开发者镜像没挂？）")?.port;
        let st = adapter.connect(port).await.map_err(|e| format!("连不上 app_service：{e}"))?;
        let mut cd = CoreDeviceServiceClient::new(st).await.map_err(|e| format!("app_service 握手失败：{e}"))?;
        let mut spec = plist::Dictionary::new(); let mut bi = plist::Dictionary::new(); bi.insert("_0".into(), bundle.clone().into()); spec.insert("bundleIdentifier".into(), bi.into());
        let mut user = plist::Dictionary::new(); user.insert("active".into(), true.into());
        let mut opt = plist::Dictionary::new();
        opt.insert("arguments".into(), plist::Value::Array(vec![])); opt.insert("environmentVariables".into(), env.into());
        opt.insert("standardIOUsesPseudoterminals".into(), true.into()); opt.insert("startStopped".into(), false.into()); opt.insert("terminateExisting".into(), true.into());
        opt.insert("user".into(), user.into()); let mut pso = Vec::new(); plist::to_writer_xml(&mut pso, &plist::Dictionary::new()).map_err(|e| e.to_string())?; opt.insert("platformSpecificOptions".into(), plist::Value::Data(pso));
        let mut req = plist::Dictionary::new(); req.insert("applicationSpecifier".into(), spec.into()); req.insert("options".into(), opt.into()); req.insert("standardIOIdentifiers".into(), plist::Dictionary::new().into());
        let r = cd.invoke_with_plist("com.apple.coredevice.feature.launchapplication", req).await.map_err(|e| format!("起 {bundle} 失败：{e}"))?;
        let pid = r.as_dictionary().and_then(|d| d.get("processToken")).and_then(|t| t.as_dictionary()).and_then(|t| t.get("processIdentifier")).and_then(|p| p.as_signed_integer());
        println!("launch {bundle} pid {}", pid.map(|p| p.to_string()).unwrap_or_else(|| "?".into()));
        return Ok(());
    }
    if op == "uninstall" {   // 卸 App（连容器一起删）：免费开发者账号一台手机最多 3 个 App，测试台腾位用
        let mut ip = idevice::installation_proxy::InstallationProxyClient::connect(&provider).await.map_err(|e| format!("installation_proxy 连不上：{e}"))?;
        ip.uninstall(bundle.clone(), None).await.map_err(|e| format!("卸 {bundle} 失败：{e}"))?;
        println!("uninstall {bundle} 完成");
        return Ok(());
    }
    if op == "install" {
        idevice::utils::installation::install_package(&provider, &a[4], None).await.map_err(|e| format!("装 {} 失败：{e}", a[4]))?;
        println!("install {} 完成", a[4]);
        return Ok(());
    }
    let ha = HouseArrestClient::connect(&provider).await.map_err(|e| format!("house_arrest 连不上：{e}"))?;
    let mut afc = ha.vend_container(bundle.clone()).await.map_err(|e| format!("取不到 {bundle} 的容器（App 没装或不是开发签名）：{e}"))?;
    match op {
        "put" => {
            let (local, remote) = (&a[4], a[5].trim_start_matches('/').to_string());
            let mut parts: Vec<&str> = remote.split('/').collect(); parts.pop();
            let mut cur = String::new();
            for p in parts { if p.is_empty() { continue; } cur = if cur.is_empty() { p.to_string() } else { format!("{cur}/{p}") }; let _ = afc.mk_dir(cur.clone()).await; }
            let mut src = tokio::fs::File::open(local).await.map_err(|e| format!("读不了 {local}：{e}"))?;   // 流式：镜像可达十几 GB，不整份读进内存
            let f = afc.open(remote.clone(), AfcFopenMode::WrOnly).await.map_err(|e| format!("打不开 {remote}：{e}"))?;
            let mut f = Box::pin(f);
            let mut w = tokio::io::BufWriter::with_capacity(4 << 20, &mut f);
            let mut buf = vec![0u8; 1 << 20]; let mut total = 0u64;
            loop {
                let n = src.read(&mut buf).await.map_err(|e| format!("读 {local} 失败：{e}"))?;
                if n == 0 { break; }
                w.write_all(&buf[..n]).await.map_err(|e| format!("写 {remote} 失败：{e}"))?; total += n as u64;
            }
            w.flush().await.map_err(|e| format!("写 {remote} 失败：{e}"))?;
            println!("put {total} 字节 → {remote}");
        }
        "put-tar" => {
            let (list, root, remote) = (read_list(&a[4])?, &a[5], a[6].trim_start_matches('/').to_string());
            let mut parts: Vec<&str> = remote.split('/').collect(); parts.pop();
            let mut cur = String::new();
            for p in parts { if p.is_empty() { continue; } cur = if cur.is_empty() { p.to_string() } else { format!("{cur}/{p}") }; let _ = afc.mk_dir(cur.clone()).await; }
            let mut f = afc.open(remote.clone(), AfcFopenMode::WrOnly).await.map_err(|e| format!("打不开 {remote}：{e}"))?;
            let n = write_tar(&mut f, &list, root).await?;
            println!("put-tar {} 个文件 {n} 字节 → {remote}", list.len());
        }
        "tar-ok" => {
            let remote = a[4].trim_start_matches('/').to_string();
            let mut f = afc.open(remote.clone(), AfcFopenMode::RdOnly).await.map_err(|e| format!("打不开 {remote}：{e}"))?;
            println!("{}", if tar_ok(&mut f).await? { "ok" } else { "bad" });
        }
        "get" => {
            let (remote, local) = (a[4].trim_start_matches('/').to_string(), &a[5]);
            let mut f = afc.open(remote.clone(), AfcFopenMode::RdOnly).await.map_err(|e| format!("打不开 {remote}：{e}"))?;
            let data = f.read_entire().await.map_err(|e| format!("读 {remote} 失败：{e}"))?;
            tokio::fs::write(local, &data).await.map_err(|e| e.to_string())?;
            println!("get {} 字节 ← {remote}", data.len());
        }
        "get-tree" => {   // 整目录拉回本机（存档备份）：目录不在 = 0 个文件、退 0
            let (root, local) = (a[4].trim_start_matches('/').to_string(), a[5].clone());
            let (mut stack, mut n) = (vec![root.clone()], 0u64);
            while let Some(dir) = stack.pop() {
                let names = match afc.list_dir(dir.clone()).await {
                    Ok(v) => v,
                    Err(_) if dir == root && afc.get_file_info(root.clone()).await.is_err() => break,
                    Err(e) => return Err(format!("列 {dir} 失败：{e}")),
                };
                for name in names.into_iter().filter(|x| x != "." && x != "..") {
                    let path = format!("{dir}/{name}");
                    if afc.get_file_info(path.clone()).await.map_err(|e| format!("读 {path} 失败：{e}"))?.st_ifmt == "S_IFDIR" { stack.push(path); continue; }
                    let dest = std::path::Path::new(&local).join(&path[root.len() + 1..]);
                    tokio::fs::create_dir_all(dest.parent().unwrap()).await.map_err(|e| e.to_string())?;
                    let mut f = afc.open(path.clone(), AfcFopenMode::RdOnly).await.map_err(|e| format!("打不开 {path}：{e}"))?;
                    let data = f.read_entire().await.map_err(|e| format!("读 {path} 失败：{e}"))?;
                    drop(f);
                    tokio::fs::write(&dest, &data).await.map_err(|e| e.to_string())?;
                    n += 1;
                }
            }
            println!("get-tree {n} 个文件 ← {root}");
        }
        "rm" => {   // 删一个文件或整目录（多款容器删一款）：已不在算成功
            let remote = a[4].trim_start_matches('/').to_string();
            if afc.get_file_info(remote.clone()).await.is_ok() { afc.remove_all(remote.clone()).await.map_err(|e| format!("删 {remote} 失败：{e}"))?; }
            println!("rm {remote}");
        }
        "free" => {   // 手机可写字节（推包前核空间）；调用约定与其它操作同，第 5 个参数占位
            let i = afc.get_device_info().await.map_err(|e| e.to_string())?; println!("{}", i.free_bytes);
        }
        "ls" => {
            for n in afc.list_dir(a[4].trim_start_matches('/').to_string()).await.map_err(|e| e.to_string())? { println!("{n}"); }
        }
        _ => return Err(format!("不认识的操作 {op}")),
    }
    Ok(())
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]   // idevice AFC 内部用 block_in_place，单线程运行时会直接 panic
async fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.len() < 5 { eprintln!("用法：s2a-devpush <udid> <包名> put <本地> <远端> | get <远端> <本地> | ls <目录>"); std::process::exit(2); }
    if let Err(e) = run(&a).await { eprintln!("{e}"); std::process::exit(1); }
}
