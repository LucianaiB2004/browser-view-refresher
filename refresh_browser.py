"""
原地刷浏览量脚本 —— 支持掘金、CSDN视频、CSDN博客、魔搭。
多篇文章在【同一个 Chrome 窗口】中以多个标签页并发刷新。

工作原理:
    - 启动一个独立的 Chrome 窗口(使用从你真实 Chrome 复制的配置,含登录态)。
    - 每个网址打开在独立标签页中。
    - 主循环依次检查每个标签页,到时间就切换过去刷新并抓取浏览量。
    - 所有标签页共享同一个浏览器窗口,停止时按 Ctrl+C 或关闭窗口即可。

用法:
    交互式(双击桌面图标):
        python refresh_browser.py
        然后按提示逐行输入网址,空行结束。

    命令行:
        python refresh_browser.py <url1> [<url2> ...]
        python refresh_browser.py <url1> <url2> --interval 5
        python refresh_browser.py --refresh-profile
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager


# ----------------------------- 路径与常量 -----------------------------

PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = PROJECT_DIR / "chrome_profile"   # 自动化用的独立 Chrome 配置(含登录态)


def default_user_data_dir() -> str:
    local = os.environ.get("LOCALAPPDATA", "")
    return str(Path(local) / "Google" / "Chrome" / "User Data")


# ----------------------------- 工具函数 -----------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("网址不能为空")
    if not urlparse(url).scheme:
        url = "https://" + url
    return url


def is_chrome_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return "chrome.exe" in out.stdout
    except Exception:
        return False


# ----------------------------- 配置复制(仅首次) -----------------------------

EXCLUDE_DIR_NAMES = [
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
    "CacheStorage", "blob_storage", "File System",
    "Application Cache", "DawnCache", "DawnGraphiteCache",
    "Service Worker", "IndexedDB", "Session Storage",
]


def _robocopy_profile(src_profile: Path, dst_profile: Path) -> None:
    args = [
        "robocopy", str(src_profile), str(dst_profile),
        "/E", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/NS", "/NC", "/XJ",
    ]
    for d in EXCLUDE_DIR_NAMES:
        args += ["/XD", d]
    result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if result.returncode >= 8:
        raise RuntimeError(f"robocopy 失败(退出码 {result.returncode})")


def ensure_profile(src_user_data: str, profile_name: str, force_refresh: bool) -> None:
    """
    确保自动化配置存在于 PROFILE_DIR。
    若不存在则提示关闭 Chrome 后从源配置复制。
    兼容老版本:若 PROFILE_DIR 下已有 base/ 子目录,自动把它提升为 PROFILE_DIR。
    """
    # 兼容老版本:之前用 base/slot 结构,现在改成单一 profile
    base_subdir = PROFILE_DIR / "base"
    if base_subdir.exists() and not (PROFILE_DIR / profile_name).exists():
        try:
            # 把 base/ 里的内容移到 PROFILE_DIR 根
            for item in base_subdir.iterdir():
                target = PROFILE_DIR / item.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink()
                shutil.move(str(item), str(target))
            shutil.rmtree(base_subdir, ignore_errors=True)
            log("已自动迁移旧配置结构。")
        except Exception as exc:
            log(f"迁移旧配置时出错(继续尝试): {exc}")

    already = (PROFILE_DIR / profile_name).exists() and (PROFILE_DIR / "Local State").exists()
    if already and not force_refresh:
        return

    src = Path(src_user_data)
    if not src.exists():
        raise RuntimeError(f"源 Chrome 配置目录不存在: {src}")

    if is_chrome_running():
        log("")
        log("【首次准备】为了获取你已保存的登录信息(cookie),需要先完全关闭 Chrome。")
        log("复制完成后会自动启动独立的自动化 Chrome 窗口,不影响你之后重新打开 Chrome。")
        try:
            input("请关闭所有 Chrome 窗口后,按回车键继续(或按 Ctrl+C 取消)...")
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("用户取消。")
        for _ in range(15):
            if not is_chrome_running():
                break
            time.sleep(1)
        if is_chrome_running():
            raise RuntimeError("Chrome 似乎仍在运行,请确保所有 Chrome 窗口都已关闭后重试。")

    log(f"正在复制 Chrome 配置到: {PROFILE_DIR}")
    log("(首次复制可能需要十几秒到半分钟,请稍候...)")
    if PROFILE_DIR.exists():
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    local_state = src / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, PROFILE_DIR / "Local State")
    _robocopy_profile(src / profile_name, PROFILE_DIR / profile_name)

    cookies = [
        PROFILE_DIR / profile_name / "Network" / "Cookies",
        PROFILE_DIR / profile_name / "Cookies",
    ]
    if not any(p.exists() for p in cookies):
        raise RuntimeError("未找到 cookie 文件,请确认源 Chrome 已完全关闭。")
    log("配置复制完成。")


# ----------------------------- Chrome 管理 -----------------------------

def cleanup_stale_chrome() -> None:
    """杀掉使用 PROFILE_DIR 的残留 Chrome 进程并清掉锁文件。"""
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{PROFILE_DIR}*' }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                try:
                    subprocess.run(["taskkill", "/F", "/PID", line],
                                   capture_output=True, timeout=5)
                except Exception:
                    pass
    except Exception:
        pass
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = PROFILE_DIR / lock_name
        try:
            if p.exists() or p.is_symlink():
                p.unlink(missing_ok=True)
        except Exception:
            pass
    time.sleep(0.3)


def build_driver(profile_directory: str, show_browser: bool = True):
    options = ChromeOptions()
    if show_browser:
        options.add_argument("--start-maximized")
    else:
        # 后台静默运行,不弹出浏览器窗口
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument(f"--profile-directory={profile_directory}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-popup-blocking")
    # headless 下部分站点会识别并拦截,用一个常见的 UA 降低概率
    if not show_browser:
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    service = ChromeService(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


# ----------------------------- 浏览量抓取 -----------------------------

def extract_metric(driver, current_url: str) -> str:
    host = (urlparse(current_url).hostname or "").lower()
    path = urlparse(current_url).path or ""

    # CSDN 视频
    if host == "live.csdn.net" and path.startswith("/v/"):
        xp = '//*[@id="floor-videodetail-page_373"]/div/div/main/div/div[3]/div/p'
        try:
            for el in driver.find_elements(By.XPATH, xp):
                raw = (el.text or "").strip()
                if not raw:
                    continue
                m = re.match(r"^\s*([\d,\.]+\s*[kKwWw万]?)", raw)
                if m:
                    return f"播放量: {m.group(1).strip()}"
                head = raw.split("·")[0].strip()
                return f"播放量: {head or raw}"
        except Exception:
            pass
        return "播放量: N/A"

    # 掘金
    if host == "juejin.cn" and path.startswith("/post/"):
        xp = '//*[@id="juejin"]/div[1]/div/main/div/div[1]/article/div[3]/div[1]/div[2]/span[1]'
        try:
            for el in driver.find_elements(By.XPATH, xp):
                txt = (el.text or "").strip()
                if txt:
                    return f"浏览量: {txt}"
        except Exception:
            pass
        return "浏览量: N/A"

    # 魔搭(modelscope.cn)
    if host.endswith("modelscope.cn"):
        return "浏览量: N/A"

    # CSDN 博客
    if host == "blog.csdn.net" and "/article/details/" in path:
        for xp in (
            '//span[contains(@class,"read-count")]',
            '//*[contains(@class,"bar-content")]//*[contains(@class,"num")]',
        ):
            try:
                for el in driver.find_elements(By.XPATH, xp):
                    for attr in ("title", "data-count", "data-vc", "data-num"):
                        v = el.get_attribute(attr)
                        if v and any(c.isdigit() for c in v):
                            return f"阅读量: {v.strip()}"
                    txt = (el.text or "").strip()
                    if txt and any(c.isdigit() for c in txt):
                        return f"阅读量: {txt}"
            except Exception:
                continue
        return "阅读量: N/A"

    return "N/A"


# ----------------------------- 多标签页刷新逻辑 -----------------------------

class TabTask:
    def __init__(self, index: int, url: str, interval: float):
        self.index = index
        self.url = url
        self.interval = interval
        self.handle: str | None = None
        self.last_refresh: float = 0.0
        self.tag = f"[#{index}]"


def open_tabs(driver, tasks: list[TabTask]) -> None:
    """在同一个浏览器窗口中为每个任务打开一个标签页。"""
    # 第一个标签页已由 driver.get 打开,剩下的用 window.open
    first = tasks[0]
    driver.get(first.url)
    first.handle = driver.current_window_handle
    # 让页面加载一下再抓第一次指标
    time.sleep(2.5)
    metric = extract_metric(driver, driver.current_url)
    log(f"{first.tag}[{time.strftime('%H:%M:%S')}] 访问 {driver.current_url} | {metric}")
    first.last_refresh = time.time()

    for task in tasks[1:]:
        driver.execute_script(f"window.open({task.url!r}, '_blank');")
        driver.switch_to.window(driver.window_handles[-1])
        task.handle = driver.current_window_handle
        time.sleep(2.5)
        metric = extract_metric(driver, driver.current_url)
        log(f"{task.tag}[{time.strftime('%H:%M:%S')}] 访问 {driver.current_url} | {metric}")
        task.last_refresh = time.time()

    # 切回第一个标签
    driver.switch_to.window(tasks[0].handle)


def refresh_loop(driver, tasks: list[TabTask]) -> None:
    """主循环:轮询每个标签,到时间就刷新。"""
    handle_to_task = {t.handle: t for t in tasks}
    while True:
        now = time.time()
        handles = driver.window_handles
        alive_tasks = [t for t in tasks if t.handle in handles]

        # 如果所有标签都被关了,退出
        if not alive_tasks:
            log("所有标签页已关闭,退出。")
            return

        for task in alive_tasks:
            if now - task.last_refresh >= task.interval:
                try:
                    driver.switch_to.window(task.handle)
                    driver.refresh()
                    time.sleep(1.5)
                    metric = extract_metric(driver, driver.current_url)
                    log(f"{task.tag}[{time.strftime('%H:%M:%S')}] 访问 {driver.current_url} | {metric}")
                    task.last_refresh = time.time()
                except WebDriverException as exc:
                    text = str(exc).lower()
                    if any(k in text for k in ("no such window", "chrome not reachable",
                                               "target window already closed", "invalid session id")):
                        log(f"{task.tag} 标签页已关闭,不再刷新该任务。")
                        if task.handle in handle_to_task:
                            handle_to_task.pop(task.handle, None)
                        # 从任务列表移除
                        tasks[:] = [t for t in tasks if t.handle != task.handle]
                        if tasks:
                            driver.switch_to.window(tasks[0].handle)
                        break
                    else:
                        # 其它瞬时错误,等一下再试
                        time.sleep(1)
        time.sleep(0.5)


# ----------------------------- 输入 -----------------------------

def read_urls_interactive() -> tuple[list[str], float | None, bool | None]:
    """
    交互式输入:依次输入刷新间隔、是否显示浏览器、网址。
    返回 (网址列表, 间隔秒数, 是否显示浏览器);None 表示用默认值。
    """
    bar = "=" * 44
    print(bar)
    print("原地刷浏览量脚本（支持掘金，CSDN视频，魔搭）")
    print(bar)

    # 1) 输入刷新间隔
    interval: float | None = None
    while True:
        try:
            raw = input("请输入刷新间隔(秒,直接回车默认 5): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return [], None, None
        if not raw:
            interval = 5.0
            break
        try:
            value = float(raw)
            if value <= 0:
                print("间隔必须大于 0,请重新输入。")
                continue
            interval = value
            break
        except ValueError:
            print("请输入一个数字,例如 5、10、3.5。")

    # 2) 选择是否显示浏览器
    show_browser: bool | None = None
    while True:
        try:
            raw = input("是否显示浏览器窗口?(Y=显示/N=后台静默运行,直接回车默认 Y): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return [], interval, None
        if raw in ("", "y", "yes", "是"):
            show_browser = True
            break
        if raw in ("n", "no", "否"):
            show_browser = False
            break
        print("请输入 Y 或 N。")

    # 3) 输入网址
    print()
    print("请逐行输入要刷新的网址(每行一个);")
    print("直接输入空行结束并开始刷新。")
    print()
    urls: list[str] = []
    while True:
        try:
            line = input(f"网址 {len(urls) + 1}(空行结束): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            break
        urls.append(line)
    print(bar)
    return urls, interval, show_browser


# ----------------------------- 主流程 -----------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="原地刷浏览量脚本 —— 支持掘金/CSDN视频/魔搭,多标签页同窗口刷新"
    )
    parser.add_argument("urls", nargs="*", help="要刷新的网址(可传多个,每个打开为一个标签页)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="每个标签页两次刷新之间的间隔秒数(默认 5)")
    parser.add_argument("--user-data-dir", default=default_user_data_dir(),
                        help="你真实的 Chrome User Data 目录路径")
    parser.add_argument("--profile-directory", default="Default",
                        help="Chrome 配置子目录名(默认 Default)")
    parser.add_argument("--refresh-profile", action="store_true",
                        help="重新从源 Chrome 复制配置(会覆盖现有自动化配置)")
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument("--show", dest="show_browser", action="store_true", default=None,
                               help="显示浏览器窗口运行(默认)")
    browser_group.add_argument("--headless", "--background", dest="show_browser", action="store_false",
                               help="后台静默运行,不弹出浏览器窗口")
    args = parser.parse_args()

    # 横幅 + 收集网址
    bar = "=" * 44
    if args.urls:
        print(bar)
        print("原地刷浏览量脚本（支持掘金，CSDN视频，魔搭）")
        print(bar)
        print(bar)
        urls_raw = list(args.urls)
        interval = args.interval
        show_browser = True if args.show_browser is None else args.show_browser
    else:
        urls_raw, interval_input, show_input = read_urls_interactive()
        interval = interval_input if interval_input is not None else args.interval
        show_browser = show_input if show_input is not None else (
            True if args.show_browser is None else args.show_browser
        )

    if not urls_raw:
        print("未输入任何网址,已退出。")
        return 1

    urls: list[str] = []
    for u in urls_raw:
        try:
            urls.append(normalize_url(u))
        except ValueError as exc:
            print(f"忽略无效网址 {u!r}: {exc}")
    if not urls:
        return 1

    tasks = [TabTask(i + 1, url, interval) for i, url in enumerate(urls)]
    mode_text = "显示浏览器" if show_browser else "后台静默运行(不显示浏览器)"
    if len(tasks) > 1:
        log(f"将在同一个 Chrome 窗口中打开 {len(tasks)} 个标签页,每个间隔 {interval} 秒刷新;运行模式: {mode_text}。")
    else:
        log(f"运行模式: {mode_text}。")

    # 准备配置
    try:
        ensure_profile(args.user_data_dir, args.profile_directory, args.refresh_profile)
    except Exception as exc:
        log(f"配置准备失败: {exc}")
        return 1

    # 清理残留并启动
    cleanup_stale_chrome()
    try:
        driver = build_driver(args.profile_directory, show_browser=show_browser)
    except Exception as exc:
        log(f"启动 Chrome 失败: {exc}")
        log("排查建议: 任务管理器结束所有 chrome.exe 后重试;或删除 chrome_profile 文件夹重新复制。")
        return 2

    try:
        open_tabs(driver, tasks)
        refresh_loop(driver, tasks)
    except KeyboardInterrupt:
        log("\n收到 Ctrl+C,正在关闭浏览器...")
    except WebDriverException as exc:
        text = str(exc).lower()
        if any(k in text for k in ("chrome not reachable", "invalid session id", "disconnected")):
            log("浏览器已被关闭,脚本退出。")
        else:
            log(f"浏览器连接断开: {str(exc).splitlines()[0]}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
