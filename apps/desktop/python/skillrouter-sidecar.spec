# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 Python 边车打成 onedir `skillrouter-server`（spec §2.7）。

关键点（不要「优化」掉）：
- datas 里 **两份** migrations：`migrations`（bootstrap.MIGRATIONS_DIR 的推导结果）与
  `apps/api/migrations`（scripts/adopt_alembic.py 的推导结果）；
- hiddenimports 显式列 `skillrouter_api.main`（P0-2：launcher 静态 import 它，这里再列一次做双保险），
  并用 collect_submodules 整包收集 alembic / uvicorn / sqlalchemy / fastapi 以覆盖运行期导入（R3）；
- 相对 datas 路径相对 **spec 文件所在目录**（SPECPATH）解析，不是 cwd（P2-2）。
"""
from PyInstaller.utils.hooks import collect_submodules
import os

# SPECPATH 是 spec 文件所在目录（apps/desktop/python）；仓库根在它上面三层。
# 必须把 apps/api 放进 pathex，否则分析期 import 不到 skillrouter_api，冻结后
# `--role api` 会 ModuleNotFoundError —— 这是 P0-2 的另一种触发方式（实施期实测到了）。
REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, '..', '..', '..'))
API_ROOT = os.path.join(REPO_ROOT, 'apps', 'api')

hidden = [
    # P0-2：冻结后 `--role api` 必须能 import 到 ASGI 应用
    'skillrouter_api.main',
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops.asyncio',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    # P2-1：不列 uvicorn.protocols.websockets.auto（与 ws="none" 语义相反）
    'alembic',
    'alembic.config',
    'alembic.command',
    'alembic.script',
    'sqlalchemy.dialects.sqlite',
    'sqlite3',
    'jsonschema',
    'jsonschema.validators',
    'pydantic_settings',
    'pydantic.deprecated',
    'argon2',
    'cryptography.hazmat.primitives.ciphers.aead',
    'itsdangerous',
    'multipart',
    'pypdf',
    'yaml',
    'email.mime.text',
    'skillrouter_api.worker.runner',
    'skillrouter_api.worker.handlers',
    'skillrouter_api.executors.http',
    'skillrouter_api.executors.async_poll',
    # R3 的结构性加固：这些包的内部导入大量发生在**运行期**（迁移 env.py、协议选择、
    # dialect 加载），逐个手写 hiddenimports 永远漏，所以整包收集。
] + collect_submodules('alembic') \
  + collect_submodules('uvicorn') \
  + collect_submodules('sqlalchemy') \
  + collect_submodules('fastapi') \
  + collect_submodules('pydantic_settings')

a = Analysis(
    ['launcher.py'],
    pathex=[API_ROOT],
    binaries=[],
    datas=[
        ('../../../apps/api/migrations', 'migrations'),              # bootstrap 推导路径
        ('../../../apps/api/migrations', 'apps/api/migrations'),     # adopt_alembic 推导路径
        ('../../../scripts/adopt_alembic.py', 'scripts'),            # bootstrap._adopt_module 推导路径
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'PyQt5',
        'matplotlib',
        'pytest',
        'respx',
        'mypy',
        'ruff',
        # 与 http="h11" / ws="none" / loop="asyncio" 一致：不要 C 加速与 ws 实现
        'httptools',
        'uvloop',
        'websockets',
        'wsproto',
        'watchfiles',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.http.httptools_impl',
    ],
    noarchive=False,
)

# PyInstaller 6.x 已移除字节码加密（cipher）与 win_no_prefer_redirects / win_private_assemblies，
# 因此这里保持最小签名 —— 显式写死会直接让 spec 解析失败。
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='skillrouter-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # console=True：stdout 由主进程接管道写日志（并用 windowsHide 隐藏窗口）
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='skillrouter-server',
)
