"""使用账号手机号和旧密码登录账号中心，再交互式修改密码。"""

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import warnings
from datetime import UTC, datetime

try:
    import requests
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    raise SystemExit("缺少依赖，请运行：python -m pip install requests cryptography") from None


ORIGIN = "https://user.pandadastudio.com"
CHANGE_PATH = "/user_center/change_pwd"


class ScriptError(Exception):
    pass


def load_config(session):
    response = session.get(ORIGIN + "/env.js", timeout=20, allow_redirects=False)
    if response.status_code != 200:
        raise ScriptError(f"无法读取前端配置，HTTP {response.status_code}。")
    config = {}
    for key in ("appId", "baseUrl", "backend", "signSecret", "encryptoSecret"):
        match = re.search(
            rf'^\s*{key}\s*:\s*[\"\x27]([^\"\x27\r\n]+)[\"\x27]',
            response.text,
            re.MULTILINE,
        )
        if not match:
            raise ScriptError(f"前端配置缺少 {key}，需要重新检查页面版本。")
        config[key] = match.group(1)
    if config["backend"] != "user.pandadastudio.com" or config["baseUrl"] != "/api/web":
        raise ScriptError("前端接口配置已变化，需要重新检查。")
    try:
        for key in ("signSecret", "encryptoSecret"):
            if not base64.b64decode(config[key], validate=True):
                raise ValueError("empty key")
    except ValueError:
        raise ScriptError("前端密钥格式已变化，需要重新检查。") from None
    return config


def encrypt_password(password, secret, iv=None):
    # 对齐前端 15f8 模块：Base64 解码后补零/截取至 32 字节。
    key = base64.b64decode(secret, validate=True)[:32].ljust(32, b"\0")
    iv = os.urandom(16) if iv is None else iv
    padder = padding.PKCS7(128).padder()
    plaintext = password.encode("utf-8")
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode("ascii")


def signed_headers(config, method, path, body, *, date=None, nonce=None, traceid=None):
    headers = {
        "x-yh-date": date or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "x-yh-nonce": nonce or base64.b64encode(os.urandom(16)).decode("ascii"),
        "x-yh-traceid": traceid or base64.b64encode(os.urandom(16)).decode("ascii"),
        "x-yh-appid": config["appId"],
        "x-yh-web-platform": "official-website",
    }
    canonical_headers = "\n".join(f"{key}:{headers[key]}" for key in sorted(headers))
    digest = base64.b64encode(hashlib.sha1(body).digest()).decode("ascii")
    canonical = "\n".join(
        (method.upper(), config["backend"], path, "", canonical_headers, digest)
    )
    signature = hmac.new(
        base64.b64decode(config["signSecret"], validate=True),
        canonical.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    headers["Authorization"] = "YH " + base64.b64encode(signature).decode("ascii")
    return headers


def request_api(session, config, method, path, payload=None):
    body = b"" if payload is None else json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    headers = signed_headers(config, method, path, body)
    headers.update({"Origin": ORIGIN, "Referer": ORIGIN + "/"})
    if payload is not None:
        headers["Content-Type"] = "application/json;charset=UTF-8"
    try:
        response = session.request(
            method, ORIGIN + config["baseUrl"] + path,
            data=body if payload is not None else None,
            headers=headers, timeout=30, allow_redirects=False,
        )
    except requests.RequestException:
        if path == CHANGE_PATH:
            raise ScriptError(
                "请求未获得完整响应，改密结果未知。请先在游戏中确认结果，勿立即重复提交。"
            ) from None
        raise ScriptError("登录或会话检查网络失败，请检查连接后重试。") from None
    try:
        result = response.json()
    except ValueError:
        raise ScriptError(
            f"服务器返回非 JSON 响应（HTTP {response.status_code}），请在游戏中确认状态。"
        ) from None
    if not isinstance(result, dict):
        raise ScriptError("服务器响应结构变化，请在游戏中确认状态。")
    code = result.get("code")
    if response.status_code != 200 or type(code) is not int or code != 100:
        safe_code = code if type(code) is int else "未知"
        raise ScriptError(
            f"请求未成功：HTTP {response.status_code}，业务码 {safe_code}。"
            "请检查登录状态、旧密码或在游戏内完成验证；脚本不会自动重试。"
        )
    if response.headers.get("x-yh-captcha-trigger") and path != CHANGE_PATH:
        raise ScriptError("服务端触发人机验证，请通过官方账号中心完成正常验证。")
    return result.get("data")


def hidden_input(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ScriptError("当前环境无法隐藏密码，请在 PowerShell 或终端中运行。") from None


def prompt_new_password(old):
    new = hidden_input("新密码（输入不显示）：")
    confirm = hidden_input("再次输入新密码：")
    length = len(new.encode("utf-16-le")) // 2
    if not 6 <= length <= 16 or any(c.isspace() or c == "\ufeff" for c in new):
        raise ScriptError("新密码须为 6–16 个字符，且不能包含空格或其他空白字符。")
    if new != confirm:
        raise ScriptError("两次新密码不一致。")
    if new == old:
        raise ScriptError("新密码与旧密码相同。")
    return new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="仅登录并检查会话，不修改密码")
    parser.add_argument("--area", default="86", help="手机号国家/地区代码，默认 86")
    args = parser.parse_args()
    try:
        with requests.Session() as session:
            if not re.fullmatch(r"[0-9]{1,4}", args.area):
                raise ScriptError("国家/地区代码格式错误，例如中国大陆为 86。")
            print("正在读取账号中心公开配置……")
            config = load_config(session)
            phone = input("账号手机号（不含国家/地区代码）：").strip()
            if not re.fullmatch(r"[0-9]{6,15}", phone):
                raise ScriptError("请输入有效的账号手机号。")
            old = hidden_input("旧密码（输入不显示）：")
            if not old:
                raise ScriptError("旧密码不能为空。")
            request_api(session, config, "POST", "/login/pwd", {
                "phone": phone,
                "pwd": encrypt_password(old, config["encryptoSecret"]),
                "area": args.area,
            })
            request_api(session, config, "GET", "/user_center/userinfo")
            print("账号登录、会话和请求签名检查通过。")
            if args.check:
                print("检查完成，未提交改密请求。")
                return 0
            print("输入完成后将提交一次改密请求，密码和 Cookie 不写入文件。")
            new = prompt_new_password(old)
            payload = {
                "old_pwd": encrypt_password(old, config["encryptoSecret"]),
                "new_pwd": encrypt_password(new, config["encryptoSecret"]),
            }
            del old, new
            request_api(session, config, "POST", CHANGE_PATH, payload)
            print("服务器返回修改密码成功，请使用新密码重新登录。")
            return 0
    except (ScriptError, requests.RequestException) as error:
        # 不打印 Requests 异常对象，避免泄漏可能附带的请求信息。
        message = str(error) if isinstance(error, ScriptError) else "读取前端配置网络失败。"
        print("错误：" + message, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n操作已中断；若已开始提交，请先在游戏中确认改密结果。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
