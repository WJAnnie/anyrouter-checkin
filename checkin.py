#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.notify import notify
from web.browser_checkin import browser_login_checkin

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'
MAX_CHECKIN_RETRIES = 3
INITIAL_RETRY_DELAY_SECONDS = 1.0


def load_balance_hash():
	"""加载余额hash"""
	try:
		if os.path.exists(BALANCE_HASH_FILE):
			with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
				return f.read().strip()
	except Exception:
		pass
	return None


def save_balance_hash(balance_hash):
	"""保存余额hash"""
	try:
		with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
			f.write(balance_hash)
	except Exception as e:
		print(f'Warning: Failed to save balance hash: {e}')


def generate_balance_hash(balances):
	"""生成余额数据的hash"""
	# 将包含 quota 和 used 的结构转换为简单的 quota 值用于 hash 计算
	simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def parse_cookies(cookies_data):
	"""解析 cookies 数据"""
	if isinstance(cookies_data, dict):
		return cookies_data

	if isinstance(cookies_data, str):
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			if '=' in cookie:
				key, value = cookie.strip().split('=', 1)
				cookies_dict[key] = value
		return cookies_dict
	return {}


def is_already_checked_in_message(message: str | None) -> bool:
	"""判断消息是否表示“已签到”"""
	if not message:
		return False

	text = str(message).strip().lower()
	keywords = [
		'already checked in',
		'already check in',
		'already signed in',
		'already_checked_in',
		'已经签到',
		'已签到',
		'重复签到',
	]
	return any(keyword in text for keyword in keywords)


def is_cloudflare_h2_challenge(response) -> bool:
	"""检测 Cloudflare 针对 HTTP/2 的 challenge（403 + cf-mitigated header）"""
	if response.status_code != 403:
		return False
	cf_mitigated = response.headers.get('cf-mitigated', '').lower()
	if 'challenge' in cf_mitigated:
		return True
	# 部分 Cloudflare 挑战不带 cf-mitigated，检查响应体
	content_type = response.headers.get('content-type', '').lower()
	if 'text/html' in content_type:
		text = response.text or ''
		if 'cf-chl' in text.lower() or 'just a moment' in text.lower():
			return True
	return False


def is_waf_challenge_response(response_text: str) -> bool:
	"""检测 HTTP 响应是否为 WAF 拦截页面（非正常 API 响应）。

	检测阿里云 WAF、Cloudflare 等常见 WAF 挑战标志。
	"""
	if not response_text:
		return False

	text_lower = response_text.lower()
	markers = [
		'acw_sc__v2',
		'<script>var arg1=',
		'just a moment',
		'checking your browser',
	]

	# HTML 响应体且包含 WAF 标记
	if response_text.strip().startswith('<') and 'acw_sc' in text_lower:
		return True

	return any(marker in text_lower for marker in markers)


async def get_waf_cookies_with_playwright(account_name: str, login_url: str, required_cookies: list[str]):
	"""使用 Playwright 获取 WAF cookies（隐私模式）"""
	print(f'[PROCESSING] {account_name}: Starting browser to get WAF cookies...')

	async with async_playwright() as p:
		import tempfile

		with tempfile.TemporaryDirectory() as temp_dir:
			context = await p.chromium.launch_persistent_context(
				user_data_dir=temp_dir,
				headless=True,
				user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
				viewport={'width': 1920, 'height': 1080},
				args=[
					'--disable-blink-features=AutomationControlled',
					'--disable-dev-shm-usage',
					'--disable-web-security',
					'--disable-features=VizDisplayCompositor',
					'--no-sandbox',
				],
			)

			page = await context.new_page()

			try:
				print(f'[PROCESSING] {account_name}: Access login page to get initial cookies...')

				await page.goto(login_url, wait_until='networkidle')

				try:
					await page.wait_for_function('document.readyState === "complete"', timeout=5000)
				except Exception:
					await page.wait_for_timeout(3000)

				cookies = await page.context.cookies()

				waf_cookies = {}
				for cookie in cookies:
					cookie_name = cookie.get('name')
					cookie_value = cookie.get('value')
					if cookie_name in required_cookies and cookie_value is not None:
						waf_cookies[cookie_name] = cookie_value

				print(f'[INFO] {account_name}: Got {len(waf_cookies)} WAF cookies')

				missing_cookies = [c for c in required_cookies if c not in waf_cookies]

				if missing_cookies:
					print(f'[FAILED] {account_name}: Missing WAF cookies: {missing_cookies}')
					await context.close()
					return None

				print(f'[SUCCESS] {account_name}: Successfully got all WAF cookies')

				await context.close()

				return waf_cookies

			except Exception as e:
				print(f'[FAILED] {account_name}: Error occurred while getting WAF cookies: {e}')
				await context.close()
				return None


