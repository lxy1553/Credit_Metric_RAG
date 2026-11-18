"""Refresh the offline interview page whenever its source HTML changes."""
import hashlib
import time
from build_interview_offline import SOURCE, main


def fingerprint():
    return hashlib.sha256(SOURCE.read_bytes()).digest()


def watch(interval=2):
    previous = None
    while True:
        try:
            current = fingerprint()
            if current != previous:
                main()
                previous = current
        except OSError as error:
            print(f'等待源文件可读取：{error}', flush=True)
        time.sleep(interval)


if __name__ == '__main__':
    watch()
