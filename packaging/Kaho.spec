# PyInstaller spec for a distributable Kaho.app.
#
# The install.sh path builds a venv into Application Support and points a thin
# launcher at it. That cannot be handed to someone else: the venv's python is a
# symlink into Homebrew, so a copied bundle is a dead link on a machine without
# Homebrew Python 3.13. This spec embeds the interpreter and every dependency
# instead, producing a bundle that runs on a stock Mac.
#
# Build:  pyinstaller packaging/Kaho.spec --noconfirm

import os
import re

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# The version comes from kaho.py's APP_VERSION. A literal default here drifted
# to 1.7.3 while the app shipped 1.7.9, so any build that forgot KAHO_VERSION
# would have been mislabelled. KAHO_VERSION still wins, for a one-off build.
with open(os.path.join(SPECPATH, "..", "kaho.py")) as _f:
    _app_version = re.search(r'^APP_VERSION = "([^"]+)"', _f.read(), re.MULTILINE).group(1)
VERSION = os.environ.get("KAHO_VERSION", _app_version)

# Naming lazy imports one at a time is a losing game — mlx alone failed on
# mlx._reprlib_fix, and each fix costs a 10-minute rebuild to discover the
# next one. collect_all sweeps up every submodule, data file, and dylib for
# the packages that load code dynamically.
_collected_datas, _collected_binaries, _collected_hidden = [], [], []
# transformers resolves AutoTokenizer through a lazy-module shim, so its
# submodules are invisible to static analysis too.
for _pkg in ("mlx", "mlx_whisper", "mlx_lm", "sounddevice", "transformers", "tokenizers"):
    _d, _b, _h = collect_all(_pkg)
    _collected_datas += _d
    _collected_binaries += _b
    _collected_hidden += _h

a = Analysis(
    ["../kaho.py"],
    pathex=[],
    binaries=_collected_binaries,
    datas=[("../assets/Kaho.icns", ".")] + _collected_datas,
    # mlx_whisper and mlx_lm resolve model code lazily, so PyInstaller's static
    # analysis misses these. mlx's C extension imports mlx._reprlib_fix and
    # friends at init time — there is no upstream PyInstaller hook for mlx, so
    # its submodules have to be named explicitly or the app dies on first
    # import with "No module named 'mlx._reprlib_fix'".
    hiddenimports=[
        "mlx",
        "mlx.core",
        "mlx.nn",
        "mlx.utils",
        "mlx.extension",
        "mlx._reprlib_fix",
        "mlx.__array_api_info",
        "mlx_whisper",
        "mlx_whisper.audio",
        "mlx_whisper.decoding",
        "mlx_whisper.load_models",
        "mlx_whisper.transcribe",
        "mlx_lm",
        "mlx_lm.models",
        "mlx_lm.tokenizer_utils",
        "mlx_lm.utils",
        "transformers",
        # transformers swaps itself for a _LazyModule that resolves names via
        # importlib at attribute-access time, so collect_all alone still left
        # AutoTokenizer unresolvable at runtime. Name the concrete modules.
        "transformers.models.auto",
        "transformers.models.auto.tokenization_auto",
        "transformers.models.auto.configuration_auto",
        "transformers.models.qwen2",
        "transformers.models.qwen2.tokenization_qwen2",
        "transformers.tokenization_utils",
        "transformers.tokenization_utils_base",
        "transformers.tokenization_utils_fast",
        "tokenizers",
        "sounddevice",
        "huggingface_hub",
    ] + _collected_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Excluding torch submodules looks like free size savings and is not:
    # torch.utils.data.dataloader imports torch.distributed unconditionally,
    # so excluding it broke the whole torch -> transformers -> AutoTokenizer
    # chain, surfacing three layers later as a bogus "AutoTokenizer" error.
    # Only exclude packages nothing in the import graph reaches.
    # torch is declared by mlx-whisper but never reached: it lives only in
    # torch_whisper.py, a conversion module nothing imports. ~530 MB saved.
    #
    # numba and scipy CANNOT be excluded despite only being used for word-level
    # timestamps we never request — transcribe.py imports timing.py at module
    # load, so the app dies with ModuleNotFoundError on startup. Tested.
    excludes=[
        "torch",
        "tkinter",
        "matplotlib",
        "PIL",
        "pytest",
        "IPython",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Kaho",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,  # signed separately, after the bundle is assembled
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Kaho",
)

app = BUNDLE(
    coll,
    name="Kaho.app",
    icon="../assets/Kaho.icns",
    bundle_identifier="com.utsavanand.kaho",
    info_plist={
        "CFBundleName": "Kaho",
        "CFBundleDisplayName": "Kaho",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": True,  # menu bar only, no Dock icon by default
        "LSMinimumSystemVersion": "14.0",
        "NSMicrophoneUsageDescription":
            "Kaho records while you hold the hotkey and transcribes on-device.",
        "NSHighResolutionCapable": True,
    },
)