def get_user_info(client, headers, user_info_url: str):
	"""获取用户信息"""
	try:
		response = client.get(user_info_url, headers=headers, timeout=30)

		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / 500000, 2)
				used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
				return {
					'success': True,
					'quota': quota,
					'used_quota': used_quota,
					'display': f':money: Current balance: ${quota}, Used: ${used_quota}',
				}
		return {'success': False, 'error': f'Failed to get user info: HTTP {response.status_code}'}
	except Exception as e:
		return {'success': False, 'error': f'Failed to get user info: {str(e)[:50]}...'}


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
	"""准备请求所需的 cookies（可能包含 WAF cookies）"""
	waf_cookies = {}

	if provider_config.needs_waf_cookies():
		login_url = f'{provider_config.domain}{provider_config.login_path}'
		waf_cookies = await get_waf_cookies_with_playwright(account_name, login_url, provider_config.waf_cookie_names)
		if not waf_cookies:
			print(f'[FAILED] {account_name}: Unable to get WAF cookies')
			return None
	else:
		print(f'[INFO] {account_name}: Bypass WAF not required, using user cookies directly')

	return {**waf_cookies, **user_cookies}


def execute_check_in(client, account_name: str, provider_config, headers: dict) -> dict:
	"""执行签到请求（带重试和指数退避）。

	可重试：网络异常、HTTP 5xx、HTTP 429。
	不重试：HTTP 200 带 API 级别失败、HTTP 4xx（429 除外）。
	WAF 挑战 / Cloudflare HTTP/2 挑战直接返回，由 scheduler 处理。
	"""
	import time

	last_result = None

	for attempt in range(MAX_CHECKIN_RETRIES):
		if attempt > 0:
			delay = INITIAL_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
			print(f'[RETRY] {account_name}: Attempt {attempt + 1}/{MAX_CHECKIN_RETRIES}, '
				  f'waiting {delay:.0f}s...')
			time.sleep(delay)

		try:
			result = _execute_check_in_once(client, account_name, provider_config, headers)

			# WAF / CF H2 挑战直接返回，由上层处理
			if result.get('_waf_challenge') or result.get('_cf_h2_challenge'):
				print(f'[WAF] {account_name}: WAF challenge detected in response')
				return result

			# 5xx/429 可重试
			if result.get('_retryable'):
				last_result = result
				continue

			return result

		except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError,
				httpx.WriteError, httpx.PoolTimeout, httpx.ConnectTimeout) as e:
			print(f'[RETRY] {account_name}: Network error on attempt {attempt + 1}: {e}')
			last_result = {'success': False, 'status': 'failed',
						   'message': f'Network error: {str(e)[:100]}'}
		except Exception as e:
			print(f'[FAILED] {account_name}: Unexpected error: {e}')
			return {'success': False, 'status': 'failed',
					'message': f'Unexpected error: {str(e)[:100]}'}

	# 所有重试用尽
	if last_result:
		last_result.pop('_retryable', None)
		last_result['message'] = f'{last_result["message"]} (after {MAX_CHECKIN_RETRIES} attempts)'
		return last_result

	return {'success': False, 'status': 'failed',
			'message': f'Check-in failed after {MAX_CHECKIN_RETRIES} attempts'}


