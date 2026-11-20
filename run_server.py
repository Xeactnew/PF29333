"""Start the PipsFarmer website + Telegram service + scheduler."""
import uvicorn

from server import config
from server.app import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", server_header=False)
