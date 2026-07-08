import secrets
import sys
from pathlib import Path


def load_secret_key(env_path=Path(".env")):
    env_path = Path(env_path)
    if not env_path.exists():
        key = secrets.token_hex(32)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(f"SECRET_KEY={key}\n")

    for line in env_path.read_text().splitlines():
        if line.startswith("SECRET_KEY="):
            return line.split("=", 1)[1].strip()

    raise RuntimeError(f"{env_path} exists but has no SECRET_KEY line")


def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"

        # 1. first call creates the file and returns a 64-hex-char key
        key = load_secret_key(env_path)
        assert env_path.exists(), "1: load_secret_key did not create the env file"
        assert len(key) == 64, f"1: expected a 64-hex-char key, got length {len(key)}"
        int(key, 16)  # raises ValueError if not hex

        # 2. second call returns the identical key - stable across restarts
        key_again = load_secret_key(env_path)
        assert key_again == key, "2: second call returned a different key"

        # 3. a file with no SECRET_KEY line raises RuntimeError
        bad_path = Path(tmp) / "bad.env"
        bad_path.write_text("SOME_OTHER_VAR=1\n")
        try:
            load_secret_key(bad_path)
            raise AssertionError("3: expected RuntimeError for missing SECRET_KEY line")
        except RuntimeError:
            pass

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return

    key = load_secret_key()
    print(f"SECRET_KEY loaded ({len(key)} chars)")


if __name__ == "__main__":
    main()