def _execute_check_in_once(client, account_name: str, provider_config, headers: dict) -> dict:
	"""执行单次签到请求（无重试）"""
	print(f'[NETWORK] {account_name}: Executing check-in')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	response = client.post(sign_in_url, headers=checkin_headers, timeout=30)

	print(f'[RESPONSE] {account_name}: Response status code {response.status_code}')

	if response.status_code != 200:
		response_text = response.text or ''
		# 检测 Cloudflare HTTP/2 挑战（403 + cf-mitigated: challenge）
		if is_cloudflare_h2_challenge(response):
			print(f'[CF-H2] {account_name}: Cloudflare HTTP/2 challenge detected (403)')
			return {'success': False, 'status': 'failed',
					'message': f'Cloudflare HTTP/2 challenge (HTTP 403)',
					'_cf_h2_challenge': True}
		# 检测 WAF 挑战
		if is_waf_challenge_response(response_text):
			return {'success': False, 'status': 'failed',
					'message': f'WAF challenge detected (HTTP {response.status_code})',
					'_waf_challenge': True}
		error_msg = f'Check-in failed - HTTP {response.status_code}'
		print(f'[FAILED] {account_name}: {error_msg}')
		# 5xx/429 标记为可重试
		if response.status_code >= 500 or response.status_code == 429:
			return {'success': False, 'status': 'failed', 'message': error_msg, '_retryable': True}
		return {'success': False, 'status': 'failed', 'message': error_msg}

	# 检测 200 响应中的 WAF 拦截（HTML 而非 JSON）
	response_text = response.text or ''
	if is_waf_challenge_response(response_text):
		print(f'[WAF] {account_name}: WAF challenge detected in 200 response body')
		return {'success': False, 'status': 'failed',
				'message': 'WAF challenge detected in response body',
				'_waf_challenge': True}

	try:
		result = response.json()
		raw_msg = result.get('msg') or result.get('message') or result.get('error') or ''

		if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
			print(f'[SUCCESS] {account_name}: Check-in successful!')
			return {
				'success': True,
				'status': 'success',
				'message': raw_msg or 'Check-in successful',
			}

		if is_already_checked_in_message(raw_msg):
			print(f'[INFO] {account_name}: Already checked in - {raw_msg}')
			return {
				'success': True,
				'status': 'already_checked_in',
				'message': raw_msg,
			}

		error_msg = raw_msg or 'Unknown error'
		print(f'[FAILED] {account_name}: Check-in failed - {error_msg}')
		return {'success': False, 'status': 'failed', 'message': error_msg}
	except json.JSONDecodeError:
		if is_already_checked_in_message(response_text):
			print(f'[INFO] {account_name}: Already checked in')
			return {
				'success': True,
				'status': 'already_checked_in',
				'message': response_text[:200],
			}
		if 'success' in response_text.lower():
			print(f'[SUCCESS] {account_name}: Check-in successful!')
			return {
				'success': True,
				'status': 'success',
				'message': 'Check-in successful',
			}

		error_msg = 'Check-in failed - Invalid response format'
		print(f'[FAILED] {account_name}: {error_msg}')
		return {'success': False, 'status': 'failed', 'message': error_msg}


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
	"""为单个账号执行签到操作"""
	account_name = account.get_display_name(account_index)
	print(f'\n[PROCESSING] Starting to process {account_name}')

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		print(f'[FAILED] {account_name}: Provider "{account.provider}" not found in configuration')
		return False, None

	print(f'[INFO] {account_name}: Using provider "{account.provider}" ({provider_config.domain})')

	# 浏览器登录模式
	if account.auth_method == 'browser_login':
		if not account.username or not account.password:
			print(f'[FAILED] {account_name}: Browser login requires username and password')
			return False, None

		print(f'[INFO] {account_name}: Using browser login mode')
		result = await browser_login_checkin(
			account_name=account_name,
			domain=provider_config.domain,
			login_path=provider_config.login_path,
			username=account.username,
			password=account.password,
			user_info_path=provider_config.user_info_path,
			sign_in_path=provider_config.sign_in_path,
		)

		if result['success']:
			print(f'[SUCCESS] {account_name}: {result["message"]}')
			user_info = {
				'success': True,
				'quota': result.get('quota'),
				'used_quota': result.get('used_quota'),
				'display': f':money: Current balance: ${result.get("quota")}, Used: ${result.get("used_quota")}',
				'checkin_status': 'success',
				'checkin_message': result['message'],
			}
			return True, user_info
		else:
			print(f'[FAILED] {account_name}: {result["message"]}')
			return False, {
				'success': False,
				'error': result['message'],
				'checkin_status': 'failed',
				'checkin_message': result['message'],
			}

	# Cookie 模式（原有逻辑）

	user_cookies = parse_cookies(account.cookies)
	if not user_cookies:
		print(f'[FAILED] {account_name}: Invalid configuration format')
		return False, None

	all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
	if not all_cookies:
		return False, None

	headers = {
		'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
		'Accept': 'application/json, text/plain, */*',
		'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
		'Accept-Encoding': 'gzip, deflate, br, zstd',
		'Referer': provider_config.domain,
		'Origin': provider_config.domain,
		'Connection': 'keep-alive',
		'Sec-Fetch-Dest': 'empty',
		'Sec-Fetch-Mode': 'cors',
		'Sec-Fetch-Site': 'same-origin',
		provider_config.api_user_key: account.api_user,
	}

	user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'

	# 先用 HTTP/2 尝试，遇到 Cloudflare H2 challenge 自动回退 HTTP/1.1
	for use_h2 in [True, False]:
		client = httpx.Client(http2=use_h2, timeout=30.0)
		try:
			client.cookies.update(all_cookies)

			if provider_config.needs_manual_check_in():
				check_in_result = execute_check_in(client, account_name, provider_config, headers)

				# Cloudflare HTTP/2 挑战 → 回退 HTTP/1.1 重试
				if check_in_result.get('_cf_h2_challenge') and use_h2:
					print(f'[CF-H2] {account_name}: Falling back to HTTP/1.1')
					continue

				# Fetch balance AFTER check-in so we get the updated value
				user_info = get_user_info(client, headers, user_info_url)
				if user_info and user_info.get('success'):
					print(user_info['display'])
				elif user_info:
					print(user_info.get('error', 'Unknown error'))

				user_info_dict = user_info.copy() if isinstance(user_info, dict) else {}
				user_info_dict['checkin_status'] = check_in_result['status']
				user_info_dict['checkin_message'] = check_in_result['message']

				# 传递内部标记供 scheduler 识别
				if check_in_result.get('_waf_challenge'):
					user_info_dict['_waf_challenge'] = True

				if check_in_result['status'] == 'failed':
					user_info_dict['success'] = False
					user_info_dict['error'] = check_in_result['message']

				if not use_h2:
					user_info_dict['_used_h1'] = True

				return check_in_result['success'], user_info_dict
			else:
				# No explicit check-in needed; fetching user info triggers auto check-in
				user_info = get_user_info(client, headers, user_info_url)
				if user_info and user_info.get('success'):
					print(user_info['display'])
				elif user_info:
					print(user_info.get('error', 'Unknown error'))
				print(f'[INFO] {account_name}: Check-in completed automatically (triggered by user info request)')
				return True, user_info

		except Exception as e:
			if use_h2:
				print(f'[WARN] {account_name}: HTTP/2 error: {str(e)[:50]}, trying HTTP/1.1...')
				continue
			print(f'[FAILED] {account_name}: Error occurred during check-in process - {str(e)[:50]}...')
			return False, None
		finally:
			client.close()

	# 两次都失败（不应该到这里，但防御性处理）
	return False, None


