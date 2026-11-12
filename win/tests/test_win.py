#!/usr/bin/env python3
# Unit tests of the Windows port's pure-Python parts (no SDK, Metal tools or game files needed): python win/tests/test_win.py
import io, lzma, os, plistlib, struct, subprocess, sys, tempfile, unittest, zlib, zipfile
from pathlib import Path
WIN = Path(__file__).resolve().parents[1]
REPO = WIN.parent
sys.path.insert(0, str(WIN)); sys.path.insert(0, str(WIN / 'sdk'))
import xcrun, build_app, extract_xip


class Xcrun(unittest.TestCase):
    def test_ios_min_becomes_target(self):
        a = xcrun.metal_args('iphoneos', ['-c', '-mios-version-min=18.0', '-fmetal-math-mode=relaxed', 'x.metal', '-o', 'x.air'])
        i = a.index('-target'); self.assertEqual(a[i + 1], 'air64-apple-ios18.0')
        self.assertNotIn('-mios-version-min=18.0', a)

    def test_macos_gets_default_target(self):
        a = xcrun.metal_args('macosx', ['-c', '-fpreserve-invariance', 'x.metal', '-o', 'x.air'])
        self.assertEqual(a[0], '-target'); self.assertTrue(a[1].startswith('air64-apple-macos'))

    def test_version_query_untouched(self):
        self.assertEqual(xcrun.metal_args('iphoneos', ['--version']), ['--version'])


class Shim(unittest.TestCase):
    def run_mapped(self, code):
        env = dict(os.environ, PYTHONPATH=str(WIN / 'pyshim'), NXW_SHIM_TRACE='1')
        return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env)

    @unittest.skipUnless(os.name == 'nt', 'the shim is active on Windows only')
    def test_which_python3_and_cdll(self):
        r = self.run_mapped('import subprocess, ctypes, sys\n'
                            'p = subprocess.run(["which", "python"], capture_output=True, text=True); print("which", p.returncode, bool(p.stdout.strip()))\n'
                            'p = subprocess.run(["python3", "-c", "print(42)"], capture_output=True, text=True); print("py3", p.stdout.strip())\n'
                            'print("clone", getattr(ctypes.CDLL(None), "clonefile", None))')
        self.assertIn('which 0 True', r.stdout, r.stderr); self.assertIn('py3 42', r.stdout); self.assertIn('clone None', r.stdout)

    @unittest.skipUnless(os.name == 'nt', 'the shim is active on Windows only')
    def test_xlate_and_build_ipa_mapping(self):
        r = self.run_mapped('import sitecustomize, subprocess\n'
                            'try: subprocess.run(["C:/r/dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate", "--source-sha"])\nexcept OSError: pass\n'
                            'try: subprocess.run(["bash", "C:/r/dm/ios/tools/build_ipa.sh", "out.ipa"], capture_output=True)\nexcept OSError: pass\n')
        self.assertIn("dmsh_xlate.dll', '--source-sha']", r.stderr)
        self.assertIn("build_app.py', 'out.ipa']", r.stderr)


class HostSources(unittest.TestCase):
    def test_matches_makefile(self):
        files, incs = build_app.host_sources(Path('gen/x'))
        names = {f.name for f in files}
        self.assertIn('main.c', names); self.assertIn('entry.S', names); self.assertNotIn('front_mac.m', names)
        self.assertTrue(any(f.parent.name == 'nvn' for f in files)); self.assertTrue(any('libvpx' in str(f) for f in files))
        self.assertTrue(all(f.exists() for f in files))
        self.assertEqual(incs[0], (REPO / 'dm/gfx/nvn/include').resolve())


class Plist(unittest.TestCase):
    def test_keys(self):
        p = build_app.info_plist('org.nxnative.x', 'Game', '1.2.1', 'public.app-category.games', False, 'en-US', '26.0', '23A1', True)
        self.assertEqual(p['CFBundleExecutable'], 'DmIOS'); self.assertEqual(p['MinimumOSVersion'], '26.0')
        self.assertEqual(p['UIApplicationSceneManifest']['UISceneConfigurations']['UIWindowSceneSessionRoleApplication'][0]['UISceneDelegateClassName'], 'SceneDelegate')
        plistlib.dumps(p, fmt=plistlib.FMT_BINARY)


