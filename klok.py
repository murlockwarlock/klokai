import time
import requests
import random
import json
import asyncio
import uuid
from pathlib import Path
from pyppeteer import connect
from pyppeteer.errors import PageError, TimeoutError as PyppeteerTimeoutError
from colorama import Fore, Style, init
import traceback

# --- Инициализация ---
init(autoreset=True)

# --- Настройки ---
PROFILES_FILE = "profile_ids.txt"
PROMPTS_FILE = "prompts.json"
ADS_POWER_API_URL = "YOUR_API_ADS_POWER"

# --- Настройки KlokApp ---
KLOKAPP_URL = "https://klokapp.ai/"
API_BASE_URL = "https://api1-pp.klokapp.ai"
CAPSOLVER_API_KEY = "YOUR_CAPSOLVER_API_KEY"  # !!! ЗАМЕНИТЕ НА ВАШ КЛЮЧ !!!
KLOKAPP_SITEKEY = "0x4AAAAAABdQypM3HkDQTuaO"

# --- Паузы и повторы ---
RETRY_ATTEMPTS = 3  # Количество повторных попыток при сетевых ошибках
RETRY_DELAY = 10  # Пауза между повторными попытками (в секундах)
PAUSE_BETWEEN_PROMPTS = (15, 30)  
PAUSE_BETWEEN_ACCOUNTS = (30, 60)  


# --- Функции для работы с файлами ---
def load_data(filename):
    path = Path(filename)
    if not path.exists():
        print(Fore.RED + f"Файл {filename} не найден.")
        return []
    with open(path, "r", encoding='utf-8') as f:
        if filename.endswith(".json"):
            try:
                return json.load(f).get("prompts", [])
            except json.JSONDecodeError:
                print(Fore.RED + f"Ошибка чтения JSON из файла {filename}");
                return []
        return [line.strip() for line in f if line.strip()]


# --- Функция для повторных попыток в запросах ---
def requests_retry_wrapper(max_attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY, timeout=30):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs, timeout=timeout)
                except requests.exceptions.RequestException as e:
                    print(
                        Fore.YELLOW + f"Попытка {attempt + 1}/{max_attempts}: Ошибка сети ({e}). Повтор через {delay} сек...")
                    if attempt < max_attempts - 1:
                        time.sleep(delay)
            print(Fore.RED + f"✗ Не удалось выполнить запрос после {max_attempts} попыток.")
            return None

        return wrapper

    return decorator


# --- Модуль решения капчи ---
@requests_retry_wrapper(timeout=45)
def create_captcha_task(payload, timeout):
    return requests.post("https://api.capsolver.com/createTask", json=payload, timeout=timeout)


@requests_retry_wrapper(timeout=45)
def get_captcha_result(payload, timeout):
    return requests.post("https://api.capsolver.com/getTaskResult", json=payload, timeout=timeout)


