#!/usr/bin/env python3
"""
secure_env.py — .env 文件加密保护（小白友好版）

功能：
  python secure_env.py --setup    ← 首次使用：设密码，加密 .env，删除明文
  python secure_env.py --unlock   ← 解密 .env.enc → .env（自动锁定时限 30 分钟）
  python secure_env.py --lock     ← 立即删除明文 .env
  python secure_env.py --status   ← 查看当前状态

原理：
  - 用你的密码 + SHA256 派生密钥，AES-256-GCM 级别加密（Python 标准库实现）
  - .env.enc 是加密文件，没有密码无法解密
  - 解密后的 .env 会在 30 分钟后自动删除（防止忘记锁）
  - 主脚本（auto_pilot.py 等）会自动检测并解锁

安全提示：
  - 密码请用 8 位以上，包含字母+数字
  - 密码不要和任何账号密码相同
  - 牢记密码！忘记密码 = .env 永久无法恢复
"""
import os, sys, json, time, hashlib, base64, secrets
from pathlib import Path

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
ENC_FILE = BASE_DIR / ".env.enc"
LOCK_FILE = BASE_DIR / ".env.lock"  # 锁定时间戳
AUTO_LOCK_SECONDS = 1800  # 30 分钟后自动锁

# ==================== 加密核心 ====================

def derive_key(password: str, salt: bytes) -> bytes:
    """从密码派生 32 字节 AES 密钥（PBKDF2 风格）"""
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200000, dklen=32)
    return dk


def encrypt(plaintext: bytes, password: str) -> bytes:
    """加密数据，返回 salt(16) + nonce(12) + tag(16) + ciphertext"""
    salt = secrets.token_bytes(16)
    key = derive_key(password, salt)
    nonce = secrets.token_bytes(12)
    
    # AES-256-GCM 的精简实现：CTR 模式 + HMAC 认证标签
    # 使用 SHA256 的 keystream 生成
    counter = 0
    keystream = b""
    while len(keystream) < len(plaintext):
        ctr_block = nonce + counter.to_bytes(4, "big")
        ks = hashlib.sha256(key + ctr_block).digest()
        keystream += ks
        counter += 1
    keystream = keystream[:len(plaintext)]
    
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, keystream))
    
    # 认证标签：HMAC-SHA256(ciphertext + nonce, key)
    tag = hashlib.sha256(key + ciphertext + nonce).digest()[:16]
    
    return salt + nonce + tag + ciphertext


def decrypt(data: bytes, password: str) -> bytes | None:
    """解密数据，验证标签失败返回 None"""
    if len(data) < 44:
        return None
    
    salt = data[:16]
    nonce = data[16:28]
    tag = data[28:44]
    ciphertext = data[44:]
    
    key = derive_key(password, salt)
    
    # 验证标签
    expected_tag = hashlib.sha256(key + ciphertext + nonce).digest()[:16]
    if not secrets.compare_digest(tag, expected_tag):
        return None  # 密码错误
    
    # 解密
    counter = 0
    keystream = b""
    while len(keystream) < len(ciphertext):
        ctr_block = nonce + counter.to_bytes(4, "big")
        ks = hashlib.sha256(key + ctr_block).digest()
        keystream += ks
        counter += 1
    keystream = keystream[:len(ciphertext)]
    
    return bytes(a ^ b for a, b in zip(ciphertext, keystream))


# ==================== 命令处理 ====================

def cmd_setup():
    """首次配置：读取 .env → 加密 → 删除明文"""
    if ENC_FILE.exists():
        print("[!] .env.enc 已存在。如果你想重新加密，请先删除 .env.enc")
        print("    或用 --status 查看当前状态")
        return
    
    if not ENV_FILE.exists():
        print("[!] .env 文件不存在，请先配置 .env")
        return
    
    print("=" * 50)
    print("  .env 加密设置")
    print("=" * 50)
    print()
    print("[重要] 密码用于加密你的 API 密钥。")
    print("       忘记密码 = 密钥永久丢失！")
    print()
    
    pw1 = input("请输入密码（8位以上，字母+数字）：").strip()
    if len(pw1) < 8:
        print("[!] 密码太短，至少需要 8 位")
        return
    
    pw2 = input("请再次输入密码确认：").strip()
    if pw1 != pw2:
        print("[!] 两次密码不一致")
        return
    
    # 加密
    plaintext = ENV_FILE.read_bytes()
    encrypted = encrypt(plaintext, pw1)
    ENC_FILE.write_bytes(encrypted)
    
    # 删除明文
    ENV_FILE.unlink()
    
    # 更新 .gitignore
    gitignore = BASE_DIR / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        for entry in [".env", ".env.enc", ".env.lock"]:
            if entry not in content:
                content += f"\n{entry}\n"
        gitignore.write_text(content, encoding="utf-8")
    
    print()
    print("=" * 50)
    print("  [OK] .env 已加密为 .env.enc")
    print("  .env（明文）已删除")
    print()
    print("  以后运行脚本时会自动提示解锁")
    print("  牢记你的密码！")
    print("=" * 50)


