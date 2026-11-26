import os, asyncio, httpx, json, time
from playwright.async_api import async_playwright
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
import uvicorn

RAILWAY = os.environ.get("CENTRAL_SERVER", "https://zoom-server-new.up.railway.app").rstrip("/")
EMAIL = os.environ.get("ZOOM_EMAIL", "").strip()
PASSWORD = os.environ.get("ZOOM_PASSWORD", "").strip()
CHECK_EVERY = int(os.environ.get("CHECK_INTERVAL", "45"))
SECRET = os.environ.get("KEEPER_SECRET", "keeper-secret-change-me")

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

async def try_fill(page, selectors, value):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            await loc.wait_for(state="visible", timeout=6000)
            await loc.click(force=True, timeout=4000)
            await asyncio.sleep(0.3)
            await loc.fill("")
            await loc.fill(value)
            return True
        except Exception:
            continue
    return False

async def try_click(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            await loc.wait_for(state="visible", timeout=6000)
            await loc.click(force=True, timeout=4000)
            return True
        except Exception:
            continue
    return False

async def do_login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        try:
            print("Opening signin page...")
            await page.goto("https://zoom.us/signin", timeout=60000)
            await page.wait_for_timeout(4000)

            email_ok = await try_fill(page, [
                '#email',
                'input[name="account"]',
                'input[name="email"]',
                'input[type="email"]',
                '//input[@id="email"]',
            ], EMAIL)

            if not email_ok:
                await try_click(page, [
                    'a:has-text("Sign in")',
                    'button:has-text("Sign In")',
                    'text=Sign in with email',
                ])
                await page.wait_for_timeout(2000)
                email_ok = await try_fill(page, [
                    '#email',
                    'input[name="account"]',
                    'input[type="email"]',
                ], EMAIL)

            if not email_ok:
                print("Email field still not found. URL:", page.url)
                return None

            print("Email filled")
            await page.wait_for_timeout(1000)

            await try_click(page, [
                '#signin_btn_next',
                'button:has-text("Next")',
                '//*[@id="signin_btn_next"]',
            ])
            await page.wait_for_timeout(4000)

            pwd_ok = await try_fill(page, [
                '#password',
                'input[name="password"]',
                'input[type="password"]',
                '//input[@id="password"]',
            ], PASSWORD)
            if not pwd_ok:
                print("Password field not found")
                return None
            print("Password filled")
            await page.wait_for_timeout(800)

            await try_click(page, [
                '#js_btn_login',
                'button:has-text("Sign In")',
                'button[type="submit"]',
                '//*[@id="js_btn_login"]',
            ])
            await page.wait_for_timeout(12000)

            url = page.url.lower()
            print("After login URL:", page.url)
            if "signin" in url or "login" in url:
                print("Still on login page")
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
            print("Login error:", str(e)[:180])
            return None
        finally:
            await browser.close()

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
        print("ZOOM_EMAIL / ZOOM_PASSWORD missing")
        return
    print(f"Keeper running | every {CHECK_EVERY}s")
    last_state = None
    while True:
        try:
            need = True
            if last_state and await is_valid(last_state):
                need = False
                print("Session valid")
            else:
                try:
                    async with httpx.AsyncClient(timeout=15) as c:
                        r = await c.get(f"{RAILWAY}/session")
                        if r.status_code == 200 and await is_valid(r.json()):
                            last_state = r.json()
                            need = False
                            print("Server session valid")
                except Exception:
                    pass
            if need:
                print("Re-login...")
                new_state = await do_login()
                if new_state:
                    if await push_to_server(new_state):
                        last_state = new_state
                        print("Pushed")
                    else:
                        print("Push failed")
                else:
                    print("Login failed")
        except Exception as e:
            print("Loop:", str(e)[:150])
        await asyncio.sleep(CHECK_EVERY)

@app.on_event("startup")
async def startup():
    asyncio.create_task(keeper_loop())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
