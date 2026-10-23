import json
import os
import subprocess
from urllib.request import ProxyHandler, build_opener

port = int(os.environ.get('DUO_WEB_PORT', '17654'))
opener = build_opener(ProxyHandler({}))
with opener.open(f'http://127.0.0.1:{port}/api/auth/status', timeout=3) as response:
    assert response.status == 200 and 'authenticated' in json.load(response)
subprocess.run(['xdpyinfo'], check=True, timeout=2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
