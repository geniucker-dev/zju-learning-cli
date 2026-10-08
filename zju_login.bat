@echo off & chcp 65001 >nul & set "PYTHONUTF8=1" & title ZJU login & py -3.11 -x "%~f0" %* & echo. & pause & goto :eof
"""双击执行的 `zju.py login`：每次都是全新的登录。

第一行是 cmd 的批次指令，`py -x` 会跳过它，所以从这里开始才是 Python。
开始前先清掉上一次留下的学号（config.json）、密码（系统凭据库）和 session cookie，
再交给 zju.py 的 login 重新问学号和密码。zju.py 本身不改。
"""
import importlib.util
import shutil
import sys
from pathlib import Path

here = Path(sys.argv[0]).resolve().parent
spec = importlib.util.spec_from_file_location("zju", here / "zju.py")
zju = importlib.util.module_from_spec(spec)
sys.modules["zju"] = zju
spec.loader.exec_module(zju)

old_user = zju.load_config().get("username")
if old_user:
    try:
        import keyring
        keyring.delete_password(zju.KEYCHAIN_SERVICE, old_user)
    except Exception:
        pass  # 本来就没存密码
shutil.rmtree(zju.STATE_DIR, ignore_errors=True)

sys.argv = ["zju.py", "login", *sys.argv[1:]]
zju.main()
