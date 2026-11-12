#!/usr/bin/env python3
# Windows replacement for dm/ios/tools/build_ipa.sh: the same unsigned all-in-one IPA (rewritten modules compiled into the executable, iOS shader libraries at DmIOS.app/msl,
# add-on content at DmIOS.app/aoc, the RomFS at DmIOS.app/romfs), built without Xcode:
#   xcodegen + xcodebuild   → LLVM clang (-target arm64-apple-ios26.0 -isysroot <iPhoneOS SDK>) per source file, linked by ld64.lld
#   Launcher.swift (SwiftUI) → win/ios/Launcher.m (UIKit, same dm_show_launcher contract), so no Swift toolchain is needed
#   actool (asset catalog)  → loose PNG icons named in CFBundleIcons (Pillow scales the game's icon.jpg)
#   sips / plutil           → Pillow / plistlib
#   make -n -B dmhost       → the source list of dm/host/Makefile, reproduced in host_sources() (checked against the Makefile text so an upstream change is noticed)
# Inputs: the same environment as build_ipa.sh (GAME, W, PATCHED, ROMFS, MSL_IOS, AOC, GFXAPI, DM_DISPLAY_NAME, DM_APP_VERSION, DM_BUNDLE_ID, DM_SHOW_FPS, DM_LANG, DM_ENV_FILE, DM_ICON, STD_FONT).
# Usage: win/build_app.py [output.ipa]   (--prepare-xcode is a Mac-only mode)
# It never signs: the sideloading tool (Sideloadly, AltStore, ...) signs the IPA, as with the upstream build.
import concurrent.futures as cf, hashlib, json, os, plistlib, re, shutil, subprocess, sys, time
from pathlib import Path
WIN = Path(__file__).resolve().parent
sys.path.insert(0, str(WIN))
import toolchain as tc

REPO = tc.REPO
DM = REPO / 'dm'
IOS = DM / 'ios'
HOST = DM / 'host'
sys.path.insert(0, str(DM / 'tools'))
import romfs as rfs   # the RomFS as a folder or as one DMROMFS1 image (nx_extract --romfs-image), read alike


def t(msg): print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)
def die(msg): t(f'FAIL: {msg}'); sys.exit(1)


def py(*args, env=None, capture=True):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=capture, text=True, env=env)
    if r.returncode: die(f'{Path(str(args[0])).name} {" ".join(map(str, args[1:]))}: {(r.stderr or r.stdout or "").strip()[-2000:]}')
    return (r.stdout or '').strip() if capture else ''


# ---- dm/host/Makefile, reproduced ----
def host_sources(game_h_dir):
    """(sources, include dirs) exactly as `make -n -B dmhost` lists them: SRC, GFX and the libvpx objects' sources; the include order of the host's link line, then libvpx's"""
    mk = (HOST / 'Makefile').read_text(encoding='utf-8')
    want_src = 'SRC=main.c loader.c hle.c crash.c probe.c thread.c memstat.m haptics_apple.m $(sort $(wildcard hle_*.c)) front_mac.m entry.S'
    want_gfx = 'GFX=$(sort $(wildcard ../gfx/*.c ../gfx/nvn/*.c ../gfx/nvn/*.m ../gfx/nvn/*.S ../gfx/tex/*.c ../gfx/tex/*.m ../gfx/vk/*.m ../gfx/gl/*.m))'
    for w in (want_src, want_gfx):
        if w not in mk: die(f'dm/host/Makefile changed ({w.split("=")[0]}= line): update host_sources() in win/build_app.py')
    src = [HOST / n for n in 'main.c loader.c hle.c crash.c probe.c thread.c memstat.m haptics_apple.m'.split()]
    src += sorted(HOST.glob('hle_*.c'), key=lambda p: p.name)
    src += [HOST / 'front_mac.m', HOST / 'entry.S']
    gfx = []
    for pat in ('../gfx/*.c', '../gfx/nvn/*.c', '../gfx/nvn/*.m', '../gfx/nvn/*.S', '../gfx/tex/*.c', '../gfx/tex/*.m', '../gfx/vk/*.m', '../gfx/gl/*.m'):
        d, g = pat.rsplit('/', 1)
        gfx += [(HOST / d / p.name) for p in (HOST / d).glob(g)]
    gfx = sorted(gfx, key=lambda p: os.path.relpath(p, HOST).replace('\\', '/'))   # make's $(sort) orders the words as text
    vpx = DM / 'third_party/libvpx'
    vpx_src = sorted(vpx.rglob('*.c'), key=lambda p: p.as_posix())
    incs = [DM / 'gfx/nvn/include', DM / 'third_party/vulkan-headers/include', game_h_dir, vpx, DM / 'third_party/stb_vorbis', vpx / 'config']
    files = []
    for s in src + gfx + vpx_src:
        s = s.resolve()
        if s.stem.endswith('_mac'): continue   # Mac-only sources (front_mac.m): front_ios.m implements front.h
        if s.parent == (IOS / 'Sources').resolve(): continue   # iOS sources the host borrows are compiled once, as iOS sources
        files.append(s)
    dirs = []
    for s in files:
        if s.parent not in dirs: dirs.append(s.parent)
    return files, [Path(i).resolve() for i in incs] + [d for d in dirs if d not in incs]