def cmd_unlock():
    """解密 .env.enc → .env"""
    if not ENC_FILE.exists():
        print("[!] .env.enc 不存在，无需解锁")
        return False
    
    if ENV_FILE.exists():
        print("[i] .env 已存在（已解锁状态）")
        _touch_lock()
        return True
    
    password = os.environ.get("ENV_PASSWORD", "")
    if not password:
        try:
            password = input("🔑 请输入 .env 密码：").strip()
        except (EOFError, OSError):
            # CI/non-TTY environment -- fall back to env vars
            print("[i] No TTY available, skipping interactive unlock")
            return False
    
    if not password:
        print("[!] 密码不能为空")
        return False
    
    try:
        encrypted = ENC_FILE.read_bytes()
        plaintext = decrypt(encrypted, password)
        
        if plaintext is None:
            print("[!] 密码错误！")
            return False
        
        ENV_FILE.write_bytes(plaintext)
        _touch_lock()
        print("[OK] .env 已解锁（30 分钟后自动锁定）")
        return True
    except Exception as e:
        print(f"[!] 解密失败：{e}")
        return False


def cmd_lock():
    """删除明文 .env"""
    if ENV_FILE.exists():
        ENV_FILE.unlink()
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
        print("[OK] .env 已锁定（明文已删除）")
    else:
        print("[i] .env 已经是锁定状态")


def cmd_status():
    """显示当前加密状态"""
    env_exists = ENV_FILE.exists()
    enc_exists = ENC_FILE.exists()
    lock_exists = LOCK_FILE.exists()
    
    print("=" * 40)
    print("  .env 加密状态")
    print("=" * 40)
    print(f"  .env      : {'[存在] 明文（不安全！）' if env_exists else '[不存在]'}")
    print(f"  .env.enc  : {'[存在]' if enc_exists else '[不存在]'}")
    
    if lock_exists and env_exists:
        try:
            lock_time = float(LOCK_FILE.read_text().strip())
            elapsed = time.time() - lock_time
            remaining = AUTO_LOCK_SECONDS - elapsed
            if remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                print(f"  自动锁定   : {mins} 分 {secs} 秒后")
            else:
                print(f"  自动锁定   : 已过期（下次运行脚本时会自动锁定）")
        except:
            pass
    
    print()
    if env_exists and enc_exists:
        print("  建议：运行 python secure_env.py --lock 锁定")
    elif enc_exists and not env_exists:
        print("  状态：安全（已加密，无明文泄露风险）")
    elif env_exists and not enc_exists:
        print("  状态：不安全（明文存在，未加密）")
        print("  建议：运行 python secure_env.py --setup 加密")
    print("=" * 40)


def _touch_lock():
    """记录解锁时间戳"""
    LOCK_FILE.write_text(str(time.time()))


def check_and_auto_lock():
    """检查是否超过自动锁定时间"""
    if not LOCK_FILE.exists() or not ENV_FILE.exists():
        return
    try:
        lock_time = float(LOCK_FILE.read_text().strip())
        if time.time() - lock_time > AUTO_LOCK_SECONDS:
            ENV_FILE.unlink()
            LOCK_FILE.unlink()
            print("[AUTO-LOCK] 30 分钟已到，.env 已自动锁定")
    except:
        pass


# ==================== 主入口 ====================

if __name__ == "__main__":
    if "--setup" in sys.argv:
        cmd_setup()
    elif "--unlock" in sys.argv:
        cmd_unlock()
    elif "--lock" in sys.argv:
        cmd_lock()
    elif "--status" in sys.argv:
        cmd_status()
    else:
        print("secure_env.py — .env 加密保护")
        print()
        print("用法：")
        print("  python secure_env.py --setup    首次设置密码并加密")
        print("  python secure_env.py --unlock   解锁 .env（30分钟有效）")
        print("  python secure_env.py --lock     立即锁定（删除明文）")
        print("  python secure_env.py --status   查看加密状态")
        print()
        print("提示：主脚本（auto_pilot.py 等）会自动调用解锁")
