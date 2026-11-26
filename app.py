import os, asyncio, httpx, json, time
from playwright.async_api import async_playwright
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
import uvicorn

RAILWAY = os.environ.get("CENTRAL_SERVER", "https://zoom-server-new.up.railway.app").rstrip("/")
EMAIL = os.environ.get("ZOOM_EMAIL", "").strip()
PASSWORD = os.environ.get("ZOOM_PASSWORD", "").strip()
CHECK_EVERY = int(os.environ.get("CHECK_INTERVAL", "30"))
SECRET = os.environ.get("KEEPER_SECRET", "keeper-secret-change-me")
PROFILE_DIR = "/tmp/zoom_profile"

app = FastAPI()

@app.get("/")
@app.get("/health")
async def health():
    return PlainTextResponse("session-keeper ok")

def cookie_jar(data):
    jar = {}
    for c in (data or {}).get("cookies") or []:
        name = c.get("name")
        if name and "zoom" in (c.get("domain") or ""):
            jar[name] = c.get("value", "")
    return jar

async def is_valid(cookies_data):
    if not cookies_data or not cookies_data.get("cookies"):
        return False
    try:
        async with httpx.AsyncClient(timeout=20, cookies=cookie_jar(cookies_data), follow_redirects=True) as c:
            r = await c.get("https://zoom.us/profile")
            url = str(r.url).lower()
            return r.status_code == 200 and "signin" not in url and "login" not in url
    except Exception:
        return False

async def safe_fill(page, selectors, value, timeout=8000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(force=True, timeout=5000)
            await loc.fill("")
            await loc.fill(value)
            return True
        except Exception:
            continue
    return False

async def safe_click(page, selectors, timeout=8000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(force=True, timeout=5000)
            return True
        except Exception:
            continue
    return False

async def do_login_and_save():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True,
            viewport={"width": 1366, "height": 768},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto("https://zoom.us/signin#/login", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # Email
            ok = await safe_fill(page, [
                '//*[@id="email"]',
                '#email',
                'input[name="account"]',
                'input[type="email"]',
            ], EMAIL)
            if not ok:
                print("Email field not filled")
                return None
            await page.wait_for_timeout(800)

            # Next button
            await safe_click(page, [
                '//*[@id="signin_btn_next"]',
                '//*[@id="signin_btn_next"]/span',
                'button:has-text("Next")',
                '#signin_btn_next',
            ])
            await page.wait_for_timeout(3500)

            # Password
            ok = await safe_fill(page, [
                '//*[@id="password"]',
                '#password',
                'input[name="password"]',
                'input[type="password"]',
            ], PASSWORD)
            if not ok:
                print("Password field not filled")
                return None
            await page.wait_for_timeout(600)

            # Login button
            await safe_click(page, [
                '//*[@id="js_btn_login"]',
                'button:has-text("Sign In")',
                'button[type="submit"]',
                '#js_btn_login',
            ])
            await page.wait_for_timeout(10000)

            url = page.url.lower()
            if "signin" in url or "login" in url:
                print("Still on login page. URL:", page.url)
                return None

            state = await context.storage_state()
            now = time.time()
            for c in state.get("cookies", []):
                exp = c.get("expires", -1)
                if exp == -1 or exp < now + 72 * 3600:
                    c["expires"] = now + 72 * 3600
            print(f"Login OK, cookies: {len(state.get('cookies', []))}")
            return state
        except Exception as e:
            print("Login exception:", str(e)[:200])
            return None
        finally:
            await context.close()

async def push_to_server(state):
    if not state or not state.get("cookies"):
        return False
    headers = {"X-Keeper-Secret": SECRET, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post(f"{RAILWAY}/api/keeper-update-session", json=state, headers=headers)
            print(f"Push: {r.status_code}")
            return r.status_code in (200, 201)
    except Exception as e:
        print("Push error:", e)
        return False

async def keeper_loop():
    if not EMAIL or not PASSWORD:
        print("Set ZOOM_EMAIL and ZOOM_PASSWORD in Railway Variables")
        return
    print(f"Keeper running | check every {CHECK_EVERY}s")
    last_state = None
    while True:
        try:
            need_login = True
            if last_state and await is_valid(last_state):
                need_login = False
                print("Session valid")
            else:
                try:
                    async with httpx.AsyncClient(timeout=15) as c:
                        r = await c.get(f"{RAILWAY}/session")
                        if r.status_code == 200:
                            server_state = r.json()
                            if await is_valid(server_state):
                                last_state = server_state
                                need_login = False
                                print("Server session valid")
                except Exception:
                    pass

            if need_login:
                print("Re-login starting...")
                new_state = await do_login_and_save()
                if new_state:
                    ok = await push_to_server(new_state)
                    if ok:
                        last_state = new_state
                        print("New session pushed")
                    else:
                        print("Push failed")
                else:
                    print("Login failed")
        except Exception as e:
            print("Loop error:", str(e)[:200])
        await asyncio.sleep(CHECK_EVERY)

@app.on_event("startup")
async def startup():
    asyncio.create_task(keeper_loop())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