FRAMEWORKS_BASE = ['QuartzCore', 'Metal', 'MetalFX', 'GameController', 'CoreHaptics', 'AudioToolbox', 'VideoToolbox', 'CoreMedia', 'CoreVideo', 'ImageIO', 'UniformTypeIdentifiers',   # dm/host/Makefile minus Cocoa
                   'UIKit', 'Foundation', 'CoreGraphics', 'CoreFoundation']   # what the UIKit side and the launcher use (Xcode would add UIKit through Swift / module autolinking)
IMPORT = re.compile(r'^\s*#\s*(?:import|include)\s*<(\w+)/', re.M)


def unpack_romfs_image(img, dst):
    """A DMROMFS1 image as a RomFS folder, for the steps that need one (mods' overlay, split data); reused while the image is unchanged"""
    img = os.fspath(img); st = os.stat(img); stamp = Path(dst).with_name(Path(dst).name + '.stamp'); want = f'{st.st_size} {st.st_mtime_ns}'
    if Path(dst).is_dir() and stamp.is_file() and stamp.read_text() == want: return Path(dst)
    t(f'unpacking {img} into a folder ({st.st_size / 2**30:.1f} GB) for the mods / split data'); shutil.rmtree(dst, ignore_errors=True); stamp.unlink(missing_ok=True)
    m = rfs.Image(img); n = 0
    with open(img, 'rb') as f:
        todo = [(0, Path(dst))]
        while todo:
            k, d = todo.pop(); d.mkdir(parents=True, exist_ok=True)
            for name, j in m.children(k):
                off, size, _, _, isfile, _ = m.entry(j)
                if not isfile: todo.append((j, d / name)); continue
                f.seek(off); left = size
                with open(d / name, 'wb') as o:
                    while left:
                        b = f.read(min(left, 8 << 20))
                        if not b: die(f'{img}: ends inside {name}')
                        o.write(b); left -= len(b)
                n += 1
    stamp.write_text(want); t(f'unpacked {n:,} files')
    return Path(dst)


def frameworks(sdk, files):
    fw_dir = sdk / 'System/Library/Frameworks'
    out = list(FRAMEWORKS_BASE)
    for f in files:
        if f.suffix not in ('.m', '.c', '.h'): continue
        for name in IMPORT.findall(f.read_text(encoding='utf-8', errors='replace')):
            if name not in out and (fw_dir / f'{name}.framework').is_dir(): out.append(name)
    return [n for n in out if (fw_dir / f'{n}.framework').is_dir()]


# ---- compile + link ----
def compile_all(clang, sdk, files, incs, obj_dir, extra_defs):
    target = f'arm64-apple-ios{tc.setting("ios_deployment", tc.IOS_DEPLOYMENT)}'
    common = ['-target', target, '-isysroot', str(sdk), '-Os', '-mcpu=apple-a18', '-w', '-fno-common', '-DNS_BLOCK_ASSERTIONS=1', *extra_defs] + [f'-I{i}' for i in incs]
    obj_dir.mkdir(parents=True, exist_ok=True)
    jobs, objs = [], []
    for f in files:
        name = f.name
        if f == (HOST / 'main.c').resolve(): flags = common + ['-std=gnu11', '-Dmain=dm_main']   # the host's main, started by main.m (project.yml compilerFlags)
        elif f.suffix == '.c': flags = common + ['-std=gnu11']
        elif f.suffix == '.m': flags = common + ['-std=gnu11', '-fobjc-arc']
        elif f.suffix == '.S': flags = common
        else: continue
        key = hashlib.sha256((str(f) + '\0' + '\0'.join(flags)).encode()).hexdigest()[:12]
        o = obj_dir / f'{f.stem}-{key}.o'
        objs.append(o)
        if o.exists() and o.stat().st_mtime >= f.stat().st_mtime and not dep_newer(o): continue
        jobs.append((f, o, flags))
    t(f'compile: {len(jobs)} of {len(objs)} files (clang {target})')
    def one(j):
        f, o, flags = j
        d = o.with_suffix('.d')
        r = subprocess.run([str(clang), *flags, '-MMD', '-MF', str(d), '-c', str(f), '-o', str(o) + '.tmp'], capture_output=True, text=True)
        if r.returncode == 0: os.replace(str(o) + '.tmp', o)
        return f, r
    bad = []
    with cf.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        for i, (f, r) in enumerate(ex.map(one, jobs), 1):
            if r.returncode: bad.append((f, r.stderr))
            if i % 50 == 0 or i == len(jobs): t(f'compile: {i}/{len(jobs)}')
    if bad:
        for f, err in bad[:5]: print(f'---- {f}\n' + '\n'.join(l for l in err.splitlines() if 'error' in l)[:3000], file=sys.stderr)
        die(f'{len(bad)} file(s) did not compile')
    return objs


