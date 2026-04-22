"""
Basic Auth 비밀번호 해시 생성 스크립트.

사용법:
    python scripts/gen_password_hash.py

사용자명과 비밀번호를 입력받아 .env 파일에 붙여넣을 수 있는
AUTH_USERNAME 및 AUTH_PASSWORD_HASH 두 줄을 출력한다.
해시에 '$' 문자가 포함되므로 반드시 작은따옴표로 감싸야 한다.
"""
import getpass
import sys

from werkzeug.security import generate_password_hash


def main():
    print("ezadmin Basic Auth 자격증명 생성")
    print("-" * 40)
    username = input("Username: ").strip()
    if not username:
        print("사용자명이 비어있습니다.", file=sys.stderr)
        return 1

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("비밀번호가 일치하지 않습니다.", file=sys.stderr)
        return 1
    if not password:
        print("비밀번호가 비어있습니다.", file=sys.stderr)
        return 1

    hashed = generate_password_hash(password)

    print()
    print("아래 두 줄을 .env 파일에 추가하세요.")
    print("해시에 '$' 문자가 들어 있으므로 반드시 작은따옴표로 감싸세요.")
    print()
    print(f"AUTH_USERNAME={username}")
    print(f"AUTH_PASSWORD_HASH='{hashed}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
