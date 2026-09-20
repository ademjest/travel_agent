import logging
import socket
import threading
import webbrowser

import uvicorn

from adapters.web_app import create_web_app
from core.settings import SettingsError
from core.web_settings import WebSettings


def main():
    try:
        settings = WebSettings.from_env()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((settings.host, settings.port))
    except (SettingsError, OSError) as error:
        raise SystemExit(f'无法启动网页：{error}。如端口占用，请修改 WEB_PORT。') from None
    app = create_web_app(settings)
    url = f'http://{settings.host}:{settings.port}'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print(f'彼岸 · 本地旅行助手：{url}\nWeb 数据独立保存；QQ 无需在线。')
    if settings.open_browser:
        opener = threading.Timer(1, lambda: webbrowser.open(url))
        opener.daemon = True
        opener.start()
    try:
        uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, workers=1)).run(sockets=[listener])
    finally:
        listener.close()


if __name__ == '__main__':
    main()