def dep_newer(o):
    """True when a header the object was built from (its .d file) is newer than the object"""
    d = o.with_suffix('.d')
    if not d.exists(): return True
    mt = o.stat().st_mtime
    text = d.read_text(errors='replace').replace('\\\n', ' ')
    for dep in re.split(r'(?<!\\)\s+', text.split(':', 1)[-1]):
        dep = dep.replace('\\ ', ' ').strip()
        if dep and os.path.exists(dep) and os.path.getmtime(dep) > mt: return True
    return False


def link(clang, sdk, objs, fws, embed_flags, out):
    target = f'arm64-apple-ios{tc.setting("ios_deployment", tc.IOS_DEPLOYMENT)}'
    args = [str(clang), '-target', target, '-isysroot', str(sdk), '-fuse-ld=lld', '-fobjc-arc', *(['-Wl,-adhoc_codesign'] if os.environ.get('NXW_ENTITLEMENTS', '1') != '0' else []), '-o', str(out), *map(str, objs),
            '-Wl,-rpath,@executable_path/Frameworks', '-lobjc', '-lz', *[x for f in fws for x in ('-framework', f)], *embed_flags]
    rt = compat_rt_needed(clang)
    if rt: args.append(str(rt))
    t(f'link: {len(objs)} objects, {len(fws)} frameworks (ld64.lld)')
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode: print(r.stderr[-6000:], file=sys.stderr); die('link failed')


def compat_rt_needed(clang):
    """LLVM for Windows ships no Darwin compiler-rt (libclang_rt.ios.a), which provides __isPlatformVersionAtLeast for @available; win/ios/compat_rt.c stands in for it"""
    r = subprocess.run([str(clang), '-print-resource-dir'], capture_output=True, text=True)
    res = Path(r.stdout.strip()) if r.returncode == 0 else None
    if res and list(res.glob('lib/darwin/libclang_rt.ios.a')): return None
    return WIN / 'build' / 'compat_rt.o'


# ---- bundle ----
def info_plist(bundle_id, name, version, category, show_fps, lang, sdk_ver, sdk_build, icons):
    p = {
        'CFBundleDevelopmentRegion': 'en', 'CFBundleExecutable': 'DmIOS', 'CFBundleIdentifier': bundle_id, 'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleName': 'DmIOS', 'CFBundleDisplayName': name, 'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': version, 'CFBundleVersion': version,
        'CFBundleSupportedPlatforms': ['iPhoneOS'], 'DTPlatformName': 'iphoneos', 'DTPlatformVersion': sdk_ver, 'DTSDKName': f'iphoneos{sdk_ver}', 'DTSDKBuild': sdk_build,
        'MinimumOSVersion': tc.setting('ios_deployment', tc.IOS_DEPLOYMENT), 'LSRequiresIPhoneOS': True, 'UIDeviceFamily': [1, 2], 'UIRequiredDeviceCapabilities': ['arm64'],
        # dm/ios/project.yml info.properties
        'UILaunchStoryboardName': '', 'UIFileSharingEnabled': True, 'LSSupportsOpeningDocumentsInPlace': True,
        'UISupportedInterfaceOrientations': ['UIInterfaceOrientationLandscapeRight', 'UIInterfaceOrientationLandscapeLeft'],
        'UISupportedInterfaceOrientations~ipad': ['UIInterfaceOrientationLandscapeRight', 'UIInterfaceOrientationLandscapeLeft'],
        'UIRequiresFullScreen': True, 'UIStatusBarHidden': True, 'UIViewControllerBasedStatusBarAppearance': False, 'LSApplicationCategoryType': category,
        'DMShowFPS': show_fps, 'DMLanguage': lang, 'LSSupportsGameMode': True, 'GCSupportsControllerUserInteraction': True,
        'GCSupportedGameControllers': [{'ProfileName': 'ExtendedGamepad'}], 'CADisableMinimumFrameDurationOnPhone': True,
        'UIApplicationSceneManifest': {'UIApplicationSupportsMultipleScenes': False, 'UISceneConfigurations': {'UIWindowSceneSessionRoleApplication': [
            {'UISceneConfigurationName': 'Default', 'UISceneDelegateClassName': 'SceneDelegate'}]}},
    }
    # The data ZIP import (win/ios/DataHelp.m) runs as a continued-processing task on iOS 26 and later: it goes on in the background with a Live Activity. Its identifiers are
    # <bundle ID>.import.<n> (a signer that changes the bundle ID makes the request fail, and the import runs in the foreground instead)
    p['BGTaskSchedulerPermittedIdentifiers'] = [f'{bundle_id}.import.*']
    p['UIBackgroundModes'] = ['processing']
    if icons:
        p['CFBundleIcons'] = {'CFBundlePrimaryIcon': {'CFBundleIconFiles': ['AppIcon60x60'], 'CFBundleIconName': 'AppIcon'}}
        p['CFBundleIcons~ipad'] = {'CFBundlePrimaryIcon': {'CFBundleIconFiles': ['AppIcon60x60', 'AppIcon76x76', 'AppIcon83.5x83.5'], 'CFBundleIconName': 'AppIcon'}}
    return p