def odc(name, mode, data=b'', ino=1):
    n = name.encode() + b'\0'
    h = b'070707' + b'%06o' % 0 + b'%06o' % ino + b'%06o' % mode + b'%06o' % 0 * 2 + b'%06o' % 1 + b'%06o' % 0 + b'%011o' % 0 + b'%06o' % len(n) + b'%011o' % len(data)
    return h + n + data


class Xip(unittest.TestCase):
    def test_extracts_only_the_sdk(self):
        sdk = './Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/'
        cpio = (odc('./Xcode.app/Contents/Info.plist', 0o100644, b'skip me') + odc(sdk + 'iPhoneOS.sdk', 0o040755) +
                odc(sdk + 'iPhoneOS.sdk/SDKSettings.json', 0o100644, b'{"Version": "26.0"}', ino=7) +
                odc(sdk + 'iPhoneOS.sdk/usr/lib/libz.tbd', 0o120755, b'libz.1.tbd') + odc(sdk + 'iPhoneOS.sdk/usr/lib/libz.1.tbd', 0o100644, b'tbd', ino=8) +
                odc(sdk + 'iPhoneOS.sdk/usr/include/canary.h', 0o100644, b'NULLcanary' + b'\0' * 6, ino=9) +   # Xcode 27: a hard link whose bytes another entry carries
                odc('./Xcode.app/Contents/Developer/Platforms/MacOSX.platform/x/canary.h', 0o100644, b'#define REAL 1\n\0', ino=9) +
                odc(sdk + 'iPhoneOS.sdk/F.framework/Versions/Current', 0o120755, b'A') + odc(sdk + 'iPhoneOS.sdk/F.framework/Headers', 0o120755, b'Versions/Current/Headers') +
                odc(sdk + 'iPhoneOS.sdk/F.framework/Versions/A/Headers/f.h', 0o100644, b'int f;', ino=10) +
                odc(sdk + 'iPhoneOS26.0.sdk', 0o120755, b'iPhoneOS.sdk') + odc('TRAILER!!!', 0))
        half = len(cpio) // 2
        body = b'pbzx' + struct.pack('>Q', 1 << 24)
        for part in (cpio[:half], cpio[half:]):
            c = lzma.compress(part, format=lzma.FORMAT_XZ); body += struct.pack('>QQ', 1 << 24, len(c)) + c
        toc = (f'<?xml version="1.0"?><xar><toc><file id="1"><name>Content</name><data><offset>0</offset><length>{len(body)}</length>'
               '<encoding style="application/octet-stream"/></data></file></toc></xar>').encode()
        tc = zlib.compress(toc)
        xip = b'xar!' + struct.pack('>HHQQI', 28, 1, len(tc), len(toc), 0) + tc + body
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'x.xip'; p.write_bytes(xip); out = Path(d) / 'out'
            sys.argv = ['extract_xip.py', str(p), str(out)]
            extract_xip.main()
            self.assertEqual((out / 'iPhoneOS.sdk/SDKSettings.json').read_bytes(), b'{"Version": "26.0"}')
            self.assertEqual((out / 'iPhoneOS.sdk/usr/lib/libz.tbd').read_bytes(), b'tbd')
            self.assertFalse((out / 'Info.plist').exists()); self.assertFalse((out / 'iPhoneOS26.0.sdk').exists())
            self.assertEqual((out / 'iPhoneOS.sdk/usr/include/canary.h').read_bytes(), b'#define REAL 1\n\0')
            self.assertEqual((out / 'iPhoneOS.sdk/F.framework/Headers/f.h').read_bytes(), b'int f;')   # a link through a link


