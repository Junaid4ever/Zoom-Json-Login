import os, asyncio, httpx, json, time
from playwright.async_api import async_playwright
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
import uvicorn

RAILWAY = os.environ.get("CENTRAL_SERVER", "https://zoom-server-new.up.railway.app").rstrip("/")
EMAIL = os.environ.get("ZOOM_EMAIL", "").strip()
PASSWORD = os.environ.get("ZOOM_PASSWORD", "").strip()
CHECK_EVERY = int(os.environ.get("CHECK_INTERVAL", "30"))  # seconds between checks
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
            await page.wait_for_timeout(2500)

            # Email
            email_field = page.locator('//*[@id="email"]')
            await email_field.click()
            await email_field.fill("")
            for ch in EMAIL:
                await email_field.type(ch, delay=60)
            await page.wait_for_timeout(600)

            # Next
            await page.locator('//*[@id="signin_btn_next"]/span').click()
            await page.wait_for_timeout(3000)

            # Password
            pwd_field = page.locator('//*[@id="password"]')
            await pwd_field.click()
            await pwd_field.fill("")
            for ch in PASSWORD:
                await pwd_field.type(ch, delay=60)
            await page.wait_for_timeout(500)

            # Login
            await page.locator('//*[@id="js_btn_login"]').click()
            await page.wait_for_timeout(8000)

            url = page.url.lower()
            if "signin" in url or "login" in url:
                print("Login may have failed. URL:", page.url)
                return None

            # Save storage state
            state = await context.storage_state()
            # Extend short-lived cookies
            now = time.time()
            for c in state.get("cookies", []):
                exp = c.get("expires", -1)
                if exp == -1 or exp < now + 72 * 3600:
                    c["expires"] = now + 72 * 3600
            return state
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
                # Try fetching current from server
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
            print("Loop error:", str(e)[:180])
        await asyncio.sleep(CHECK_EVERY)

@app.on_event("startup")
async def startup():
    asyncio.create_task(keeper_loop())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