ICONS = [('AppIcon60x60@2x.png', 120), ('AppIcon60x60@3x.png', 180), ('AppIcon76x76@2x~ipad.png', 152), ('AppIcon83.5x83.5@2x~ipad.png', 167), ('AppIcon.png', 1024)]


def make_icons(src, app):
    try: from PIL import Image
    except ImportError: t('no app icon: Pillow is not installed (pip install pillow); the home-screen icon stays blank'); return False
    im = Image.open(src).convert('RGB')
    for name, px in ICONS: im.resize((px, px), Image.LANCZOS).save(app / name, 'PNG')
    t(f'app icon: {src}')
    return True


_LISTED = {}
def list_files(src):
    """[(path, path relative to src with '/', size)] of a folder, by os.scandir (on Windows the sizes come with the listing, no stat per file: TotK's RomFS is 300,000 files).
    Listed once per build"""
    src = Path(src)
    if src in _LISTED: return _LISTED[src]
    out, stack = [], [(str(src), '')]
    while stack:
        d, rel = stack.pop()
        with os.scandir(d) as it:
            for e in it:
                r = rel + e.name
                if e.is_dir(follow_symlinks=False): stack.append((e.path, r + '/'))
                else: out.append((e.path, r, e.stat(follow_symlinks=False).st_size))
    out.sort(key=lambda x: x[1]); _LISTED[src] = out
    return out