class BcSoft(unittest.TestCase):
    """dm/gfx/tex/bc_soft.c, built for this PC as a DLL without a C runtime, against Pillow's BCn decoder: BC1–BC5 within interpolation rounding, BC7 and unsigned BC6H exact.
    Signed BC6H is checked in its modes without deltas (9, 10, 13) only: in the delta modes Pillow does not wrap an endpoint to the endpoint precision as the format specification does"""
    @classmethod
    def setUpClass(cls):
        import ctypes, shutil
        clang = shutil.which('clang') or (r'C:\Program Files\LLVM\bin\clang.exe' if os.path.exists(r'C:\Program Files\LLVM\bin\clang.exe') else None)
        if os.name != 'nt' or not clang: raise unittest.SkipTest('needs clang on Windows')
        cls.tmp = tempfile.mkdtemp(); crt = Path(cls.tmp) / 'crt.c'; dll = Path(cls.tmp) / 'bc.dll'
        crt.write_text('#include <stddef.h>\nvoid *memcpy(void *d, const void *s, size_t n){ char *a=d; const char *b=s; while(n--) *a++=*b++; return d; }\n'
                       'void *memset(void *d, int c, size_t n){ char *a=d; while(n--) *a++=(char)c; return d; }\nint _fltused;\n')
        tex = REPO / 'dm/gfx/tex'
        r = subprocess.run([clang, '--target=x86_64-pc-windows-msvc', '-O2', '-ffreestanding', '-fno-builtin', '-nostdlib', '-shared', '-fuse-ld=lld', '-Wl,/NOENTRY',
                            '-Wl,/EXPORT:bc_soft_block', '-Wl,/EXPORT:bc_soft_out_block', f'-I{tex}', '-o', str(dll), str(tex / 'bc_soft.c'), str(crt)], capture_output=True, text=True)
        if r.returncode: raise unittest.SkipTest('could not build bc_soft.c: ' + r.stderr[-300:])
        cls.lib = ctypes.CDLL(str(dll))

    def block(self, pf, blk, bpp):
        import ctypes
        out = (ctypes.c_uint8 * (16 * bpp))()
        self.assertEqual(self.lib.bc_soft_block(pf, blk, out, 4 * bpp), 0)
        return bytes(out)

    def test_astc(self):
        """BC → ASTC 4x4 (nvn_bc_astc), decoded by texture2ddecoder: BC1 four-colour blocks keep their palette (within 2), other blocks are a close fit and never the error colour"""
        try: import texture2ddecoder
        except ImportError: raise unittest.SkipTest('pip install texture2ddecoder')
        import ctypes, random
        rnd = random.Random(11)
        def astc(pf, b):
            out = (ctypes.c_uint8 * 16)(); self.assertEqual(self.lib.bc_soft_out_block(pf, b, out, 16), 0)
            bgra = texture2ddecoder.decode_astc(bytes(out), 4, 4, 4, 4)
            return bytes(x for i in range(16) for x in (bgra[i * 4 + 2], bgra[i * 4 + 1], bgra[i * 4], bgra[i * 4 + 3]))
        worst = {}
        for pf, size in ((130, 8), (134, 16), (152, 16)):
            errs = []
            for _ in range(400):
                b = bytes(rnd.getrandbits(8) for _ in range(size))
                if pf == 130 and b[0] | b[1] << 8 <= b[2] | b[3] << 8: b = b[2:4] + b[0:2] + b[4:]
                ref, got = self.block(pf, b, 4), astc(pf, b)
                if pf == 130 and b[0] | b[1] << 8 > b[2] | b[3] << 8: self.assertLessEqual(max(abs(x - y) for x, y in zip(ref, got)), 2, 'BC1 palette')
                errs.append(sum((x - y) ** 2 for x, y in zip(ref, got)) / 64)
            mse = sum(errs) / len(errs); worst[pf] = mse
        print('ASTC mean squared error per channel: BC1 %.2f, BC3 %.1f, BC7 %.1f (random blocks)' % (worst[130], worst[134], worst[152]))
        self.assertLess(worst[130], 1.0)
        for _ in range(300):   # opaque BC3 (alpha 255): the colour part is a BC1 palette, fitted with 3-bit weights
            b = bytes([255, 255, 0, 0, 0, 0, 0, 0]) + bytes(rnd.getrandbits(8) for _ in range(8))
            ref, got = self.block(134, b, 4), astc(134, b)
            self.assertLessEqual(max(abs(x - y) for x, y in zip(ref, got)), 24, 'opaque BC3')

    def test_against_pillow(self):
        import random, struct
        from PIL import Image
        rnd = random.Random(7)
        def blk(n): return bytes(rnd.getrandbits(8) for _ in range(n))
        def pil(n, fmt, mode, b): return Image.frombytes(mode, (4, 4), b, 'bcn', (n, fmt)).tobytes()
        for pf, n, fmt, mode, size, bpp, tol in ((130, 1, 'BC1', 'RGBA', 8, 4, 2), (132, 2, 'BC2', 'RGBA', 16, 4, 2), (134, 3, 'BC3', 'RGBA', 16, 4, 2), (140, 4, 'BC4', 'L', 8, 1, 1)):
            for _ in range(300):
                b = blk(size); a, p = self.block(pf, b, bpp), pil(n, fmt, mode, b)
                self.assertLessEqual(max(abs(x - y) for x, y in zip(a, p)), tol, fmt)
        for _ in range(300):
            b = blk(16); a, p = self.block(142, b, 2), pil(5, 'BC5', 'RGB', b)
            self.assertLessEqual(max(max(abs(a[2 * i] - p[3 * i]), abs(a[2 * i + 1] - p[3 * i + 1])) for i in range(16)), 1, 'BC5')
        for m in range(8):
            for _ in range(300):
                b = bytearray(blk(16)); b[0] = (b[0] & ~((1 << (m + 1)) - 1) & 0xff) | (1 << m)
                self.assertEqual(self.block(152, bytes(b), 4), pil(7, 'BC7', 'RGBA', bytes(b)), f'BC7 mode {m}')
        def half(h):
            s = -1 if h & 0x8000 else 1; e = (h >> 10) & 31; mm = h & 1023
            return s * (mm / 1024 * 2 ** -14 if e == 0 else float('inf') if e == 31 else (1 + mm / 1024) * 2 ** (e - 15))
        modes = [0, 1, 0x02, 0x06, 0x0a, 0x0e, 0x12, 0x16, 0x1a, 0x1e, 0x03, 0x07, 0x0b, 0x0f]
        for pf, fmt, which in ((151, 'BC6H', range(14)), (150, 'BC6HS', (9, 10, 13))):
            for mi in which:
                for _ in range(200):
                    b = bytearray(blk(16)); bits = 2 if mi < 2 else 5; b[0] = (b[0] & ~((1 << bits) - 1) & 0xff) | modes[mi]
                    hs = struct.unpack('<64H', self.block(pf, bytes(b), 8)); p = pil(6, fmt, 'RGB', bytes(b))
                    f8 = [max(0, min(255, round(half(hs[4 * i + c]) * 255))) for i in range(16) for c in range(3)]
                    self.assertLessEqual(max(abs(x - y) for x, y in zip(f8, p)), 1, f'{fmt} mode {mi}')