async def main():
	"""主函数"""
	print('[SYSTEM] AnyRouter.top multi-account auto check-in script started (using Playwright)')
	print(f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

	app_config = AppConfig.load_from_env()
	print(f'[INFO] Loaded {len(app_config.providers)} provider configuration(s)')

	accounts = load_accounts_config()
	if not accounts:
		print('[FAILED] Unable to load account configuration, program exits')
		sys.exit(1)

	print(f'[INFO] Found {len(accounts)} account configurations')

	last_balance_hash = load_balance_hash()

	success_count = 0
	failed_count = 0
	total_count = len(accounts)
	notification_content = []
	current_balances = {}
	need_notify = False  # 是否需要发送通知
	balance_changed = False  # 余额是否有变化

	for i, account in enumerate(accounts):
		account_key = f'account_{i + 1}'
		try:
			success, user_info = await check_in_account(account, i, app_config)
			checkin_status = user_info.get('checkin_status') if user_info else None
			if checkin_status in {'success', 'already_checked_in'}:
				success_count += 1
			elif checkin_status == 'failed':
				failed_count += 1
			elif success:
				success_count += 1
			else:
				failed_count += 1

			should_notify_this_account = False

			if checkin_status == 'failed' or (checkin_status is None and not success):
				should_notify_this_account = True
				need_notify = True
				account_name = account.get_display_name(i)
				print(f'[NOTIFY] {account_name} failed, will send notification')

			if user_info and user_info.get('success'):
				current_quota = user_info['quota']
				current_used = user_info['used_quota']
				current_balances[account_key] = {'quota': current_quota, 'used': current_used}

			if should_notify_this_account:
				account_name = account.get_display_name(i)
				status = '[SUCCESS]' if success else '[FAIL]'
				account_result = f'{status} {account_name}'
				if user_info and user_info.get('success'):
					account_result += f'\n{user_info["display"]}'
				elif user_info:
					account_result += f'\n{user_info.get("error", "Unknown error")}'
				notification_content.append(account_result)

		except Exception as e:
			account_name = account.get_display_name(i)
			print(f'[FAILED] {account_name} processing exception: {e}')
			failed_count += 1
			need_notify = True  # 异常也需要通知
			notification_content.append(f'[FAIL] {account_name} exception: {str(e)[:50]}...')

	# 检查余额变化
	current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
	if current_balance_hash:
		if last_balance_hash is None:
			# 首次运行
			balance_changed = True
			need_notify = True
			print('[NOTIFY] First run detected, will send notification with current balances')
		elif current_balance_hash != last_balance_hash:
			# 余额有变化
			balance_changed = True
			need_notify = True
			print('[NOTIFY] Balance changes detected, will send notification')
		else:
			print('[INFO] No balance changes detected')

	# 为有余额变化的情况添加所有成功账号到通知内容
	if balance_changed:
		for i, account in enumerate(accounts):
			account_key = f'account_{i + 1}'
			if account_key in current_balances:
				account_name = account.get_display_name(i)
				# 只添加成功获取余额的账号，且避免重复添加
				account_result = f'[BALANCE] {account_name}'
				account_result += f'\n:money: Current balance: ${current_balances[account_key]["quota"]}, Used: ${current_balances[account_key]["used"]}'
				# 检查是否已经在通知内容中（避免重复）
				if not any(account_name in item for item in notification_content):
					notification_content.append(account_result)

	# 保存当前余额hash
	if current_balance_hash:
		save_balance_hash(current_balance_hash)

	if need_notify and notification_content:
		# 构建通知内容
		summary = [
			'[STATS] Check-in result statistics:',
			f'[SUCCESS] Success: {success_count}/{total_count}',
			f'[FAIL] Failed: {failed_count}/{total_count}',
		]

		if failed_count == 0 and success_count == total_count:
			summary.append('[SUCCESS] All accounts check-in successful!')
		elif failed_count == 0:
			summary.append('[SUCCESS] All accounts completed (including already checked in)')
		elif success_count > 0:
			summary.append('[WARN] Some accounts completed, some failed')
		else:
			summary.append('[ERROR] All accounts check-in failed')

		time_info = f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

		notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])

		print(notify_content)
		notify.push_message('AnyRouter Check-in Alert', notify_content, msg_type='text')
		print('[NOTIFY] Notification sent due to failures or balance changes')
	else:
		print('[INFO] All accounts successful and no balance changes detected, notification skipped')

	# 设置退出码
	sys.exit(0 if failed_count == 0 and total_count > 0 else 1)


def run_main():
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print('\n[WARNING] Program interrupted by user')
		sys.exit(1)
	except Exception as e:
		print(f'\n[FAILED] Error occurred during program execution: {e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