def split_data(data, parts, stamp, name, skip_suffix=()):
    """Fill the data folder of a split build: {path inside it: source folder}. Files are hard-linked (no disk space) on several threads, and copied where links fail
    (another drive). A folder that a build with the same stamp and the same files filled is kept as it is; otherwise it is made again in <folder>.partial and swapped in,
    so it never mixes files from two builds. Returns (files, bytes)"""
    import concurrent.futures as cf
    todo = [(f, f'{rel}/{r}', n) for rel, src in parts.items() for f, r, n in list_files(src) if not r.endswith(tuple(skip_suffix))]
    count, size = len(todo), sum(n for _, _, n in todo)
    sig = stamp + f'files\t{count}\nbytes\t{size}\n'
    if (data / 'nxw_data.tsv').is_file() and (data / 'nxw_data.tsv').read_text(encoding='utf-8') == sig:
        t(f'split build: reusing the data folder {data} ({count:,} files, {size / 2**30:.2f} GB, the same build)')
        return count, size
    t(f'split build: game files and shader libraries go to {data} ({count:,} files, hard links on {min(32, (os.cpu_count() or 4) * 2)} threads)')
    tmp = data.with_name(data.name + '.partial')
    if tmp.exists(): shutil.rmtree(tmp)
    for d in sorted({str(Path(r).parent) for _, r, _ in todo}): (tmp / d).mkdir(parents=True, exist_ok=True)
    def one(item):
        f, r, _ = item; dst = tmp / r
        try: os.link(f, dst)
        except OSError: shutil.copy2(f, dst)
    with cf.ThreadPoolExecutor(min(32, (os.cpu_count() or 4) * 2)) as ex: list(ex.map(one, todo, chunksize=256))
    (tmp / 'nxw_data.tsv').write_text(sig, encoding='utf-8')
    (tmp / 'README.txt').write_text(
        f'{name}: game data for the split IPA\n\n'
        'Install the IPA, open it once, then copy this whole folder (or its contents) into the app\'s Documents:\n'
        '  - Windows: Apple Devices app (or iTunes) > your iPhone/iPad > Files > the app > drag this folder in\n'
        '  - iPhone/iPad: Files app > On My iPhone/iPad > the app (from a USB drive, a network share or iCloud Drive)\n'
        'Then fully close and reopen the app. Re-signing the IPA later does not touch these files.\n'
        'Copy the data folder again only after a new build of the same game (nxw_data.tsv names the build).\n', encoding='utf-8')
    if data.exists(): shutil.rmtree(data)
    os.replace(tmp, data)
    t(f'data folder: {count:,} files, {size / 2**30:.2f} GB')
    return count, size


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--prepare-xcode': die('--prepare-xcode makes an Xcode project: it needs a Mac (this Windows build packs the IPA directly)')
    env = os.environ
    game = env.get('GAME') or die('GAME is not set')
    w = Path(env.get('W') or die('W is not set: W=<NSP folder>/<title ID>-work'))
    sys.path.insert(0, str(DM / 'games')); import game as gm
    prof = gm.load(game)
    gd = prof.dir
    gp = lambda k: prof.get(k)
    if not (gd / 'game.tsv').exists(): die(f'no game profile {gd}/game.tsv')
    pkg = gp('package')
    patched = Path(env.get('PATCHED') or w / f'build/patched_{pkg}')
    romfs = rfs.Path(env.get('ROMFS') or rfs.source(w / f'data/{pkg}'))   # <extraction>/RomFS.img, else the RomFS folder (build_ipa.sh)
    image = rfs.is_image(romfs)
    msl_ios = Path(env.get('MSL_IOS') or w / 'build/ios/dmsh_b')
    aoc = Path(env['AOC']) if env.get('AOC') else (None if 'AOC' in env else w / 'data/aoc')
    out_ipa = Path(sys.argv[1]) if len(sys.argv) > 1 else w.parent / gp('ipa')
    mods, marker = gp('modules').split(), gp('romfs_marker')
    nacp = w / 'data/v0/control.nacp'
    if not nacp.exists(): nacp = romfs.parent / 'control.nacp'
    vnacp = romfs.parent / 'control.nacp'
    if not vnacp.exists(): vnacp = w / 'data/v0/control.nacp'
    name = env.get('DM_DISPLAY_NAME') or py(DM / 'tools/nacp.py', nacp)
    r = subprocess.run([sys.executable, str(DM / 'tools/nacp.py'), '--version', str(vnacp)], capture_output=True, text=True)
    version = env.get('DM_APP_VERSION') or (r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else '1.0')
    bundle_id = env.get('DM_BUNDLE_ID') or f'org.nxnative.{game.lower()}'
    prefix = env.get('NXW_BUNDLE_PREFIX')   # the Windows driver's default prefix (org.nxnativewindows), unless --bundle-id named an ID
    if prefix and bundle_id.startswith('org.nxnative.'): bundle_id = prefix.rstrip('.') + bundle_id[len('org.nxnative'):]
    show_fps = env.get('DM_SHOW_FPS', '0') in ('1', 'YES')
    lang = env.get('DM_LANG') or 'en-US'

    for m in mods:
        if not ((patched / f'{m}.patched.bin').exists() and (patched / f'{m}.fixups.tsv').exists()): die(f'no rewritten module {patched}/{m} (run dm/tools/patch_nso.py first)')
    if not (romfs / marker).exists(): die(f'no RomFS at {romfs} (missing {marker})')
    if image and (env.get('NXW_MODS_ROMFS') or env.get('NXW_SPLIT_DATA', '') not in ('', '0')):   # both lay out the RomFS as a folder, and the NSP script extracts one image (nx_extract --romfs-image)
        romfs, image = rfs.Path(unpack_romfs_image(romfs, w / 'build/romfs_unpacked')), False
    if env.get('NXW_MODS_ROMFS'):   # mods' RomFS folders (win/mods.py): hard links of the game's files with the mods' laid over them
        import mods as mod_overlay   # not "mods": that name is the module list gen_embed.py takes below
        romfs = mod_overlay.overlay_romfs(romfs, env['NXW_MODS_ROMFS'].split(os.pathsep), w / 'build/romfs_mods', log=t)
    hdr = (DM / 'gfx/nvn/dmsh_chain.h').read_text(encoding='utf-8')
    v = (re.search(r'^#define DMSH_CHAIN "(.*)"', hdr, re.M) or die('no DMSH_CHAIN in dmsh_chain.h')).group(1)
    gfx = env.get('GFXAPI') or ('nvn' if list((msl_ios / f'b-{v}').glob('*_b.metallib')) else 'vulkan' if (msl_ios / 'vk/vk_shaders.metallib').exists()
                                else 'gl' if (msl_ios / 'gl/gl_shaders.metallib').exists() else 'nvn')
    capture = env.get('NXW_CAPTURE_IPA') == '1'   # a capture IPA: no shader libraries (the game runs only to compile its run-time shaders, which its env.txt has the host save)
    if capture:
        msldir = {'vulkan': 'vk', 'gl': 'gl'}.get(gfx, f'b-{v}'); msl_ios = w / 'build/ios/empty_msl'; (msl_ios / msldir).mkdir(parents=True, exist_ok=True)
        t('capture IPA: no shader libraries; the game saves the shaders it compiles at run time into Documents')
    elif gfx == 'vulkan':
        if not (msl_ios / 'vk/vk_shaders.metallib').exists(): die(f'no iOS Vulkan shader library {msl_ios}/vk')
        msldir = 'vk'
    elif gfx == 'gl':
        if not (msl_ios / 'gl/gl_shaders.metallib').exists(): die(f'no iOS GL shader library {msl_ios}/gl')
        msldir = 'gl'
    else:
        msldir = f'b-{v}'
        if not list((msl_ios / msldir).glob('*_b.metallib')): die(f'no iOS shader libraries {msl_ios}/{msldir}')
        inputs = py(DM / 'gfx/tools/dmsh_chain.py', '--inputs', env=dict(env, DM_GAME=game))
        stamp = (msl_ios / msldir / 'inputs.tsv')
        lines = stamp.read_text(encoding='utf-8').splitlines() if stamp.exists() else []
        if f'game\t{game}' not in lines or f'inputs\t{inputs}' not in lines: die(f'the iOS shader libraries {msl_ios}/{msldir} are not a full build from this profile\'s current tables (inputs {inputs})')
        c1 = gd / 'c1_full.tsv'
        if not (c1.exists() and c1.read_text(encoding='utf-8').splitlines()[:1] and c1.read_text(encoding='utf-8').splitlines()[0].startswith(f'# chain b-{v} inputs {inputs} ')):
            die(f'{c1} is missing or not made from these libraries (chain {v}, inputs {inputs})')

    sdk = tc.ios_sdk() or die('no iPhoneOS SDK: set NXW_IOS_SDK (win/sdk/extract_xip.py extracts it from Xcode.xip)')
    clang = tc.llvm_tool('clang') or die('no clang: install LLVM for Windows (https://github.com/llvm/llvm-project/releases)')
    if not tc.llvm_tool('ld64.lld'): die('no ld64.lld in the LLVM folder')
    sdk_ver, sdk_build = tc.sdk_settings(sdk)
    t(f'app: {name} {version} ({bundle_id}), language {lang}{", frame-rate counter on" if show_fps else ""}; SDK {sdk.name} {sdk_ver} {sdk_build}')

    base = w / 'build/ios/win'
    gen, app = base / 'Gen', base / 'Payload/DmIOS.app'
    if gen.exists(): shutil.rmtree(gen)
    if app.exists(): shutil.rmtree(app)
    (gen / 'profile').mkdir(parents=True); app.mkdir(parents=True)
    t('generate Gen/ (profile header, embedded modules, tables)')
    py(DM / 'games/game.py', 'header', game, gen / 'profile/game_profile.h')
    flags = py(IOS / 'tools/gen_embed.py', patched, gen / 'embed.S', *mods)
    es = gen / 'embed.S'
    es.write_text(re.sub(r'\.incbin "([^"]*)"', lambda m: '.incbin "' + m.group(1).replace('\\', '/') + '"', es.read_text(encoding='utf-8')), encoding='utf-8')   # assembler strings take \ as an escape

    dmres = app / 'dm'; dmres.mkdir()
    for pat in ('*.fixups.tsv', '*.gates.tsv', '*.cuts.tsv', '*.obs.tsv'):
        for f in patched.glob(pat): shutil.copy2(f, dmres)
    if (patched / 'nro.tsv').exists(): shutil.copy2(patched / 'nro.tsv', dmres)
    for f in ('shader_map.tsv', 'pso_manifest.tsv') + (() if gfx in ('vulkan', 'gl') else ('c1_full.tsv',)):
        if capture and not (gd / f).exists(): continue   # a capture IPA has no shader build: the tables it would stamp may not exist yet
        shutil.copy2(gd / f, dmres)
    if gfx == 'nvn':
        if (gd / 'cb_extent.tsv').exists(): shutil.copy2(gd / 'cb_extent.tsv', dmres)
        else: t(f'no {gd}/cb_extent.tsv: the host copies whole uniform bindings')
        # the shader pairs, beside the shader map where the pipeline prebuild reads them (nvn_mtl.m pso_prebuild): without them the app makes no pulling bases (build_ipa.sh)
        for f in ('shader_pairs.tsv', 'pso_pairs.tsv'):
            if (gd / f).exists(): shutil.copy2(gd / f, dmres)
    # The pipeline archive made offline (dm/gfx/tools/pso_offline.py, as build_ipa.sh): the base of every shader pair compiled by metal-tt (Metal Developer Tools for Windows has it)
    # for each GPU architecture in DM_PSO_GPUS (comma-separated; none = no archive). Default: g18p for a game whose profile sets pso_mtl4 on (only the Metal 4 path reads it), else none
    pso = None
    gpus = env['DM_PSO_GPUS'] if 'DM_PSO_GPUS' in env else ('g18p' if prof.get('pso_mtl4', 'off') == 'on' else 'none')
    if gfx == 'nvn' and not capture and gpus and gpus != 'none' and (gd / 'shader_pairs.tsv').exists():
        pso = w / 'build/ios/pso/bundle'
        t(f'pipeline archive for {gpus} (metal-tt; minutes when its inputs changed)')
        r = subprocess.run([sys.executable, str(DM / 'gfx/tools/pso_offline.py'), '--game', game, '--msl', str(msl_ios / msldir), '--out', str(pso), '--work', str(w / 'build/ios/pso/work'),
                            '--gpu', gpus] + (['--os-min', env['DM_PSO_IOS_MIN']] if env.get('DM_PSO_IOS_MIN') else []))
        if r.returncode: die('pipeline archive (dm/gfx/tools/pso_offline.py; DM_PSO_GPUS=none builds the app without it)')
    elif gfx == 'nvn': t('no pipeline archive (' + (f'DM_PSO_GPUS={gpus}' if 'DM_PSO_GPUS' in env else 'the profile does not set pso_mtl4 on; DM_PSO_GPUS=g18p builds one')
                         + ('' if (gd / 'shader_pairs.tsv').exists() else ', no shader_pairs.tsv') + ')')
    t('launcher: ' + py(DM / 'games/settings.py', game, patched, vnacp, dmres / 'launcher.json', env=dict(env, GFXAPI=gfx)))
    if env.get('DM_ENV_FILE'): shutil.copy2(env['DM_ENV_FILE'], dmres / 'env.txt')
    (dmres / 'font').mkdir()
    font = tc.std_font() or die('no standard font: set STD_FONT to a TTF with broad Unicode coverage')
    shutil.copy2(font, dmres / 'font/standard.ttf'); t(f'standard font: {font}')
    py(DM / 'tools/make_ext_font.py', dmres / 'font/nintendo_ext.ttf', tc.letter_font() or die('no bold TTF for the button icons: set LETTER_FONT'))

    files, incs = host_sources(gen / 'profile')
    files += [p.resolve() for p in sorted((IOS / 'Sources').glob('*.m'))] + [(WIN / 'ios/Launcher.m').resolve(), (WIN / 'ios/DataHelp.m').resolve(), (WIN / 'ios/nxw_unzip.c').resolve(), es.resolve()]
    fws = frameworks(sdk, files)
    rt = compat_rt_needed(clang)
    if rt:
        rt.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([str(clang), '-target', f'arm64-apple-ios{tc.IOS_DEPLOYMENT}', '-isysroot', str(sdk), '-Os', '-c', str(WIN / 'ios/compat_rt.c'), '-o', str(rt)], capture_output=True, text=True)
        if r.returncode: print(r.stderr, file=sys.stderr); die('compat_rt.c')
    objs = compile_all(clang, sdk, files, incs, base / 'obj', [])
    link(clang, sdk, objs, fws, flags.split(), app / 'DmIOS')
    # The entitlements the app asks for, in an ad-hoc signature of its own (win/macho_sign.py): the player's signing tool keeps those its Apple ID allows. Increased Memory Limit
    # is free to enable on an App ID (SideInstaller does it), but SideStore installs only what the executable asks for: unsigned, it asked for nothing
    # On by default (NXW_ENTITLEMENTS=0 / --no-entitlements leaves the executable unsigned). Checked against rcodesign's own ad-hoc signature; SideStore with Increased Memory Limit
    # enabled for the App ID (SideInstaller) installs it On, and a signing tool that may not grant it drops it: the app opens either way
    if env.get('NXW_ENTITLEMENTS', '1') != '0':
      import macho_sign
      ents = {'com.apple.developer.kernel.increased-memory-limit': True}
      t(f'entitlements asked for: {", ".join(ents)} (' + str(macho_sign.sign(str(app / 'DmIOS'), bundle_id, ents)) + '-byte ad-hoc signature)')

    icon = Path(env.get('DM_ICON') or romfs.parent / 'icon.jpg')
    icons = icon.exists() and make_icons(icon, app)
    if not icon.exists(): t(f'no app icon ({icon} not found): the home-screen icon stays blank')
    with open(app / 'Info.plist', 'wb') as f:
        pl = info_plist(bundle_id, name, version, gp('app_category'), show_fps, lang, sdk_ver, sdk_build, icons)
        if env.get('DM_METAL_CAPTURE') == '1': pl['MetalCaptureEnabled'] = True; t('Metal frame capture: enabled (DM_METAL_CAPTURE=1)')   # the host's DM_GPU_CAPTURE switch
        sp = env.get('NXW_SPLIT_DATA', '')
        if sp and sp != '0':   # a split build: the app's "game data needed" screen (win/ios/DataHelp.m) names the data folder and its size
            dname = Path(sp).name if sp not in ('1', 'YES') else out_ipa.stem + '-data'
            dsize = sum(n for d in (romfs, msl_ios / msldir, aoc if aoc and aoc.is_dir() else None) if d for _, _, n in list_files(d))   # listed once, reused by split_data
            pl['NXWDataFolder'] = dname; pl['NXWDataSize'] = f'{dsize / 2**30:.1f} GB' if dsize >= 2**30 else f'{dsize / 2**20:.0f} MB'
        plistlib.dump(pl, f, fmt=plistlib.FMT_BINARY)
    (app / 'PkgInfo').write_bytes(b'APPL????')

    has_aoc = bool(aoc and aoc.is_dir() and any(aoc.iterdir()))
    if has_aoc: t('add-on content: ' + ' '.join(sorted(p.name for p in aoc.iterdir())))
    split = env.get('NXW_SPLIT_DATA', '')
    maps = [f'Payload/DmIOS.app={app}']
    if pso: maps.append(f'Payload/DmIOS.app/pso={pso}')
    # The pulling vertex libraries (<key>_bp) serve the hybrid pipelines: packed when the profile compiles pso_mtl4 in (DM_GAME_PSO_MTL4), or IOS_PULL=1, and always with an archive (build_ipa.sh)
    m = re.search(r'^#define DM_GAME_PSO_MTL4 (\d)', (gen / 'profile/game_profile.h').read_text(encoding='utf-8'), re.M)
    pull = env.get('IOS_PULL') or (m.group(1) if m else '0')
    skip_bp = not pso and pull != '1'
    if split and split != '0':
        # Split build: the IPA holds only the app (small, quick to sign and install); the game files and shader libraries go to a data folder the player copies
        # into the app's Documents (dm/ios/Sources/main.m looks for <Documents>/romfs and <Documents>/<folder>/romfs, and takes msl/ and aoc/ beside it)
        data = Path(split) if split not in ('1', 'YES') else out_ipa.with_name(out_ipa.stem + '-data')
        split_data(data, {'romfs': romfs, f'msl/{msldir}': msl_ios / msldir, **({'aoc': aoc} if has_aoc else {})},
                   f'game\t{game}\nbundle_id\t{bundle_id}\nversion\t{version}\nchain\t{v}\ngfx\t{gfx}\n', name, skip_suffix=('_bp.metallib',) if skip_bp else ())
    else:
        maps.append(f'Payload/DmIOS.app/msl/{msldir}={msl_ios / msldir}')
        if has_aoc: maps.append(f'Payload/DmIOS.app/aoc={aoc}')
        maps.append(f'Payload/DmIOS.app/romfs.img={romfs}' if image else f'Payload/DmIOS.app/romfs={romfs}')   # main.m romfs_locate opens <App>/romfs.img (build_ipa.sh)
    t('pack IPA (already-compressed game files stored, everything else deflated)')
    r = subprocess.run([sys.executable, str(IOS / 'tools/pack_ipa.py'), str(out_ipa), '--store', ':'.join(gp('zip_store').split(':')),
                        '--exec', 'Payload/DmIOS.app/DmIOS', '--skip', 'Payload/DmIOS.app/msl', '--skip', 'Payload/DmIOS.app/aoc', '--skip', 'Payload/DmIOS.app/romfs', '--skip', 'Payload/DmIOS.app/romfs.img',
                        '--skip', 'Payload/DmIOS.app/pso', *(['--skip-suffix', '_bp.metallib'] if skip_bp else []), *maps])
    if r.returncode: die('pack_ipa')
    sz = out_ipa.stat().st_size; t(f'done: {out_ipa} ({sz / 2**30:.1f} GB)' if sz >= 2**30 else f'done: {out_ipa} ({sz / 2**20:.1f} MB)')


if __name__ == '__main__':
    main()