class UnpackRomfsImage(unittest.TestCase):
    def test_round_trip_and_reuse(self):
        sys.path.insert(0, str(REPO / 'dm/tools')); import romfs_image
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'src'; (src / 'arc/sub').mkdir(parents=True); (src / 'ui').mkdir()
            files = {'arc/data.trpfd': b'index', 'arc/sub/big.bin': os.urandom(3 << 20), 'ui/empty.bin': b'', 'top.txt': b'top'}
            for rel, b in files.items(): (src / rel).write_bytes(b)
            img = Path(d) / 'RomFS.img'; romfs_image.pack(src, img)
            out = build_app.unpack_romfs_image(img, Path(d) / 'unpacked')
            self.assertEqual({p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob('*') if p.is_file()}, files)
            (out / 'top.txt').write_bytes(b'marker')   # an unchanged image reuses the folder
            build_app.unpack_romfs_image(img, out); self.assertEqual((out / 'top.txt').read_bytes(), b'marker')


class PackIpa(unittest.TestCase):
    def test_exec_bit_on_windows(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / 'DmIOS.app'; app.mkdir(); (app / 'DmIOS').write_bytes(b'\xcf\xfa\xed\xfe'); (app / 'Info.plist').write_bytes(b'x')
            ipa = Path(d) / 'a.ipa'
            subprocess.run([sys.executable, str(REPO / 'dm/ios/tools/pack_ipa.py'), str(ipa), '--exec', 'Payload/DmIOS.app/DmIOS', f'Payload/DmIOS.app={app}'], check=True, capture_output=True)
            with zipfile.ZipFile(ipa) as z: modes = {i.filename: i.external_attr >> 16 for i in z.infolist()}
            self.assertEqual(modes['Payload/DmIOS.app/DmIOS'] & 0o777, 0o755)
            self.assertEqual(modes['Payload/DmIOS.app/Info.plist'] & 0o777, 0o644 if os.name == 'nt' else modes['Payload/DmIOS.app/Info.plist'] & 0o777)


if __name__ == '__main__':
    unittest.main(verbosity=2)