def solve_turnstile(ua):
    print(Fore.CYAN + "Решаю капчу Turnstile..." + Style.RESET_ALL)
    task_payload = {"clientKey": CAPSOLVER_API_KEY, "task": {
        "type": "AntiTurnstileTask", "websiteURL": KLOKAPP_URL,
        "websiteKey": KLOKAPP_SITEKEY, "userAgent": ua
    }}

    res_json = create_captcha_task(task_payload)
    if not res_json or res_json.json().get("errorId") != 0:
        print(
            Fore.RED + f"✗ CapSolver/create: {res_json.json().get('errorDescription') if res_json else 'Нет ответа'}");
        return None

    task_id = res_json.json().get("taskId")
    result_payload = {"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}

    for _ in range(20):
        time.sleep(5)
        res_json = get_captcha_result(result_payload)
        if not res_json: continue  

        res = res_json.json()
        if res.get("errorId") != 0:
            print(Fore.RED + f"✗ CapSolver/get: {res.get('errorDescription')}");
            return None
        if res.get("status") == "ready":
            print(Fore.GREEN + "✓ Капча решена." + Style.RESET_ALL)
            return res.get("solution", {}).get("token")

    print(Fore.RED + "✗ Не удалось решить капчу за 100 секунд.");
    return None


# --- Функции AdsPower ---
async def start_profile(profile_id):
    url = f"{ADS_POWER_API_URL}/browser/start?user_id={profile_id}&headless=0"
    print(f"{Fore.CYAN}Запуск профиля {profile_id}...{Style.RESET_ALL}")
    try:
        response = requests.get(url, timeout=60).json()
        if response.get("code") == 0:
            ws_endpoint = response["data"]["ws"]["puppeteer"]
            print(f"{Fore.GREEN}✓ Профиль запущен: {ws_endpoint}{Style.RESET_ALL}")
            return ws_endpoint
        print(f"{Fore.RED}✗ Ошибка запуска: {response.get('msg', 'Нет сообщения')}")
    except Exception as e:
        print(f"{Fore.RED}✗ Ошибка соединения с API AdsPower: {e}")
    return None


def stop_profile(profile_id):
    url = f"{ADS_POWER_API_URL}/browser/stop?user_id={profile_id}"
    try:
        requests.get(url, timeout=20)
        print(f"{Fore.GREEN}✓ Профиль остановлен.{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}✗ Ошибка API при остановке: {e}")


async def setup_browser(ws_endpoint):
    try:
        browser = await asyncio.wait_for(connect(browserWSEndpoint=ws_endpoint, ignoreHTTPSErrors=True), timeout=60)
        initial_pages = await browser.pages()
        if initial_pages: await initial_pages[0].close()
        page = await browser.newPage()
        await page.setViewport({'width': 1920, 'height': 1080})
        print(f"{Fore.GREEN}✓ Успешно подключено к браузеру и создана новая страница.{Style.RESET_ALL}")
        return browser, page
    except Exception as e:
        print(f"{Fore.RED}✗ Ошибка подключения к Puppeteer или создания страницы: {e}")
        traceback.print_exc()
        return None, None


# --- Функции API KlokApp ---
async def check_and_login(page):
    print(f"{Fore.CYAN}Переход на {KLOKAPP_URL}...{Style.RESET_ALL}")
    for attempt in range(RETRY_ATTEMPTS):
        try:
            await page.goto(KLOKAPP_URL, {"waitUntil": "domcontentloaded", "timeout": 90000})  
            try:
                await page.waitForSelector('form textarea', {"timeout": 15000})
                print(f"{Fore.GREEN}✓ Уже авторизованы.{Style.RESET_ALL}")
                return True
            except PyppeteerTimeoutError:
                print(f"{Fore.YELLOW}Не авторизованы. Попытка входа...{Style.RESET_ALL}")
                google_button = await page.waitForXPath("/html/body/div[1]/div/div[4]/button", {"timeout": 20000})
                await google_button.click()
                await page.waitForSelector('form textarea', {"timeout": 60000})  
                print(f"{Fore.GREEN}✓ Успешная авторизация.{Style.RESET_ALL}")
                return True
        except (PageError, PyppeteerTimeoutError) as e:
            print(
                f"{Fore.YELLOW}Попытка {attempt + 1}/{RETRY_ATTEMPTS}: Ошибка загрузки или поиска элемента ({e}).{Style.RESET_ALL}")
            if attempt < RETRY_ATTEMPTS - 1:
                print(f"Повтор через {RETRY_DELAY} секунд...")
                await asyncio.sleep(RETRY_DELAY)
            else:
                print(f"{Fore.RED}✗ Не удалось загрузить страницу после {RETRY_ATTEMPTS} попыток.{Style.RESET_ALL}")
                return False
        except Exception as e:
            print(f"{Fore.RED}✗ Непредвиденная ошибка во время входа: {e}")
            traceback.print_exc()
            return False
    return False


async def get_browser_data(page):
    try:
        session_token = await page.evaluate("() => localStorage.getItem('session_token')")
        user_agent = await page.browser.userAgent()
        if not session_token:
            print(f"{Fore.RED}✗ Не найден session_token.{Style.RESET_ALL}");
            return None, None
        return session_token, user_agent
    except Exception as e:
        print(f"{Fore.RED}✗ Ошибка извлечения данных: {e}");
        return None, None


@requests_retry_wrapper(timeout=45)
def get_rate_limit_request(session_token, user_agent, timeout):
    headers = {'User-Agent': user_agent, 'x-session-token': session_token, 'Origin': 'https://klokapp.ai'}
    return requests.get(f"{API_BASE_URL}/v1/rate-limit", headers=headers, timeout=timeout)


def get_rate_limit(session_token, user_agent):
    print(Fore.BLUE + "Обновляю информацию о лимитах..." + Style.RESET_ALL)
    response = get_rate_limit_request(session_token, user_agent)
    if response:
        try:
            res_json = response.json()
            remaining = res_json.get("remaining", 0)
            print(Fore.GREEN + f"✓ Лимит: {remaining} запросов.")
            return remaining
        except json.JSONDecodeError:
            print(Fore.RED + "✗ Не удалось прочитать JSON ответа о лимитах.")
            return None  
    return None  


@requests_retry_wrapper(timeout=120) 
def submit_prompt_request(headers, payload, timeout):
    return requests.post(f"{API_BASE_URL}/v1/chat", headers=headers, json=payload, timeout=timeout)


def submit_prompt_via_requests(session_token, user_agent, prompt):
    print(f"{Fore.CYAN}Отправка промпта: \"{prompt[:50]}...\"{Style.RESET_ALL}")
    turnstile_token = solve_turnstile(user_agent)
    if not turnstile_token: return False

    headers = {
        'User-Agent': user_agent, 'Content-Type': 'application/json', 'Origin': 'https://klokapp.ai',
        'Referer': f'{KLOKAPP_URL}', 'x-session-token': session_token, 'x-turnstile-token': turnstile_token
    }
    payload = {"id": str(uuid.uuid4()), "messages": [{"role": "user", "content": prompt}],
               "model": "llama-3.3-70b-instruct"}

    response = submit_prompt_request(headers, payload)
    if response and response.status_code == 200:
        print(Fore.GREEN + "✓ Ответ от чата получен.")
        return True
    elif response:
        print(f"{Fore.RED}✗ Ошибка ответа от чата: {response.status_code} {response.text}")
    return False


# --- Основной процесс ---
async def process_profile(profile_id, prompts_list):
    print(f"{Fore.MAGENTA}{'=' * 15} Начинаем работу с профилем {profile_id} {'=' * 15}{Style.RESET_ALL}")
    browser, page = None, None
    try:
        ws_endpoint = await start_profile(profile_id)
        if not ws_endpoint: return

        browser, page = await setup_browser(ws_endpoint)
        if not (browser and page): return

        if not await check_and_login(page): return

        await asyncio.sleep(5)
        session_token, user_agent = await get_browser_data(page)
        if not session_token: return

        prompts_sent_this_session = 0
        while True:
            
            current_prompts = get_rate_limit(session_token, user_agent)

            if current_prompts is None:
                print(Fore.YELLOW + f"Не удалось получить лимиты. Ждем {RETRY_DELAY * 2} секунд и пробуем снова...")
                await asyncio.sleep(RETRY_DELAY * 2)
                continue

            if current_prompts <= 0:
                print(Fore.YELLOW + "Лимит промптов исчерпан.")
                break

            if prompts_sent_this_session >= len(prompts_list):
                print(Fore.YELLOW + "Все промпты из файла были использованы в этой сессии.")
                break

            
            print(Fore.BLUE + "Обновляю страницу в браузере для имитации активности...")
            await page.reload({"waitUntil": "domcontentloaded"})
            

            prompt_to_send = prompts_list[prompts_sent_this_session]

            if submit_prompt_via_requests(session_token, user_agent, prompt_to_send):
                prompts_sent_this_session += 1
            else:
                print(f"{Fore.RED}Не удалось отправить промпт после нескольких попыток. Пропускаем.{Style.RESET_ALL}")

            pause_duration = random.randint(*PAUSE_BETWEEN_PROMPTS)
            print(f"Пауза {pause_duration} секунд...")
            await asyncio.sleep(pause_duration)

        print(
            f"{Fore.GREEN}✓ Работа с профилем {profile_id} завершена. Отправлено: {prompts_sent_this_session}.{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}✗ Критическая ошибка в process_profile: {e}");
        traceback.print_exc()
    finally:
        if browser: await browser.close()
        stop_profile(profile_id)
        print(f"{Fore.MAGENTA}{'=' * 15} Завершили работу с профилем {profile_id} {'=' * 15}{Style.RESET_ALL}\n")

async def main():
    print(f"{Fore.BLUE}Скрипт запущен...{Style.RESET_ALL}")
    profile_ids = load_data("profile_ids.txt")
    prompts_list = load_data("prompts.json")
    if not profile_ids or not prompts_list:
        print(f"{Fore.RED}✗ Профили или промпты не загружены.");
        return

    for i, profile_id in enumerate(profile_ids):
        await process_profile(profile_id, prompts_list)
        if i < len(profile_ids) - 1:
            pause = random.randint(*PAUSE_BETWEEN_ACCOUNTS)
            print(f"{Fore.CYAN}Пауза {pause} секунд перед следующим профилем...{Style.RESET_ALL}")
            await asyncio.sleep(pause)

    print(f"{Fore.BLUE}{'=' * 20} Все профили обработаны. Скрипт завершен. {'=' * 20}{Style.RESET_ALL}")


if __name__ == "__main__":
    if "ЗАМЕНИТЕ НА ВАШ КЛЮЧ" in CAPSOLVER_API_KEY:
        print(f"{Fore.RED}!!! ВНИМАНИЕ: Укажите ваш API ключ в CAPSOLVER_API_KEY !!!{Style.RESET_ALL}")
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}Программа прервана пользователем.{Style.RESET_ALL}")
